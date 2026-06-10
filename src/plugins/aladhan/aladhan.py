from plugins.base_plugin.base_plugin import BasePlugin

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
import logging
import os
from pathlib import Path
import random
import re

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.aladhan.com/v1"

PRAYER_KEYS = ["Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha"]
OPTIONAL_KEYS = ["Imsak", "Sunset", "Midnight"]

METHODS = [
    {"id": 2, "name": "Islamic Society of North America (ISNA)"},
    {"id": 3, "name": "Muslim World League"},
    {"id": 5, "name": "Egyptian General Authority of Survey"},
    {"id": 4, "name": "Umm Al-Qura University, Makkah"},
    {"id": 1, "name": "University of Islamic Sciences, Karachi"},
    {"id": 8, "name": "Gulf Region"},
    {"id": 9, "name": "Kuwait"},
    {"id": 10, "name": "Qatar"},
    {"id": 11, "name": "Singapore"},
    {"id": 12, "name": "France / UOIF"},
    {"id": 13, "name": "Turkey / Diyanet"},
    {"id": 14, "name": "Russia"},
    {"id": 15, "name": "Moonsighting Committee"},
    {"id": 16, "name": "Dubai"},
    {"id": 17, "name": "JAKIM Malaysia"},
    {"id": 18, "name": "Tunisia"},
    {"id": 19, "name": "Algeria"},
    {"id": 20, "name": "KEMENAG Indonesia"},
    {"id": 21, "name": "Morocco"},
    {"id": 22, "name": "Portugal / Lisboa"},
    {"id": 23, "name": "Jordan"},
    {"id": 0, "name": "Jafari / Shia Ithna-Ashari"},
    {"id": 7, "name": "Tehran"},
    {"id": 99, "name": "Custom angles"},
]


def _to_bool(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in ["1", "true", "yes", "on"]


def _is_valid_timezone(timezone_name):
    if not timezone_name:
        return False
    try:
        ZoneInfo(str(timezone_name))
        return True
    except Exception:
        return False


class Aladhan(BasePlugin):
    """InkyPi plugin for AlAdhan prayer times, Hijri dates, and Ramadan calendar."""

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = True
        template_params["methods"] = METHODS
        template_params["device_timezone"] = self.get_system_timezone(default="Etc/UTC")
        template_params["audio_default_dir"] = self.default_audio_directory()
        template_params["audio_reciters"] = self.scan_audio_reciters(self.default_audio_directory())
        return template_params

    def generate_image(self, settings, device_config):
        settings = self._normalise_settings(settings)
        lat = self._coordinate_float(settings, "latitude", "Latitude", -90, 90)
        lon = self._coordinate_float(settings, "longitude", "Longitude", -180, 180)

        timezone_name = self.resolve_timezone(settings, device_config)
        try:
            tz = ZoneInfo(timezone_name)
        except Exception:
            raise RuntimeError(f"Invalid timezone: {timezone_name}")

        now = datetime.now(tz)
        mode = settings.get("displayMode", "today")
        time_format = settings.get("timeFormat") or device_config.get_config(
            "time_format", default="12h"
        )

        try:
            today_data = self.get_timings(now, lat, lon, timezone_name, settings)
            self.write_prayer_audio_schedule(settings, today_data.get("timings", {}), timezone_name, now, "aladhan")
            current_prayer, next_prayer = self.get_prayer_status(today_data, now, time_format)
            calendar_rows = []
            ramadan_rows = []
            ramadan_title = None

            if mode == "calendar":
                calendar_rows = self.get_calendar_rows(now, lat, lon, timezone_name, settings)

            if mode == "ramadan":
                hijri = today_data.get("date", {}).get("hijri", {})
                ramadan_year = self.get_ramadan_hijri_year(settings, hijri)
                ramadan_rows = self.get_hijri_calendar_rows(
                    ramadan_year, 9, lat, lon, timezone_name, settings
                )
                ramadan_title = f"Ramadan {ramadan_year} AH"
        except RuntimeError:
            raise
        except Exception as exc:
            logger.exception("AlAdhan API request failed")
            raise RuntimeError(f"AlAdhan request failed: {exc}")

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        template_params = {
            "title": settings.get("customTitle") or "Prayer Times",
            "mode": mode,
            "time_format": time_format,
            "today": self.parse_today(today_data, time_format, settings),
            "calendar_rows": calendar_rows,
            "ramadan_rows": ramadan_rows,
            "ramadan_title": ramadan_title,
            "current_prayer": current_prayer,
            "next_prayer": next_prayer,
            "method_name": today_data.get("meta", {}).get("method", {}).get("name", ""),
            "location_label": settings.get("locationName") or "",
            "last_refresh_time": self.format_dt(now, time_format),
            "daily_refresh_time": settings.get("dailyRefreshTime", "00:05"),
            "daily_refresh_label": self.format_hhmm(settings.get("dailyRefreshTime", "00:05"), time_format),
            "plugin_settings": settings,
        }

        image = self.render_image(dimensions, "aladhan.html", "aladhan.css", template_params)
        if not image:
            raise RuntimeError("Failed to render AlAdhan image. Please check logs.")
        return image

    def default_audio_directory(self):
        configured = os.environ.get("INKYPI_PRAYER_AUDIO_LIBRARY")
        if configured:
            return configured
        inkypi_home = Path("/home/inkypi")
        if inkypi_home.exists():
            return str(inkypi_home / "adhan_audio")
        return str(Path.home() / "adhan_audio")

    def audio_config_dir(self):
        configured = os.environ.get("INKYPI_PRAYER_AUDIO_DIR")
        if configured:
            return Path(configured).expanduser()
        return Path.home() / ".config" / "inkypi" / "prayer_audio"

    def scan_audio_reciters(self, audio_dir):
        base = Path(str(audio_dir or self.default_audio_directory())).expanduser()
        if not base.exists() or not base.is_dir():
            return []
        names = []
        for child in sorted(base.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if self.reciter_has_audio(child):
                names.append(child.name)
        return names

    def reciter_has_audio(self, reciter_dir):
        extensions = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}
        try:
            files = [p for p in Path(reciter_dir).iterdir() if p.is_file() and p.suffix.lower() in extensions]
        except Exception:
            return False
        names = [p.stem.lower() for p in files]
        return any("adhan" in name for name in names) or any("iqama" in name for name in names)

    def clean_audio_timing(self, value):
        if not value:
            return None
        raw = str(value).strip()
        if "T" in raw:
            raw = raw.split("T", 1)[1]
        raw = raw.split(" ", 1)[0]
        match = re.search(r"(\d{1,2}):(\d{2})", raw)
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return None
        return f"{hour:02d}:{minute:02d}"

    def selected_audio_reciter(self, settings, now):
        audio_dir = settings.get("audioReciterDirectory") or self.default_audio_directory()
        reciters = self.scan_audio_reciters(audio_dir)
        manual = str(settings.get("audioReciterManualName") or "").strip()
        selected = str(settings.get("audioReciterName") or "").strip()
        if manual:
            selected = manual
        if settings.get("audioReciterMode") == "random_daily" and reciters:
            rng = random.Random(f"{now.strftime('%Y-%m-%d')}|aladhan|{'|'.join(reciters)}")
            return rng.choice(reciters)
        return selected if selected else (reciters[0] if reciters else "")

    def enabled_audio_prayers(self, settings):
        prayers = []
        for key in ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]:
            setting_key = f"audioPrayer{key}"
            if _to_bool(settings.get(setting_key), True):
                prayers.append(key)
        return prayers

    def display_refresh_events(self, settings):
        """Return prayer/state-change events that should refresh the display.

        Main prayers are controlled by dedicated refresh checkboxes. Optional
        events follow the corresponding Show/Hide options, so Sunrise only
        triggers a refresh when Sunrise is displayed.
        """
        events = []
        for key in ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]:
            if _to_bool(settings.get(f"stateRefreshPrayer{key}"), True):
                events.append(key)

        optional_event_controls = {
            "Imsak": "showImsak",
            "Sunrise": "showSunrise",
            "Sunset": "showSunset",
            "Midnight": "showMidnight",
        }
        # Weather + AlAdhan uses showSunrisePrayer for the Sunrise row.
        if "showSunrisePrayer" in settings:
            optional_event_controls["Sunrise"] = "showSunrisePrayer"

        for event_name, show_key in optional_event_controls.items():
            if _to_bool(settings.get(show_key), False) and _to_bool(settings.get(f"stateRefresh{event_name}"), True):
                events.append(event_name)
        return events

    def display_refresh_timings(self, timings):
        values = {}
        for key in ["Imsak", "Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Sunset", "Isha", "Midnight"]:
            clean = self.clean_audio_timing(timings.get(key))
            if clean:
                values[key] = clean
        return values

    def display_refresh_delay_seconds(self, settings):
        try:
            return max(0, min(3600, int(float(settings.get("stateDisplayRefreshDelaySeconds") or 60))))
        except Exception:
            return 60

    def write_prayer_audio_schedule(self, settings, timings, timezone_name, now, plugin_id):
        config_dir = self.audio_config_dir()
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / f"{plugin_id}.json"
        enabled = _to_bool(settings.get("audioEnabled"), False)
        prayers = {}
        for key in ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]:
            clean = self.clean_audio_timing(timings.get(key))
            if clean:
                prayers[key] = clean
        try:
            delay = max(0, min(180, int(float(settings.get("audioDelayMinutes") or 10))))
        except Exception:
            delay = 10
        try:
            volume = max(0, min(100, int(float(settings.get("audioVolumePercent") or 80))))
        except Exception:
            volume = 80
        playback_mode = settings.get("audioPlaybackMode") if settings.get("audioPlaybackMode") in {"both", "iqama_only"} else "both"
        sequence = "iqama_then_adhan"
        refresh_settings = dict(settings or {})
        refresh_settings["plugin_id"] = plugin_id
        payload = {
            "pluginId": plugin_id,
            "enabled": enabled,
            "date": now.strftime("%Y-%m-%d"),
            "timezone": timezone_name,
            "prayers": prayers,
            "enabledPrayers": self.enabled_audio_prayers(settings),
            "reciterDirectory": str(Path(settings.get("audioReciterDirectory") or self.default_audio_directory()).expanduser()),
            "reciterMode": settings.get("audioReciterMode", "specific"),
            "selectedReciter": self.selected_audio_reciter(settings, now),
            "sequence": sequence,
            "playbackMode": playback_mode,
            "delayMinutes": delay,
            "volumePercent": volume,
            "playerCommand": str(settings.get("audioPlayerCommand") or "").strip(),
            "displayRefreshEnabled": _to_bool(settings.get("stateDisplayRefreshEnabled"), True),
            "displayRefreshDelaySeconds": self.display_refresh_delay_seconds(settings),
            "displayRefreshEvents": self.display_refresh_events(settings),
            "refreshTimings": self.display_refresh_timings(timings),
            "displayRefreshUrl": str(settings.get("displayRefreshUrl") or "http://127.0.0.1/update_now").strip(),
            "displayRefreshCommand": str(settings.get("displayRefreshCommand") or "").strip(),
            "displayUpdateSettings": refresh_settings,
            "writtenAt": datetime.now().isoformat(timespec="seconds"),
        }
        tmp = config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(config_path)
        logger.info("Prayer audio schedule written to %s", config_path)

    def _normalise_settings(self, settings):
        defaults = {
            "displayMode": "today",
            "method": "2",
            "school": "0",
            "midnightMode": "0",
            "latitudeAdjustmentMethod": "3",
            "adjustment": "0",
            "shafaq": "general",
            "timeFormat": "12h",
            "dailyRefreshTime": "00:05",
            "calendarDays": "7",
            "ramadanYearMode": "upcoming",
            "displayRefreshTime": "true",
            "showHijri": "true",
            "showGregorian": "true",
            "showImsak": "false",
            "showSunrise": "true",
            "showSunset": "false",
            "showMidnight": "false",
            "showNextPrayer": "true",
            "audioEnabled": "false",
            "audioReciterDirectory": self.default_audio_directory(),
            "audioReciterMode": "specific",
            "audioReciterName": "",
            "audioReciterManualName": "",
            "audioSequence": "iqama_then_adhan",
            "audioPlaybackMode": "both",
            "audioDelayMinutes": "10",
            "audioVolumePercent": "80",
            "audioPlayerCommand": "",
            "stateDisplayRefreshEnabled": "true",
            "stateDisplayRefreshDelaySeconds": "60",
            "displayRefreshUrl": "http://127.0.0.1/update_now",
            "displayRefreshCommand": "",
            "stateRefreshPrayerFajr": "true",
            "stateRefreshPrayerDhuhr": "true",
            "stateRefreshPrayerAsr": "true",
            "stateRefreshPrayerMaghrib": "true",
            "stateRefreshPrayerIsha": "true",
            "stateRefreshImsak": "true",
            "stateRefreshSunrise": "true",
            "stateRefreshSunset": "true",
            "stateRefreshMidnight": "true",
            "audioPrayerFajr": "true",
            "audioPrayerDhuhr": "true",
            "audioPrayerAsr": "true",
            "audioPrayerMaghrib": "true",
            "audioPrayerIsha": "true",
        }
        normalised = dict(defaults)
        normalised.update(settings or {})
        for key in [
            "displayRefreshTime",
            "showHijri",
            "showGregorian",
            "showImsak",
            "showSunrise",
            "showSunset",
            "showMidnight",
            "showNextPrayer",
            "audioEnabled",
            "stateDisplayRefreshEnabled",
            "stateRefreshPrayerFajr",
            "stateRefreshPrayerDhuhr",
            "stateRefreshPrayerAsr",
            "stateRefreshPrayerMaghrib",
            "stateRefreshPrayerIsha",
            "stateRefreshImsak",
            "stateRefreshSunrise",
            "stateRefreshSunset",
            "stateRefreshMidnight",
            "audioPrayerFajr",
            "audioPrayerDhuhr",
            "audioPrayerAsr",
            "audioPrayerMaghrib",
            "audioPrayerIsha",
        ]:
            normalised[key] = "true" if _to_bool(normalised.get(key)) else "false"
        normalised["dailyRefreshTime"] = self.normalise_hhmm(
            normalised.get("dailyRefreshTime", "00:05"), "Daily refresh time"
        )
        return normalised

    def resolve_timezone(self, settings, device_config):
        """Resolve timezone using explicit setting, then InkyPi/Pi timezone, then UTC.

        A manually entered timezone should fail loudly if invalid. If the field is
        blank, the plugin follows the Pi/InkyPi timezone so prayer calculations
        track the device's configured clock and daylight-saving rules.
        """
        explicit_timezone = str(settings.get("timezonestring") or "").strip()
        if explicit_timezone:
            if _is_valid_timezone(explicit_timezone):
                return explicit_timezone
            raise RuntimeError(f"Invalid timezone: {explicit_timezone}")

        device_timezone = self.get_device_timezone(device_config)
        if _is_valid_timezone(device_timezone):
            return device_timezone

        system_timezone = self.get_system_timezone(default="Etc/UTC")
        if _is_valid_timezone(system_timezone):
            return system_timezone

        return "Etc/UTC"

    def get_device_timezone(self, device_config):
        """Read the timezone configured in InkyPi, if available."""
        if not device_config:
            return None
        try:
            return device_config.get_config("timezone", default=None)
        except TypeError:
            try:
                return device_config.get_config("timezone")
            except Exception:
                return None
        except Exception:
            return None

    def get_system_timezone(self, default="Etc/UTC"):
        """Best-effort Pi/Linux timezone detection for settings defaults.

        Raspberry Pi OS usually exposes the timezone through /etc/timezone or the
        /etc/localtime symlink to /usr/share/zoneinfo/<Area>/<City>.
        """
        tz_env = os.environ.get("TZ")
        if tz_env and not tz_env.startswith(":") and _is_valid_timezone(tz_env):
            return tz_env

        try:
            tz_file = "/etc/timezone"
            if os.path.exists(tz_file):
                timezone_name = open(tz_file, "r", encoding="utf-8").read().strip()
                if _is_valid_timezone(timezone_name):
                    return timezone_name
        except Exception:
            pass

        try:
            localtime = "/etc/localtime"
            if os.path.islink(localtime):
                target = os.path.realpath(localtime)
                marker = "/zoneinfo/"
                if marker in target:
                    timezone_name = target.split(marker, 1)[1]
                    if _is_valid_timezone(timezone_name):
                        return timezone_name
        except Exception:
            pass

        return default

    def _required_float(self, settings, key, label):
        value = settings.get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            raise RuntimeError(f"{label} is required and must be a number.")

    def _coordinate_float(self, settings, key, label, minimum, maximum):
        value = self._required_float(settings, key, label)
        if not minimum <= value <= maximum:
            raise RuntimeError(f"{label} must be between {minimum} and {maximum}.")
        return value

    def normalise_hhmm(self, value, label):
        value = str(value or "00:05").strip()
        if not re.fullmatch(r"\d{1,2}:\d{2}", value):
            raise RuntimeError(f"{label} must use 24-hour HH:MM format, e.g. 00:05.")
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        if hour > 23 or minute > 59:
            raise RuntimeError(f"{label} must be a valid time between 00:00 and 23:59.")
        return f"{hour:02d}:{minute:02d}"

    def format_hhmm(self, value, time_format):
        value = self.normalise_hhmm(value, "Daily refresh time")
        hour, minute = [int(part) for part in value.split(":", 1)]
        dt = datetime(2000, 1, 1, hour, minute)
        return self.format_dt(dt, time_format)

    def build_params(self, lat, lon, timezone_name, settings):
        params = {
            "latitude": lat,
            "longitude": lon,
            "method": settings.get("method", "2"),
            "school": settings.get("school", "0"),
            "midnightMode": settings.get("midnightMode", "0"),
            "latitudeAdjustmentMethod": settings.get("latitudeAdjustmentMethod", "3"),
            "adjustment": settings.get("adjustment", "0"),
            "timezonestring": timezone_name,
            "iso8601": "true",
        }

        if settings.get("method") == "99" and settings.get("methodSettings"):
            params["methodSettings"] = settings.get("methodSettings")

        if settings.get("tune"):
            if not re.fullmatch(r"-?\d+(,-?\d+){0,8}", settings.get("tune", "")):
                raise RuntimeError(
                    "Tune must be comma-separated minute offsets, e.g. 0,2,0,1,0,0,0,2,0"
                )
            params["tune"] = settings.get("tune")

        if settings.get("shafaq"):
            params["shafaq"] = settings.get("shafaq")

        return params

    def request_json(self, endpoint, params=None):
        response = requests.get(f"{API_BASE}/{endpoint}", params=params or {}, timeout=30)
        if not 200 <= response.status_code < 300:
            logger.error("AlAdhan API failure: %s %s", response.status_code, response.text)
            raise RuntimeError("Failed to retrieve data from AlAdhan.")
        payload = response.json()
        if payload.get("code") != 200:
            logger.error("AlAdhan API returned non-OK payload: %s", payload)
            raise RuntimeError(payload.get("status", "AlAdhan returned an error."))
        return payload.get("data")

    def get_timings(self, dt, lat, lon, timezone_name, settings):
        date_arg = dt.strftime("%d-%m-%Y")
        return self.request_json(
            f"timings/{date_arg}", self.build_params(lat, lon, timezone_name, settings)
        )

    def get_calendar_rows(self, now, lat, lon, timezone_name, settings):
        days = self.get_calendar_days(settings)
        rows = []
        cursor = now

        # Fetch the current month and, when needed, continue into following
        # months so a late-month start can still show the requested number of rows.
        while len(rows) < days and len(rows) < 30:
            data = self.request_json(
                f"calendar/{cursor.year}/{cursor.month}",
                self.build_params(lat, lon, timezone_name, settings),
            )
            for item in data:
                date_data = item.get("date", {}).get("gregorian", {})
                greg_day = int(date_data.get("day", 0))
                if cursor.year == now.year and cursor.month == now.month and greg_day < now.day:
                    continue
                rows.append(self.parse_calendar_day(item, settings.get("timeFormat", "12h"), settings))
                if len(rows) >= days:
                    break

            if len(rows) >= days:
                break
            cursor = (cursor.replace(day=1) + timedelta(days=32)).replace(day=1)

        return rows

    def get_hijri_calendar_rows(self, year, month, lat, lon, timezone_name, settings):
        days = self.get_calendar_days(settings)
        data = self.request_json(
            f"hijriCalendar/{year}/{month}", self.build_params(lat, lon, timezone_name, settings)
        )
        return [
            self.parse_calendar_day(item, settings.get("timeFormat", "12h"), settings)
            for item in data[:days]
        ]

    def get_calendar_days(self, settings):
        try:
            return max(1, min(int(settings.get("calendarDays", 7)), 30))
        except (TypeError, ValueError):
            raise RuntimeError("Calendar rows must be a number between 1 and 30.")

    def get_ramadan_hijri_year(self, settings, hijri):
        if settings.get("ramadanYearMode") == "custom" and settings.get("ramadanHijriYear"):
            try:
                return int(settings.get("ramadanHijriYear"))
            except ValueError:
                raise RuntimeError("Custom Ramadan Hijri year must be a number, e.g. 1447.")

        year = int(hijri.get("year"))
        month = int(hijri.get("month", {}).get("number"))
        if settings.get("ramadanYearMode") == "current":
            return year
        # Upcoming Ramadan: if this year's Ramadan has passed, use next Hijri year.
        return year + 1 if month > 9 else year

    def parse_today(self, data, time_format, settings):
        timings = data.get("timings", {})
        date_data = data.get("date", {})
        hijri = date_data.get("hijri", {})
        gregorian = date_data.get("gregorian", {})

        visible_keys = list(PRAYER_KEYS)
        if settings.get("showImsak") == "true":
            visible_keys.insert(0, "Imsak")
        if settings.get("showSunrise") != "true" and "Sunrise" in visible_keys:
            visible_keys.remove("Sunrise")
        if settings.get("showSunset") == "true":
            visible_keys.append("Sunset")
        if settings.get("showMidnight") == "true":
            visible_keys.append("Midnight")

        prayers = [{"name": key, "time": self.format_api_time(timings.get(key), time_format)} for key in visible_keys]

        return {
            "prayers": prayers,
            "hijri": self.format_hijri(hijri),
            "gregorian": gregorian.get("date") or date_data.get("readable", ""),
            "weekday": gregorian.get("weekday", {}).get("en", ""),
            "holidays": hijri.get("holidays", []),
        }

    def parse_calendar_day(self, item, time_format, settings):
        timings = item.get("timings", {})
        date_data = item.get("date", {})
        hijri = date_data.get("hijri", {})
        gregorian = date_data.get("gregorian", {})
        return {
            "gregorian_day": gregorian.get("day", ""),
            "gregorian_month": gregorian.get("month", {}).get("en", ""),
            "weekday": gregorian.get("weekday", {}).get("en", ""),
            "hijri_day": hijri.get("day", ""),
            "hijri_month": hijri.get("month", {}).get("en", ""),
            "hijri_year": hijri.get("year", ""),
            "fajr": self.format_api_time(timings.get("Fajr"), time_format),
            "sunrise": self.format_api_time(timings.get("Sunrise"), time_format),
            "maghrib": self.format_api_time(timings.get("Maghrib"), time_format),
            "isha": self.format_api_time(timings.get("Isha"), time_format),
            "holidays": hijri.get("holidays", []),
        }

    def get_prayer_status(self, data, now, time_format):
        timings = data.get("timings", {})
        prayer_times = []
        for key in ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]:
            parsed = self.parse_iso_time(timings.get(key))
            if parsed:
                prayer_times.append((key, parsed))

        current = None
        next_prayer = None
        for index, (name, dt) in enumerate(prayer_times):
            if now < dt:
                next_prayer = {"name": name, "time": self.format_dt(dt, time_format)}
                if index > 0:
                    prev = prayer_times[index - 1]
                    current = {"name": prev[0], "time": self.format_dt(prev[1], time_format)}
                break

        if not next_prayer and prayer_times:
            current = {"name": prayer_times[-1][0], "time": self.format_dt(prayer_times[-1][1], time_format)}
            next_prayer = {"name": "Fajr", "time": self.format_dt(prayer_times[0][1] + timedelta(days=1), time_format)}

        return current, next_prayer

    def parse_iso_time(self, value):
        if not value:
            return None
        clean_value = str(value).split(" ")[0]
        try:
            return datetime.fromisoformat(clean_value)
        except ValueError:
            return None

    def format_api_time(self, value, time_format):
        parsed = self.parse_iso_time(value)
        if parsed:
            return self.format_dt(parsed, time_format)
        if not value:
            return "--"
        # Handles HH:MM strings if iso8601 is disabled by API changes.
        return str(value).split(" ")[0]

    def format_dt(self, dt, time_format):
        if time_format == "24h":
            return dt.strftime("%H:%M")
        return dt.strftime("%I:%M %p").lstrip("0")

    def format_hijri(self, hijri):
        day = hijri.get("day", "")
        month = hijri.get("month", {}).get("en", "")
        year = hijri.get("year", "")
        if not any([day, month, year]):
            return ""
        return f"{day} {month} {year} AH".strip()
