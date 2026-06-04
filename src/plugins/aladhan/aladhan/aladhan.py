from plugins.base_plugin.base_plugin import BasePlugin

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import logging
import re

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.aladhan.com/v1"

PRAYER_KEYS = ["Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha"]
OPTIONAL_KEYS = ["Imsak", "Sunset", "Midnight"]

METHODS = [
    {"id": 3, "name": "Muslim World League"},
    {"id": 2, "name": "Islamic Society of North America (ISNA)"},
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


class Aladhan(BasePlugin):
    """InkyPi plugin for AlAdhan prayer times, Hijri dates, and Ramadan calendar."""

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = True
        template_params["methods"] = METHODS
        return template_params

    def generate_image(self, settings, device_config):
        settings = self._normalise_settings(settings)
        lat = self._coordinate_float(settings, "latitude", "Latitude", -90, 90)
        lon = self._coordinate_float(settings, "longitude", "Longitude", -180, 180)

        timezone_name = settings.get("timezonestring") or device_config.get_config(
            "timezone", default="America/New_York"
        )
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

    def _normalise_settings(self, settings):
        defaults = {
            "displayMode": "today",
            "method": "3",
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
        ]:
            normalised[key] = "true" if _to_bool(normalised.get(key)) else "false"
        normalised["dailyRefreshTime"] = self.normalise_hhmm(
            normalised.get("dailyRefreshTime", "00:05"), "Daily refresh time"
        )
        return normalised

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
            "method": settings.get("method", "3"),
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
