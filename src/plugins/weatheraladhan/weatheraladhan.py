"""
Weather + AlAdhan plugin for InkyPi.

This combined dashboard shows a simple current-weather/forecast summary from
Open-Meteo and daily prayer times / Hijri date from AlAdhan.
"""

from datetime import datetime, timedelta, timezone
import logging
import os
import re
from urllib.parse import quote

import requests
from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python < 3.9 fallback if ever needed.
    ZoneInfo = None


METHODS = [
    {"id": 2, "name": "Islamic Society of North America (ISNA)"},
    {"id": 3, "name": "Muslim World League"},
    {"id": 4, "name": "Umm Al-Qura University, Makkah"},
    {"id": 5, "name": "Egyptian General Authority of Survey"},
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

PRAYER_KEYS = ["Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha"]
NEXT_PRAYER_KEYS = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]

WEATHER_CODE_DESCRIPTIONS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Rain showers",
    81: "Rain showers",
    82: "Heavy showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm hail",
    99: "Thunderstorm hail",
}


def _to_bool(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _is_valid_timezone(timezone_name):
    if not timezone_name or ZoneInfo is None:
        return False
    try:
        ZoneInfo(str(timezone_name))
        return True
    except Exception:
        return False


class WeatherAladhan(BasePlugin):
    """Combined weather-provider and AlAdhan prayer-time dashboard."""

    OPEN_METEO_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
    OPEN_WEATHER_MAP_ENDPOINT = "https://api.openweathermap.org/data/3.0/onecall"
    ALADHAN_TIMINGS_ENDPOINT = "https://api.aladhan.com/v1/timings/{date}"

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = True
        template_params["api_key"] = {
            "required": False,
            "service": "OpenWeatherMap",
            "expected_key": "OPEN_WEATHER_MAP_SECRET",
        }
        template_params["methods"] = METHODS
        template_params["device_timezone"] = self.get_system_timezone(default="Etc/UTC")
        return template_params

    def generate_image(self, settings, device_config):
        settings = self._normalize_settings(settings)
        lat = self._coordinate_float(settings, "latitude", "Latitude", -90, 90)
        lon = self._coordinate_float(settings, "longitude", "Longitude", -180, 180)
        timezone_name = self.resolve_timezone(settings, device_config)
        if ZoneInfo is None:
            raise RuntimeError("Python zoneinfo is unavailable; cannot resolve timezone.")
        tz = ZoneInfo(timezone_name)
        now = datetime.now(tz)
        time_format = device_config.get_config("time_format", default=settings.get("timeFormat", "12h"))
        if settings.get("timeFormat") in {"12h", "24h"}:
            time_format = settings.get("timeFormat")

        try:
            weather_data = self.get_weather_data(lat, lon, timezone_name, settings, device_config)
            prayer_data = self.get_prayer_data(now, lat, lon, timezone_name, settings)
        except RuntimeError:
            raise
        except Exception as exc:
            logger.exception("Weather + Prayer API request failed")
            raise RuntimeError(f"Weather + Prayer request failed: {exc}")

        weather = self.parse_weather(weather_data, settings, timezone_name)
        prayers = self.parse_prayers(prayer_data, now, time_format, settings)

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        template_params = {
            "title": settings.get("customTitle") or "Weather + Prayer Times",
            "location_label": settings.get("locationName") or "",
            "weather": weather,
            "prayers": prayers,
            "weather_provider_label": weather.get("provider_label", ""),
            "show_weather_forecast": _to_bool(settings.get("showWeatherForecast"), True),
            "show_weather_details": _to_bool(settings.get("showWeatherDetails"), True),
            "show_next_prayer": _to_bool(settings.get("showNextPrayer"), True),
            "show_hijri": _to_bool(settings.get("showHijri"), True),
            "method_name": prayer_data.get("data", {}).get("meta", {}).get("method", {}).get("name", ""),
            "last_refresh_time": self.format_dt(now, time_format),
            "daily_refresh_label": self.format_hhmm(settings.get("dailyRefreshTime", "00:05"), time_format),
            "timezone_name": timezone_name,
            "plugin_settings": settings,
        }

        image = self.render_image(
            dimensions,
            "weatheraladhan.html",
            "weatheraladhan.css",
            template_params,
        )
        if not image:
            raise RuntimeError("Failed to render Weather + Prayer Times image. Please check logs.")
        return image

    def _normalize_settings(self, settings):
        defaults = {
            "locationName": "",
            "customTitle": "Weather + Prayer Times",
            "latitude": "",
            "longitude": "",
            "timezonestring": "",
            "dailyRefreshTime": "00:05",
            "weatherProvider": "OpenMeteo",
            "units": "imperial",
            "forecastDays": "3",
            "method": "2",
            "school": "0",
            "latitudeAdjustmentMethod": "3",
            "midnightMode": "0",
            "shafaq": "general",
            "adjustment": "0",
            "methodSettings": "",
            "tune": "",
            "timeFormat": "12h",
            "showWeatherForecast": "true",
            "showWeatherDetails": "false",
            "showNextPrayer": "true",
            "showHijri": "true",
        }
        merged = dict(defaults)
        merged.update(settings or {})
        for key in ["showWeatherForecast", "showWeatherDetails", "showNextPrayer", "showHijri"]:
            merged[key] = "true" if _to_bool(merged.get(key), defaults[key] == "true") else "false"
        merged["dailyRefreshTime"] = self.normalize_hhmm(merged.get("dailyRefreshTime"), "Daily refresh time")
        if merged.get("weatherProvider") not in {"OpenMeteo", "OpenWeatherMap"}:
            raise RuntimeError("Weather provider must be OpenMeteo or OpenWeatherMap.")
        if merged.get("units") not in {"imperial", "metric"}:
            raise RuntimeError("Units must be imperial or metric.")
        if merged.get("timeFormat") not in {"12h", "24h"}:
            merged["timeFormat"] = "12h"
        self._validate_aladhan_settings(merged)
        return merged

    def _validate_aladhan_settings(self, settings):
        for key in ["method", "school", "latitudeAdjustmentMethod", "midnightMode", "adjustment"]:
            try:
                int(str(settings.get(key, "0")))
            except ValueError:
                raise RuntimeError(f"{key} must be a number.")
        if settings.get("method") == "99" and settings.get("methodSettings"):
            parts = [part.strip() for part in settings["methodSettings"].split(",")]
            if len(parts) != 3:
                raise RuntimeError("Custom method settings must have 3 comma-separated values.")
        tune = str(settings.get("tune") or "").strip()
        if tune:
            parts = [part.strip() for part in tune.split(",")]
            if len(parts) != 9:
                raise RuntimeError("Fine tune times must contain 9 comma-separated minute values.")
            for part in parts:
                if not re.fullmatch(r"-?\d+", part):
                    raise RuntimeError("Fine tune times can only contain whole-minute offsets.")

    def _required_float(self, settings, key, label):
        try:
            return float(settings.get(key))
        except (TypeError, ValueError):
            raise RuntimeError(f"{label} is required and must be a number.")

    def _coordinate_float(self, settings, key, label, minimum, maximum):
        value = self._required_float(settings, key, label)
        if not minimum <= value <= maximum:
            raise RuntimeError(f"{label} must be between {minimum} and {maximum}.")
        return value

    def resolve_timezone(self, settings, device_config):
        explicit = str(settings.get("timezonestring") or "").strip()
        if explicit:
            if _is_valid_timezone(explicit):
                return explicit
            raise RuntimeError(f"Invalid timezone: {explicit}")
        device_timezone = self.get_device_timezone(device_config)
        if _is_valid_timezone(device_timezone):
            return device_timezone
        return self.get_system_timezone(default="Etc/UTC")

    def get_device_timezone(self, device_config):
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
        tz_env = os.environ.get("TZ")
        if tz_env and not tz_env.startswith(":") and _is_valid_timezone(tz_env):
            return tz_env
        try:
            if os.path.exists("/etc/timezone"):
                value = open("/etc/timezone", "r", encoding="utf-8").read().strip()
                if _is_valid_timezone(value):
                    return value
        except Exception:
            pass
        try:
            if os.path.islink("/etc/localtime"):
                target = os.path.realpath("/etc/localtime")
                marker = "/zoneinfo/"
                if marker in target:
                    value = target.split(marker, 1)[1]
                    if _is_valid_timezone(value):
                        return value
        except Exception:
            pass
        return default

    def get_weather_data(self, lat, lon, timezone_name, settings, device_config):
        provider = settings.get("weatherProvider", "OpenMeteo")
        if provider == "OpenWeatherMap":
            return self.get_openweathermap_data(lat, lon, settings, device_config)
        return self.get_openmeteo_data(lat, lon, timezone_name, settings)

    def get_openmeteo_data(self, lat, lon, timezone_name, settings):
        units = settings.get("units", "imperial")
        forecast_days = int(settings.get("forecastDays", "3"))
        forecast_days = min(max(forecast_days, 1), 7)
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_direction_10m,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": timezone_name,
            "forecast_days": forecast_days,
        }
        if units == "imperial":
            params.update({
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "precipitation_unit": "inch",
            })
        else:
            params.update({
                "temperature_unit": "celsius",
                "wind_speed_unit": "kmh",
                "precipitation_unit": "mm",
            })
        response = requests.get(self.OPEN_METEO_ENDPOINT, params=params, timeout=30)
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Open-Meteo request failed with status {response.status_code}.")
        return {"provider": "OpenMeteo", "payload": response.json()}

    def get_openweathermap_data(self, lat, lon, settings, device_config):
        api_key = self.load_openweather_api_key(device_config)
        if not api_key:
            raise RuntimeError("OpenWeatherMap selected, but OPEN_WEATHER_MAP_SECRET is not configured.")
        params = {
            "lat": lat,
            "lon": lon,
            "appid": api_key,
            "units": settings.get("units", "imperial"),
            "exclude": "minutely,hourly,alerts",
        }
        response = requests.get(self.OPEN_WEATHER_MAP_ENDPOINT, params=params, timeout=30)
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"OpenWeatherMap request failed with status {response.status_code}.")
        return {"provider": "OpenWeatherMap", "payload": response.json()}

    def load_openweather_api_key(self, device_config):
        if not device_config:
            return os.environ.get("OPEN_WEATHER_MAP_SECRET")
        try:
            return device_config.load_env_key("OPEN_WEATHER_MAP_SECRET")
        except Exception:
            return os.environ.get("OPEN_WEATHER_MAP_SECRET")

    def get_prayer_data(self, now, lat, lon, timezone_name, settings):
        date_path = now.strftime("%d-%m-%Y")
        params = {
            "latitude": lat,
            "longitude": lon,
            "method": settings.get("method", "2"),
            "school": settings.get("school", "0"),
            "midnightMode": settings.get("midnightMode", "0"),
            "latitudeAdjustmentMethod": settings.get("latitudeAdjustmentMethod", "3"),
            "adjustment": settings.get("adjustment", "0"),
            "timezonestring": timezone_name,
        }
        if settings.get("shafaq"):
            params["shafaq"] = settings.get("shafaq")
        if settings.get("method") == "99" and settings.get("methodSettings"):
            params["methodSettings"] = settings.get("methodSettings")
        if settings.get("tune"):
            params["tune"] = settings.get("tune")
        response = requests.get(self.ALADHAN_TIMINGS_ENDPOINT.format(date=date_path), params=params, timeout=30)
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"AlAdhan request failed with status {response.status_code}.")
        payload = response.json()
        if payload.get("code") not in (200, "200"):
            raise RuntimeError(payload.get("status") or "AlAdhan returned an error.")
        return payload

    def parse_weather(self, weather_data, settings, timezone_name):
        provider = weather_data.get("provider", "OpenMeteo")
        payload = weather_data.get("payload", {})
        if provider == "OpenWeatherMap":
            return self.parse_openweathermap_weather(payload, settings, timezone_name)
        return self.parse_openmeteo_weather(payload, settings)

    def parse_openmeteo_weather(self, weather_data, settings):
        current = weather_data.get("current", {})
        daily = weather_data.get("daily", {})
        units = settings.get("units", "imperial")
        temperature_unit = "°F" if units == "imperial" else "°C"
        wind_unit = "mph" if units == "imperial" else "km/h"
        rain_unit = "in" if units == "imperial" else "mm"
        code = int(current.get("weather_code", 0) or 0)
        forecast = []
        max_days = min(int(settings.get("forecastDays", "3")), 7)
        for i, day in enumerate(daily.get("time", [])[:max_days]):
            try:
                dt = datetime.fromisoformat(day)
                day_label = dt.strftime("%a")
            except Exception:
                day_label = day
            daily_code = int((daily.get("weather_code") or [0])[i] or 0) if i < len(daily.get("weather_code", [])) else 0
            forecast.append({
                "day": day_label,
                "high": self._round(daily.get("temperature_2m_max", [])[i] if i < len(daily.get("temperature_2m_max", [])) else None),
                "low": self._round(daily.get("temperature_2m_min", [])[i] if i < len(daily.get("temperature_2m_min", [])) else None),
                "precip": self._round(daily.get("precipitation_probability_max", [])[i] if i < len(daily.get("precipitation_probability_max", [])) else None),
                "summary": WEATHER_CODE_DESCRIPTIONS.get(daily_code, "Weather"),
                "icon": self.weather_icon_from_code(daily_code),
            })
        return {
            "provider_label": "Open-Meteo",
            "today_label": self._format_weather_today_label((daily.get("time") or [None])[0]),
            "temperature": self._round(current.get("temperature_2m")),
            "feels_like": self._round(current.get("apparent_temperature")),
            "humidity": self._round(current.get("relative_humidity_2m")),
            "precipitation": self._format_precip(current.get("precipitation", 0)),
            "wind_speed": self._round(current.get("wind_speed_10m")),
            "wind_direction": self.get_wind_arrow(current.get("wind_direction_10m", 0)),
            "summary": WEATHER_CODE_DESCRIPTIONS.get(code, "Weather"),
            "icon": self.weather_icon_from_code(code),
            "temperature_unit": temperature_unit,
            "wind_unit": wind_unit,
            "rain_unit": rain_unit,
            "forecast": forecast,
        }

    def parse_openweathermap_weather(self, weather_data, settings, timezone_name):
        current = weather_data.get("current", {})
        daily = weather_data.get("daily", [])
        units = settings.get("units", "imperial")
        temperature_unit = "°F" if units == "imperial" else "°C"
        wind_unit = "mph" if units == "imperial" else "m/s"
        rain_unit = "in" if units == "imperial" else "mm"
        description = self._weather_description(current)
        forecast = []
        max_days = min(int(settings.get("forecastDays", "3")), 7)
        tz = ZoneInfo(timezone_name) if ZoneInfo and _is_valid_timezone(timezone_name) else timezone.utc
        for day in daily[:max_days]:
            try:
                dt = datetime.fromtimestamp(day.get("dt", 0), tz=timezone.utc).astimezone(tz)
                day_label = dt.strftime("%a")
            except Exception:
                day_label = "Day"
            pop = day.get("pop", 0)
            forecast.append({
                "day": day_label,
                "high": self._round((day.get("temp") or {}).get("max")),
                "low": self._round((day.get("temp") or {}).get("min")),
                "precip": self._round(float(pop or 0) * 100),
                "summary": self._weather_description(day),
                "icon": self.weather_icon_from_openweather(day),
            })
        precipitation = self._openweather_precip(current, units)
        return {
            "provider_label": "OpenWeatherMap",
            "today_label": self._format_weather_today_label_from_timestamp((daily[0] or {}).get("dt") if daily else None, timezone_name),
            "temperature": self._round(current.get("temp")),
            "feels_like": self._round(current.get("feels_like")),
            "humidity": self._round(current.get("humidity")),
            "precipitation": self._format_precip(precipitation),
            "wind_speed": self._round(current.get("wind_speed")),
            "wind_direction": self.get_wind_arrow(current.get("wind_deg", 0)),
            "summary": description,
            "icon": self.weather_icon_from_openweather(current),
            "temperature_unit": temperature_unit,
            "wind_unit": wind_unit,
            "rain_unit": rain_unit,
            "forecast": forecast,
        }

    def _weather_description(self, block):
        try:
            value = (block.get("weather") or [{}])[0].get("description") or (block.get("weather") or [{}])[0].get("main")
            return str(value).title() if value else "Weather"
        except Exception:
            return "Weather"

    def _openweather_precip(self, current, units):
        value = 0.0
        for key in ("rain", "snow"):
            item = current.get(key) or {}
            value += float(item.get("1h") or item.get("3h") or 0)
        if units == "imperial":
            return value / 25.4
        return value

    def _format_precip(self, value):
        try:
            value = float(value)
        except Exception:
            return "0"
        if value == 0:
            return "0"
        if value < 1:
            return f"{value:.2f}".rstrip("0").rstrip(".")
        return str(int(round(value)))

    def weather_icon_from_code(self, code):
        try:
            code = int(code)
        except Exception:
            return "☼"
        if code in {0, 1}:
            return "☼"
        if code in {2, 3}:
            return "☁"
        if code in {45, 48}:
            return "≋"
        if code in {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}:
            return "☂"
        if code in {71, 73, 75, 77, 85, 86}:
            return "✻"
        if code in {95, 96, 99}:
            return "ϟ"
        return "☼"

    def weather_icon_from_openweather(self, block):
        try:
            weather_id = int((block.get("weather") or [{}])[0].get("id", 800))
        except Exception:
            return "☼"
        if 200 <= weather_id < 300:
            return "ϟ"
        if 300 <= weather_id < 600:
            return "☂"
        if 600 <= weather_id < 700:
            return "✻"
        if 700 <= weather_id < 800:
            return "≋"
        if weather_id == 800:
            return "☼"
        return "☁"

    def _format_weather_today_label(self, value):
        try:
            if value:
                return datetime.fromisoformat(str(value)).strftime("%A, %b %d")
        except Exception:
            pass
        return datetime.today().strftime("%A, %b %d")

    def _format_weather_today_label_from_timestamp(self, timestamp, timezone_name):
        try:
            if timestamp:
                tz = ZoneInfo(timezone_name) if ZoneInfo and _is_valid_timezone(timezone_name) else timezone.utc
                return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).astimezone(tz).strftime("%A, %b %d")
        except Exception:
            pass
        return datetime.today().strftime("%A, %b %d")

    def parse_prayers(self, prayer_data, now, time_format, settings):
        data = prayer_data.get("data", {})
        timings = data.get("timings", {})
        date_data = data.get("date", {})
        hijri = date_data.get("hijri", {})
        gregorian = date_data.get("gregorian", {})
        prayer_rows = []
        for key in PRAYER_KEYS:
            value = timings.get(key)
            if not value:
                continue
            prayer_rows.append({"name": key, "time": self.format_prayer_time(value, time_format)})
        current_prayer, next_prayer = self.get_prayer_status(timings, now, time_format)
        return {
            "rows": prayer_rows,
            "hijri_date": self.format_hijri(hijri),
            "gregorian_date": gregorian.get("date", now.strftime("%d-%m-%Y")),
            "current_prayer": current_prayer,
            "next_prayer": next_prayer,
        }

    def get_prayer_status(self, timings, now, time_format):
        events = []
        for key in NEXT_PRAYER_KEYS:
            raw = timings.get(key)
            if not raw:
                continue
            parsed = self.parse_timing_for_today(raw, now)
            if parsed:
                events.append((key, parsed))
        events.sort(key=lambda item: item[1])
        current = None
        next_event = None
        for index, (name, dt) in enumerate(events):
            if dt > now:
                next_event = {"name": name, "time": self.format_dt(dt, time_format)}
                current = events[index - 1][0] if index > 0 else None
                break
        if not next_event and events:
            tomorrow_first = events[0][1] + timedelta(days=1)
            next_event = {"name": events[0][0], "time": self.format_dt(tomorrow_first, time_format)}
            current = events[-1][0]
        return current, next_event

    def parse_timing_for_today(self, raw_value, now):
        clean = self.clean_timing(raw_value)
        try:
            hour, minute = [int(part) for part in clean.split(":")[:2]]
            return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except Exception:
            return None

    def clean_timing(self, raw_value):
        return str(raw_value).split(" ", 1)[0].strip()

    def format_prayer_time(self, raw_value, time_format):
        clean = self.clean_timing(raw_value)
        try:
            hour, minute = [int(part) for part in clean.split(":")[:2]]
            return self.format_dt(datetime(2000, 1, 1, hour, minute), time_format)
        except Exception:
            return clean

    def format_hijri(self, hijri):
        if not hijri:
            return ""
        day = hijri.get("day", "")
        month = (hijri.get("month") or {}).get("en", "")
        year = hijri.get("year", "")
        pieces = [str(piece) for piece in [day, month, year] if piece]
        return " ".join(pieces) + (" AH" if year else "")

    def normalize_hhmm(self, value, label):
        value = str(value or "00:05").strip()
        if not re.fullmatch(r"\d{1,2}:\d{2}", value):
            raise RuntimeError(f"{label} must use 24-hour HH:MM format, e.g. 00:05.")
        hour, minute = [int(part) for part in value.split(":", 1)]
        if hour > 23 or minute > 59:
            raise RuntimeError(f"{label} must be a valid time between 00:00 and 23:59.")
        return f"{hour:02d}:{minute:02d}"

    def format_hhmm(self, value, time_format):
        value = self.normalize_hhmm(value, "Daily refresh time")
        hour, minute = [int(part) for part in value.split(":", 1)]
        return self.format_dt(datetime(2000, 1, 1, hour, minute), time_format)

    def format_dt(self, dt, time_format):
        if time_format == "24h":
            return dt.strftime("%H:%M")
        return dt.strftime("%-I:%M %p") if os.name != "nt" else dt.strftime("%#I:%M %p")

    def _round(self, value):
        try:
            return int(round(float(value)))
        except Exception:
            return "—"

    def get_wind_arrow(self, wind_deg):
        try:
            deg = float(wind_deg) % 360
        except Exception:
            return ""
        directions = [
            ("N", 22.5), ("NE", 67.5), ("E", 112.5), ("SE", 157.5),
            ("S", 202.5), ("SW", 247.5), ("W", 292.5), ("NW", 337.5), ("N", 360.0),
        ]
        for label, upper in directions:
            if deg < upper:
                return label
        return "N"
