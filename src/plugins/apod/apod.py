"""
APOD Plugin for InkyPi.

This plugin fetches the Astronomy Picture of the Day (APOD) from NASA's API
and displays it on the InkyPi device. It supports optional manual date selection,
random dates, and optional overlays showing the APOD image date and title.

For the API key, set `NASA_SECRET={API_KEY}` in your .env file.
"""

from datetime import datetime, timedelta
import logging
from random import randint

from PIL import ImageDraw, ImageFont

from plugins.base_plugin.base_plugin import BasePlugin
from utils.http_client import get_http_session

logger = logging.getLogger(__name__)


class Apod(BasePlugin):
    """NASA Astronomy Picture of the Day plugin."""

    APOD_ENDPOINT = "https://api.nasa.gov/planetary/apod"
    RANDOM_RETRY_COUNT = 3
    RANDOM_START_DATE = datetime(2015, 1, 1)
    KNOWN_GOOD_IMAGE_DATES = [
        # NASA APOD title: "The ISS Meets Venus". This is the verified
        # April 11, 2025 ISS/Venus conjunction image requested as a fallback.
        # ("2025-04-11", "The ISS Meets Venus"), # does not show correctly on E-ink display
        ("2026-06-05", "The Hydra Cluster of Galaxies"),
        ("2022-02-06", "Blue Marble Earth"),
    ]

    OVERLAY_POSITIONS = {
        "top-left",
        "top-center",
        "top-right",
        "middle-left",
        "center",
        "middle-right",
        "bottom-left",
        "bottom-center",
        "bottom-right",
    }
    DATE_POSITIONS = OVERLAY_POSITIONS
    TITLE_POSITIONS = OVERLAY_POSITIONS

    DATE_FORMATS = {
        "iso",
        "long",
        "medium",
        "slash",
        "day-month",
    }

    OVERLAY_SIZES = {"x-small", "small", "medium", "large", "x-large"}
    SIZE_FACTORS = {
        "x-small": 0.7,
        "small": 0.85,
        "medium": 1.0,
        "large": 1.2,
        "x-large": 1.4,
    }

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["api_key"] = {
            "required": True,
            "service": "NASA",
            "expected_key": "NASA_SECRET",
        }
        template_params["style_settings"] = False
        return template_params

    def generate_image(self, settings, device_config):
        logger.info("=== APOD Plugin: Starting image generation ===")

        api_key = device_config.load_env_key("NASA_SECRET")
        if not api_key:
            logger.error("NASA API Key not configured")
            raise RuntimeError("NASA API Key not configured.")

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
            logger.debug(
                "Vertical orientation detected, dimensions: %sx%s",
                dimensions[0],
                dimensions[1],
            )

        session = get_http_session()
        data, image = self._fetch_apod_image_with_retries(settings, session, api_key, dimensions)
        image = self._add_metadata_overlays(image, data, settings)

        logger.info("=== APOD Plugin: Image generation complete ===")
        return image

    def _fetch_apod_image_with_retries(self, settings, session, api_key, dimensions):
        """Fetch APOD metadata and image, retrying/falling back when needed.

        Random mode tries three random dates first. If those fail because NASA
        returns an error, a non-image/video APOD, a missing URL, or the image
        cannot be loaded, the plugin falls back to today, yesterday, and then
        known-good APOD image dates. Custom date and normal today mode also use
        the fallback list so the display does not go blank after an APOD/API
        issue.
        """
        attempts = self._build_apod_attempt_plan(settings)
        failures = []

        for attempt in attempts:
            date_value = attempt.get("date")
            label = attempt["label"]
            logger.info("Fetching APOD: %s%s", label, f" ({date_value})" if date_value else "")

            try:
                data, image = self._try_fetch_apod_image(
                    session=session,
                    api_key=api_key,
                    dimensions=dimensions,
                    date_value=date_value,
                )
                logger.info(
                    "Using APOD '%s' from %s via %s",
                    data.get("title", "Untitled"),
                    data.get("date", date_value or "today"),
                    label,
                )
                return data, image
            except Exception as exc:  # noqa: BLE001 - keep display alive after API/image failures.
                message = f"{label}{f' ({date_value})' if date_value else ''}: {exc}"
                failures.append(message)
                logger.warning("APOD attempt failed: %s", message)

        logger.error("All APOD attempts failed: %s", " | ".join(failures))
        raise RuntimeError("Failed to retrieve a usable NASA APOD image after retries and fallbacks.")

    def _build_apod_attempt_plan(self, settings):
        """Return ordered APOD attempts for current settings."""
        attempts = []
        seen_dates = set()

        def add_attempt(label, date_value=None):
            key = date_value or "__today_without_date__"
            if key in seen_dates:
                return
            seen_dates.add(key)
            attempts.append({"label": label, "date": date_value})

        if settings.get("randomizeApod") == "true":
            for index, date_value in enumerate(self._random_apod_dates(), start=1):
                add_attempt(f"random attempt {index}/{self.RANDOM_RETRY_COUNT}", date_value)
        elif settings.get("customDate"):
            add_attempt("custom date", settings["customDate"])
        else:
            add_attempt("today")

        for date_value, label in self._fallback_apod_dates():
            add_attempt(label, date_value)

        return attempts

    def _random_apod_dates(self):
        """Return unique random APOD dates to try before fallbacks."""
        start = self.RANDOM_START_DATE
        end = datetime.today()
        delta_days = max(0, (end - start).days)

        dates = []
        seen = set()
        guard = 0
        while len(dates) < self.RANDOM_RETRY_COUNT and guard < self.RANDOM_RETRY_COUNT * 20:
            guard += 1
            date_value = (start + timedelta(days=randint(0, delta_days))).strftime("%Y-%m-%d")
            if date_value in seen:
                continue
            seen.add(date_value)
            dates.append(date_value)

        return dates

    def _fallback_apod_dates(self):
        """Return today, yesterday, then verified known-good APOD image dates."""
        today = datetime.today()
        fallback_dates = [
            (today.strftime("%Y-%m-%d"), "current date fallback"),
            ((today - timedelta(days=1)).strftime("%Y-%m-%d"), "yesterday fallback"),
        ]

        # Do not request a future known-good date if the device clock is earlier
        # than that date. This keeps the fallback plan safe if reused before
        # June 5, 2026.
        today_date = today.date()
        for date_value, title in self.KNOWN_GOOD_IMAGE_DATES:
            try:
                parsed = datetime.strptime(date_value, "%Y-%m-%d").date()
            except ValueError:
                logger.warning("Skipping invalid known-good APOD fallback date: %s", date_value)
                continue
            if parsed <= today_date:
                fallback_dates.append((date_value, f"known-good fallback: {title}"))

        return fallback_dates

    def _try_fetch_apod_image(self, session, api_key, dimensions, date_value=None):
        """Fetch one APOD date and load its image or raise a useful error."""
        params = {"api_key": api_key}
        if date_value:
            params["date"] = date_value

        logger.debug("Requesting NASA APOD API with params: %s", {**params, "api_key": "***"})
        response = session.get(self.APOD_ENDPOINT, params=params)

        if response.status_code != 200:
            response_text = getattr(response, "text", "")
            raise RuntimeError(f"NASA API status {response.status_code}: {response_text[:240]}")

        data = response.json()
        logger.debug("APOD API response received: %s", data.get("title", "No title"))

        if data.get("media_type") != "image":
            raise RuntimeError(f"APOD media type is '{data.get('media_type')}', not 'image'")

        image_url = data.get("hdurl") or data.get("url")
        if not image_url:
            raise RuntimeError("APOD response did not include an image URL")

        logger.info("APOD image URL: %s", image_url)
        logger.debug("Using %s", "HD URL" if data.get("hdurl") else "standard URL")

        # Use adaptive image loader for memory-efficient processing.
        image = self.image_loader.from_url(image_url, dimensions, timeout_ms=40000)
        if not image:
            raise RuntimeError("Failed to load APOD image")

        return data, image

    def _add_metadata_overlays(self, image, data, settings):
        """Return image with enabled APOD metadata overlays."""
        title_text = ""
        date_text = ""
        title_position = self._normalize_title_position(settings.get("apodTitlePosition", "bottom-left"))
        date_position = self._normalize_date_position(settings.get("apodDatePosition", "bottom-right"))
        title_size = self._normalize_overlay_size(settings.get("apodTitleSize", "x-small"))
        date_size = self._normalize_overlay_size(settings.get("apodDateSize", "x-small"))

        if settings.get("showApodTitle", "false") == "true":
            title_text = self._normalize_apod_title(data.get("title"))

        if settings.get("showApodDate", "true") != "false":
            date_text = self._format_apod_date(data.get("date"), settings.get("apodDateFormat", "long"))

        if not title_text and not date_text:
            return image

        original_mode = image.mode
        canvas = image.convert("RGBA")
        draw = ImageDraw.Draw(canvas, "RGBA")
        occupied_boxes = []

        combine_same_line = (
            settings.get("combineTitleDateSingleLine", "false") == "true"
            and title_text
            and date_text
            and title_position == date_position
        )

        if combine_same_line:
            occupied_boxes.append(
                self._draw_combined_overlay_label(
                    draw,
                    canvas.size,
                    title_text,
                    date_text,
                    title_position,
                    title_size,
                    date_size,
                    occupied_boxes,
                )
            )
        else:
            overlays = []
            if title_text:
                overlays.append(
                    {
                        "text": title_text,
                        "position": title_position,
                        "kind": "title",
                        "size": title_size,
                    }
                )
            if date_text:
                overlays.append(
                    {
                        "text": date_text,
                        "position": date_position,
                        "kind": "date",
                        "size": date_size,
                    }
                )

            for overlay in overlays:
                occupied_boxes.append(
                    self._draw_overlay_label(
                        draw,
                        canvas.size,
                        overlay["text"],
                        overlay["position"],
                        overlay["kind"],
                        occupied_boxes,
                        overlay.get("size", "medium"),
                    )
                )

        if original_mode in ("1", "L", "RGB"):
            return canvas.convert(original_mode)
        return canvas

    def _draw_overlay_label(self, draw, canvas_size, text, position, kind, occupied_boxes, size_key="medium"):
        """Draw one translucent metadata label and return its bounding box."""
        width, height = canvas_size
        min_dim = min(width, height)

        if kind == "title":
            base_font_size = max(15, min(46, int(min_dim / 16)))
            max_width = int(width * 0.74)
            max_lines = 2
        else:
            base_font_size = max(14, min(42, int(min_dim / 18)))
            max_width = int(width * 0.58)
            max_lines = 1

        font_size = self._scale_font_size(base_font_size, size_key)

        font = self._load_overlay_font(font_size)
        lines = self._wrap_overlay_text(draw, text, font, max_width, max_lines)
        if not lines:
            return (0, 0, 0, 0)

        line_spacing = max(2, int(font_size * 0.18))
        line_metrics = []
        text_width = 0
        text_height = 0
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            line_width = bbox[2] - bbox[0]
            line_height = bbox[3] - bbox[1]
            line_metrics.append((line, bbox, line_width, line_height))
            text_width = max(text_width, line_width)
            text_height += line_height
        text_height += line_spacing * max(0, len(lines) - 1)

        margin = max(8, int(min_dim * 0.035))
        pad_x = max(8, int(font_size * 0.55))
        pad_y = max(5, int(font_size * 0.35))
        label_width = text_width + (pad_x * 2)
        label_height = text_height + (pad_y * 2)

        x, y = self._position_box(position, width, height, label_width, label_height, margin)
        x, y = self._avoid_overlay_collisions(
            x,
            y,
            label_width,
            label_height,
            position,
            width,
            height,
            margin,
            occupied_boxes,
        )
        radius = max(4, int(label_height * 0.22))

        draw.rounded_rectangle(
            [x, y, x + label_width, y + label_height],
            radius=radius,
            fill=(0, 0, 0, 172),
            outline=(255, 255, 255, 130),
            width=max(1, int(font_size / 14)),
        )

        cursor_y = y + pad_y
        for line, bbox, line_width, line_height in line_metrics:
            if position.endswith("left"):
                text_x = x + pad_x
            elif position.endswith("right"):
                text_x = x + label_width - pad_x - line_width
            else:
                text_x = x + (label_width - line_width) // 2

            draw.text(
                (text_x, cursor_y - bbox[1]),
                line,
                font=font,
                fill=(255, 255, 255, 255),
            )
            cursor_y += line_height + line_spacing

        return (x, y, x + label_width, y + label_height)

    def _draw_combined_overlay_label(
        self,
        draw,
        canvas_size,
        title_text,
        date_text,
        position,
        title_size_key,
        date_size_key,
        occupied_boxes,
    ):
        """Draw a single-line title + date label when both share one location."""
        width, height = canvas_size
        min_dim = min(width, height)
        margin = max(8, int(min_dim * 0.035))

        title_font = self._load_overlay_font(
            self._scale_font_size(max(15, min(46, int(min_dim / 16))), title_size_key)
        )
        date_font = self._load_overlay_font(
            self._scale_font_size(max(14, min(42, int(min_dim / 18))), date_size_key)
        )

        separator = "  —  "
        max_width = int(width * 0.84)

        date_text = self._normalize_apod_title(date_text)
        title_text = self._normalize_apod_title(title_text)
        if not title_text and not date_text:
            return (0, 0, 0, 0)
        if not title_text:
            return self._draw_overlay_label(draw, canvas_size, date_text, position, "date", occupied_boxes, date_size_key)
        if not date_text:
            return self._draw_overlay_label(draw, canvas_size, title_text, position, "title", occupied_boxes, title_size_key)

        date_width = self._text_width(draw, date_text, date_font)
        sep_width = self._text_width(draw, separator, title_font)
        available_title_width = max(40, max_width - date_width - sep_width)
        title_text = self._truncate_to_width(draw, title_text, title_font, available_title_width)

        title_bbox = draw.textbbox((0, 0), title_text, font=title_font)
        sep_bbox = draw.textbbox((0, 0), separator, font=title_font)
        date_bbox = draw.textbbox((0, 0), date_text, font=date_font)

        title_width = title_bbox[2] - title_bbox[0]
        sep_width = sep_bbox[2] - sep_bbox[0]
        date_width = date_bbox[2] - date_bbox[0]
        text_width = title_width + sep_width + date_width
        text_height = max(title_bbox[3] - title_bbox[1], date_bbox[3] - date_bbox[1])

        pad_x = max(8, int(max(title_font.size, date_font.size) * 0.55))
        pad_y = max(5, int(max(title_font.size, date_font.size) * 0.35))
        label_width = text_width + (pad_x * 2)
        label_height = text_height + (pad_y * 2)

        x, y = self._position_box(position, width, height, label_width, label_height, margin)
        x, y = self._avoid_overlay_collisions(
            x, y, label_width, label_height, position, width, height, margin, occupied_boxes
        )
        radius = max(4, int(label_height * 0.22))

        draw.rounded_rectangle(
            [x, y, x + label_width, y + label_height],
            radius=radius,
            fill=(0, 0, 0, 172),
            outline=(255, 255, 255, 130),
            width=max(1, int(max(title_font.size, date_font.size) / 14)),
        )

        if position.endswith("left"):
            cursor_x = x + pad_x
        elif position.endswith("right"):
            cursor_x = x + label_width - pad_x - text_width
        else:
            cursor_x = x + (label_width - text_width) // 2

        title_y = y + pad_y - title_bbox[1] + (text_height - (title_bbox[3] - title_bbox[1])) // 2
        date_y = y + pad_y - date_bbox[1] + (text_height - (date_bbox[3] - date_bbox[1])) // 2

        draw.text((cursor_x, title_y), title_text, font=title_font, fill=(255, 255, 255, 255))
        cursor_x += title_width
        draw.text((cursor_x, title_y), separator, font=title_font, fill=(255, 255, 255, 220))
        cursor_x += sep_width
        draw.text((cursor_x, date_y), date_text, font=date_font, fill=(255, 255, 255, 255))

        return (x, y, x + label_width, y + label_height)

    def _normalize_apod_title(self, value):
        """Return a safe single-line title string from NASA APOD metadata."""
        if not value:
            return ""
        return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())

    def _wrap_overlay_text(self, draw, text, font, max_width, max_lines):
        """Wrap overlay text to fit the selected display width."""
        text = self._normalize_apod_title(text)
        if not text:
            return []

        words = text.split(" ")
        lines = []
        current = ""
        used_words = 0

        for index, word in enumerate(words):
            candidate = word if not current else f"{current} {word}"
            if self._text_width(draw, candidate, font) <= max_width:
                current = candidate
                used_words = index + 1
                continue

            if current:
                lines.append(current)
                current = word
            else:
                lines.append(self._truncate_to_width(draw, word, font, max_width))
                current = ""
                used_words = index + 1

            if len(lines) == max_lines:
                break

        if current and len(lines) < max_lines:
            lines.append(current)

        if used_words < len(words) and lines:
            lines[-1] = self._truncate_to_width(draw, lines[-1] + "…", font, max_width)

        return lines[:max_lines]

    def _truncate_to_width(self, draw, text, font, max_width):
        ellipsis = "…"
        if self._text_width(draw, text, font) <= max_width:
            return text
        while text and self._text_width(draw, text + ellipsis, font) > max_width:
            text = text[:-1].rstrip()
        return (text + ellipsis) if text else ellipsis

    def _text_width(self, draw, text, font):
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]

    def _avoid_overlay_collisions(
        self,
        x,
        y,
        box_width,
        box_height,
        position,
        canvas_width,
        canvas_height,
        margin,
        occupied_boxes,
    ):
        """Nudge labels if the user places date and title in the same area."""
        if not occupied_boxes:
            return x, y

        step = box_height + max(4, margin // 2)
        for _ in range(12):
            candidate = (x, y, x + box_width, y + box_height)
            if not any(self._boxes_overlap(candidate, box) for box in occupied_boxes):
                return x, y

            if position.startswith("bottom"):
                y -= step
            elif position.startswith("top"):
                y += step
            else:
                y += step if y + box_height + step <= canvas_height - margin else -step

            y = max(margin, min(y, canvas_height - box_height - margin))

        return x, y

    def _boxes_overlap(self, first, second):
        if second == (0, 0, 0, 0):
            return False
        return not (
            first[2] <= second[0]
            or first[0] >= second[2]
            or first[3] <= second[1]
            or first[1] >= second[3]
        )

    def _normalize_date_position(self, value):
        position = (value or "bottom-right").strip().lower()
        if position not in self.DATE_POSITIONS:
            logger.warning("Invalid APOD date position '%s'; using bottom-right", value)
            return "bottom-right"
        return position

    def _normalize_title_position(self, value):
        position = (value or "bottom-left").strip().lower()
        if position not in self.TITLE_POSITIONS:
            logger.warning("Invalid APOD title position '%s'; using bottom-left", value)
            return "bottom-left"
        return position

    def _normalize_overlay_size(self, value):
        size_key = (value or "medium").strip().lower()
        if size_key not in self.OVERLAY_SIZES:
            logger.warning("Invalid APOD overlay size '%s'; using medium", value)
            return "medium"
        return size_key

    def _scale_font_size(self, base_size, size_key):
        factor = self.SIZE_FACTORS.get(self._normalize_overlay_size(size_key), 1.0)
        return max(10, int(round(base_size * factor)))

    def _format_apod_date(self, value, date_format):
        if not value:
            return ""

        try:
            date_obj = datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            logger.warning("Could not parse APOD date '%s'; rendering raw value", value)
            return str(value)

        fmt = (date_format or "long").strip().lower()
        if fmt not in self.DATE_FORMATS:
            fmt = "long"

        if fmt == "iso":
            return date_obj.strftime("%Y-%m-%d")
        if fmt == "medium":
            return f"{date_obj.strftime('%b')} {date_obj.day}, {date_obj.year}"
        if fmt == "slash":
            return f"{date_obj.month}/{date_obj.day}/{date_obj.year}"
        if fmt == "day-month":
            return f"{date_obj.day} {date_obj.strftime('%b')} {date_obj.year}"
        return f"{date_obj.strftime('%B')} {date_obj.day}, {date_obj.year}"

    def _position_box(self, position, width, height, box_width, box_height, margin):
        if position.endswith("left"):
            x = margin
        elif position.endswith("right"):
            x = width - box_width - margin
        else:
            x = (width - box_width) // 2

        if position.startswith("top"):
            y = margin
        elif position.startswith("bottom"):
            y = height - box_height - margin
        else:
            y = (height - box_height) // 2

        return max(margin, x), max(margin, y)

    def _load_overlay_font(self, size):
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]
        for path in candidates:
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
        return ImageFont.load_default()
