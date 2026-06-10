#!/usr/bin/env python3
"""Prayer audio and display-refresh scheduler for InkyPi AlAdhan-based plugins.

This service watches schedule JSON files written by the AlAdhan and
Weather + AlAdhan plugins. It can:

1. Play adhan and optional iqama audio at prayer times.
2. Trigger an InkyPi display refresh shortly after prayer/state-change times,
   so the static e-paper image updates the current/next prayer highlight.

The display refresh uses InkyPi's local /update_now endpoint by default. That
endpoint performs the same kind of manual plugin render used by the web UI.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple
from urllib import parse as urlparse
from urllib import request as urlrequest

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

LOG = logging.getLogger("inkypi-prayer-audio")
DEFAULT_CONFIG_DIR = Path(os.environ.get("INKYPI_PRAYER_AUDIO_DIR", Path.home() / ".config" / "inkypi" / "prayer_audio"))
STATE_FILE = DEFAULT_CONFIG_DIR / "state.json"
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}
DEFAULT_DISPLAY_REFRESH_URL = os.environ.get("INKYPI_DISPLAY_REFRESH_URL", "http://127.0.0.1/update_now")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def clean_time(value: str) -> str:
    return str(value or "").split(" ", 1)[0].strip()


def parse_event_time(date_value: str, time_value: str, timezone_name: str) -> Optional[datetime]:
    try:
        hour, minute = [int(part) for part in clean_time(time_value).split(":")[:2]]
        tz = ZoneInfo(timezone_name) if ZoneInfo else None
        year, month, day = [int(part) for part in date_value.split("-")]
        return datetime(year, month, day, hour, minute, 0, tzinfo=tz)
    except Exception:
        return None


def audio_files_for_reciter(reciter_dir: Path) -> Dict[str, Optional[Path]]:
    files = [p for p in reciter_dir.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS]

    def pick(kind: str, exclude: Tuple[str, ...] = ()) -> Optional[Path]:
        exact = [p for p in files if p.stem.lower() == kind]
        if exact:
            return exact[0]
        partial = [
            p for p in files
            if kind in p.stem.lower() and all(blocked not in p.stem.lower() for blocked in exclude)
        ]
        return partial[0] if partial else None

    regular_adhan = pick("adhan", exclude=("fajr_adhan",))
    return {
        "adhan": regular_adhan,
        "fajr_adhan": pick("fajr_adhan") or regular_adhan,
        "iqama": pick("iqama"),
    }


def find_player_command(file_path: Path, config: Dict) -> Optional[List[str]]:
    template = str(config.get("playerCommand") or "").strip()
    if template:
        return [part.replace("{file}", str(file_path)) for part in shlex.split(template)]
    for candidate in ("mpg123", "ffplay", "cvlc", "aplay", "paplay"):
        executable = shutil.which(candidate)
        if not executable:
            continue
        if candidate == "mpg123":
            return [executable, "-q", str(file_path)]
        if candidate == "ffplay":
            return [executable, "-nodisp", "-autoexit", "-loglevel", "quiet", str(file_path)]
        if candidate == "cvlc":
            return [executable, "--play-and-exit", "--quiet", str(file_path)]
        return [executable, str(file_path)]
    return None


def set_volume_if_requested(config: Dict) -> None:
    value = str(config.get("volumePercent") or "").strip()
    if not value:
        return
    try:
        percent = max(0, min(100, int(float(value))))
    except Exception:
        return
    amixer = shutil.which("amixer")
    if not amixer:
        return
    try:
        subprocess.run([amixer, "sset", "Master", f"{percent}%"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def play_file(file_path: Path, config: Dict, dry_run: bool = False) -> bool:
    if not file_path or not file_path.exists():
        LOG.warning("Audio file missing: %s", file_path)
        return False
    command = find_player_command(file_path, config)
    if not command:
        LOG.error("No supported audio player found. Install mpg123 or ffmpeg, or set playerCommand.")
        return False
    LOG.info("Playing %s", file_path)
    if dry_run:
        print("DRY RUN AUDIO:", " ".join(shlex.quote(part) for part in command))
        return True
    set_volume_if_requested(config)
    try:
        subprocess.run(command, check=False)
        return True
    except Exception as exc:
        LOG.exception("Could not play audio: %s", exc)
        return False


def build_audio_events(config: Dict) -> Iterable[Tuple[datetime, str, Path, Dict]]:
    if not config.get("enabled"):
        return []
    timezone_name = config.get("timezone") or "Etc/UTC"
    date_value = config.get("date")
    selected_reciter = config.get("selectedReciter") or ""
    reciter_dir = Path(config.get("reciterDirectory") or "") / selected_reciter
    files = audio_files_for_reciter(reciter_dir) if reciter_dir.exists() else {"adhan": None, "fajr_adhan": None, "iqama": None}
    delay_minutes = int(config.get("delayMinutes") or 10)
    prayers = config.get("prayers") or {}
    enabled_prayers = set(config.get("enabledPrayers") or [])
    if not date_value or not selected_reciter:
        return []
    built = []
    for prayer_name, prayer_time in prayers.items():
        if enabled_prayers and prayer_name not in enabled_prayers:
            continue
        base_time = parse_event_time(date_value, prayer_time, timezone_name)
        if not base_time:
            continue
        playback_mode = config.get("playbackMode") or "both"
        # Backward compatibility: older builds used iqama_only when the first file
        # was incorrectly labeled as iqama. After the naming correction, that mode
        # means play only the first call, which is adhan.
        if playback_mode == "iqama_only":
            playback_mode = "adhan_only"
        adhan_key = "fajr_adhan" if prayer_name == "Fajr" else "adhan"
        adhan_file = files.get(adhan_key) or files.get("adhan")
        iqama_file = files.get("iqama")
        if adhan_file:
            built.append((base_time, f"{prayer_name}:adhan", adhan_file, config))
        if playback_mode != "adhan_only" and iqama_file:
            built.append((base_time + timedelta(minutes=delay_minutes), f"{prayer_name}:iqama", iqama_file, config))
    return built


def build_display_refresh_events(config: Dict) -> Iterable[Tuple[datetime, str, Dict]]:
    """Build display-refresh events from a plugin schedule config."""
    if not config.get("displayRefreshEnabled"):
        return []
    timezone_name = config.get("timezone") or "Etc/UTC"
    date_value = config.get("date")
    timings = config.get("refreshTimings") or config.get("prayers") or {}
    enabled_events = set(config.get("displayRefreshEvents") or [])
    if not date_value or not enabled_events:
        return []
    try:
        delay_seconds = max(0, min(3600, int(float(config.get("displayRefreshDelaySeconds") or 60))))
    except Exception:
        delay_seconds = 60
    built = []
    for event_name, event_time_value in timings.items():
        if event_name not in enabled_events:
            continue
        base_time = parse_event_time(date_value, event_time_value, timezone_name)
        if not base_time:
            continue
        built.append((base_time + timedelta(seconds=delay_seconds), f"{event_name}:display", config))
    return built


def trigger_display_refresh(config: Dict, label: str, dry_run: bool = False) -> bool:
    """Trigger InkyPi to re-render this plugin using the saved settings."""
    settings = config.get("displayUpdateSettings") or {}
    plugin_id = settings.get("plugin_id") or config.get("pluginId")
    if not plugin_id:
        LOG.error("Display refresh requested but no plugin_id was configured.")
        return False
    settings = dict(settings)
    settings["plugin_id"] = plugin_id
    command_template = str(config.get("displayRefreshCommand") or "").strip()
    url = str(config.get("displayRefreshUrl") or DEFAULT_DISPLAY_REFRESH_URL).strip()

    if command_template:
        replacements = {
            "{label}": label,
            "{plugin_id}": str(plugin_id),
            "{url}": url,
        }
        command = []
        for part in shlex.split(command_template):
            for token, value in replacements.items():
                part = part.replace(token, value)
            command.append(part)
        LOG.info("Triggering display refresh with command for %s", label)
        if dry_run:
            print("DRY RUN DISPLAY:", " ".join(shlex.quote(part) for part in command))
            return True
        try:
            completed = subprocess.run(command, check=False)
            return completed.returncode == 0
        except Exception as exc:
            LOG.exception("Display refresh command failed: %s", exc)
            return False

    encoded = urlparse.urlencode(settings).encode("utf-8")
    request = urlrequest.Request(url, data=encoded, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    LOG.info("Triggering InkyPi display refresh for %s via %s", label, url)
    if dry_run:
        print(f"DRY RUN DISPLAY: POST {url} plugin_id={plugin_id} label={label}")
        return True
    try:
        with urlrequest.urlopen(request, timeout=180) as response:
            body = response.read(512).decode("utf-8", errors="replace")
            if 200 <= response.status < 300:
                LOG.info("Display refresh request accepted: %s", body[:200])
                return True
            LOG.error("Display refresh HTTP %s: %s", response.status, body[:200])
            return False
    except Exception as exc:
        LOG.exception("Display refresh request failed: %s", exc)
        return False


def load_configs(config_dir: Path) -> List[Dict]:
    configs = []
    for path in sorted(config_dir.glob("*.json")):
        if path.name == STATE_FILE.name:
            continue
        payload = load_json(path, None)
        if isinstance(payload, dict):
            payload["_path"] = str(path)
            configs.append(payload)
    return configs


def event_key(config: Dict, event_time: datetime, label: str) -> str:
    return f"{config.get('pluginId','plugin')}|{event_time.date().isoformat()}|{label}"


def should_fire_event(current: datetime, event_time: datetime, window_seconds: int) -> bool:
    local_now = current.astimezone(event_time.tzinfo) if event_time.tzinfo else current.replace(tzinfo=None)
    seconds = (local_now - event_time).total_seconds()
    return 0 <= seconds <= window_seconds


def run_once(config_dir: Path, dry_run: bool = False, now: Optional[datetime] = None, window_seconds: int = 45) -> int:
    state_file = config_dir / STATE_FILE.name
    state = load_json(state_file, {})
    played = state.setdefault("played", {})
    refreshed = state.setdefault("display_refreshed", {})
    current = now or datetime.now().astimezone()
    count = 0
    for config in load_configs(config_dir):
        for event_time, label, file_path, event_config in build_audio_events(config):
            if should_fire_event(current, event_time, window_seconds):
                key = event_key(event_config, event_time, label)
                if played.get(key):
                    continue
                if play_file(file_path, event_config, dry_run=dry_run):
                    played[key] = datetime.now().isoformat(timespec="seconds")
                    count += 1

        for event_time, label, event_config in build_display_refresh_events(config):
            if should_fire_event(current, event_time, window_seconds):
                key = event_key(event_config, event_time, label)
                if refreshed.get(key):
                    continue
                if trigger_display_refresh(event_config, label, dry_run=dry_run):
                    refreshed[key] = datetime.now().isoformat(timespec="seconds")
                    count += 1

    # Keep state small.
    today_prefix = datetime.now().date().isoformat()
    if len(played) > 500:
        state["played"] = {k: v for k, v in played.items() if today_prefix in k}
    if len(refreshed) > 500:
        state["display_refreshed"] = {k: v for k, v in refreshed.items() if today_prefix in k}
    write_json(state_file, state)
    return count


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="InkyPi prayer audio and display-refresh scheduler")
    parser.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR), help="Directory containing plugin schedule JSON files")
    parser.add_argument("--interval", type=int, default=20, help="Polling interval in seconds")
    parser.add_argument("--once", action="store_true", help="Check schedules once, then exit")
    parser.add_argument("--dry-run", action="store_true", help="Print actions instead of playing audio or refreshing display")
    parser.add_argument("--list", action="store_true", help="List upcoming configured audio and display-refresh events")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
    config_dir = Path(args.config_dir).expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)

    if args.list:
        audio_events = []
        display_events = []
        for config in load_configs(config_dir):
            audio_events.extend(build_audio_events(config))
            display_events.extend(build_display_refresh_events(config))
        for event_time, label, file_path, config in sorted(audio_events, key=lambda item: item[0]):
            print(f"{event_time.isoformat()}  {config.get('pluginId')}  audio:{label}  {file_path}")
        for event_time, label, config in sorted(display_events, key=lambda item: item[0]):
            print(f"{event_time.isoformat()}  {config.get('pluginId')}  refresh:{label}  {config.get('displayRefreshUrl') or DEFAULT_DISPLAY_REFRESH_URL}")
        return 0

    if args.once:
        return 0 if run_once(config_dir, dry_run=args.dry_run) >= 0 else 1

    LOG.info("Starting prayer audio/display scheduler; config dir: %s", config_dir)
    while True:
        try:
            run_once(config_dir, dry_run=args.dry_run)
        except Exception:
            LOG.exception("Scheduler loop failed")
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
