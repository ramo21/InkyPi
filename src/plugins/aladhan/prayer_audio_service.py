#!/usr/bin/env python3
"""Prayer audio scheduler for InkyPi AlAdhan-based plugins.

This service watches prayer-audio schedule JSON files written by the AlAdhan
and Weather + AlAdhan plugins, then plays iqama and optional adhan audio files at the right
local times. It is intentionally independent from e-paper refreshes.
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

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

LOG = logging.getLogger("inkypi-prayer-audio")
DEFAULT_CONFIG_DIR = Path(os.environ.get("INKYPI_PRAYER_AUDIO_DIR", Path.home() / ".config" / "inkypi" / "prayer_audio"))
STATE_FILE = DEFAULT_CONFIG_DIR / "state.json"
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}


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

    def pick(kind: str) -> Optional[Path]:
        exact = [p for p in files if p.stem.lower() == kind]
        if exact:
            return exact[0]
        partial = [p for p in files if kind in p.stem.lower()]
        return partial[0] if partial else None

    # Fajr commonly uses a different iqama recording. Preferred filename is
    # fajr_iqama.mp3. iqama_fajr.mp3 is also accepted. If neither exists,
    # the service falls back to the regular iqama.mp3.
    fajr_iqama = pick("fajr_iqama") or pick("iqama_fajr")
    regular_iqama = pick("iqama")
    return {
        "adhan": pick("adhan"),
        "iqama": regular_iqama,
        "fajr_iqama": fajr_iqama or regular_iqama,
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
        print("DRY RUN:", " ".join(shlex.quote(part) for part in command))
        return True
    set_volume_if_requested(config)
    try:
        subprocess.run(command, check=False)
        return True
    except Exception as exc:
        LOG.exception("Could not play audio: %s", exc)
        return False


def build_events(config: Dict) -> Iterable[Tuple[datetime, str, Path, Dict]]:
    if not config.get("enabled"):
        return []
    timezone_name = config.get("timezone") or "Etc/UTC"
    date_value = config.get("date")
    selected_reciter = config.get("selectedReciter") or ""
    reciter_dir = Path(config.get("reciterDirectory") or "") / selected_reciter
    files = audio_files_for_reciter(reciter_dir) if reciter_dir.exists() else {"adhan": None, "iqama": None, "fajr_iqama": None}
    sequence = config.get("sequence") or "iqama_then_adhan"
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
        iqama_key = "fajr_iqama" if prayer_name == "Fajr" else "iqama"
        iqama_file = files.get(iqama_key) or files.get("iqama")
        adhan_file = files.get("adhan")
        if iqama_file:
            built.append((base_time, f"{prayer_name}:iqama", iqama_file, config))
        if playback_mode != "iqama_only" and adhan_file:
            built.append((base_time + timedelta(minutes=delay_minutes), f"{prayer_name}:adhan", adhan_file, config))
    return built


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


def run_once(config_dir: Path, dry_run: bool = False, now: Optional[datetime] = None, window_seconds: int = 45) -> int:
    state = load_json(STATE_FILE, {})
    played = state.setdefault("played", {})
    current = now or datetime.now().astimezone()
    count = 0
    for config in load_configs(config_dir):
        for event_time, label, file_path, event_config in build_events(config):
            # Compare aware datetimes in event timezone.
            local_now = current.astimezone(event_time.tzinfo) if event_time.tzinfo else current.replace(tzinfo=None)
            seconds = (local_now - event_time).total_seconds()
            if 0 <= seconds <= window_seconds:
                key = event_key(event_config, event_time, label)
                if played.get(key):
                    continue
                if play_file(file_path, event_config, dry_run=dry_run):
                    played[key] = datetime.now().isoformat(timespec="seconds")
                    count += 1
    # Keep state small.
    today_prefix = datetime.now().date().isoformat()
    if len(played) > 500:
        state["played"] = {k: v for k, v in played.items() if today_prefix in k}
    write_json(STATE_FILE, state)
    return count


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="InkyPi prayer audio scheduler")
    parser.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR), help="Directory containing plugin schedule JSON files")
    parser.add_argument("--interval", type=int, default=20, help="Polling interval in seconds")
    parser.add_argument("--once", action="store_true", help="Check schedules once, then exit")
    parser.add_argument("--dry-run", action="store_true", help="Print player commands instead of playing audio")
    parser.add_argument("--list", action="store_true", help="List upcoming configured audio events")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
    config_dir = Path(args.config_dir).expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)

    if args.list:
        events = []
        for config in load_configs(config_dir):
            events.extend(build_events(config))
        for event_time, label, file_path, config in sorted(events, key=lambda item: item[0]):
            print(f"{event_time.isoformat()}  {config.get('pluginId')}  {label}  {file_path}")
        return 0

    if args.once:
        return 0 if run_once(config_dir, dry_run=args.dry_run) >= 0 else 1

    LOG.info("Starting prayer audio scheduler; config dir: %s", config_dir)
    while True:
        try:
            run_once(config_dir, dry_run=args.dry_run)
        except Exception:
            LOG.exception("Scheduler loop failed")
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
