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

        params = {"api_key": api_key}

        # Determine date to fetch.
        if settings.get("randomizeApod") == "true":
            start = datetime(2015, 1, 1)
            end = datetime.today()
            delta_days = (end - start).days
            random_date = start + timedelta(days=randint(0, delta_days))
            params["date"] = random_date.strftime("%Y-%m-%d")
            logger.info("Fetching random APOD from date: %s", params["date"])
        elif settings.get("customDate"):
            params["date"] = settings["customDate"]
            logger.info("Fetching APOD from custom date: %s", params["date"])
        else:
            logger.info("Fetching today's APOD")

        logger.debug("Requesting NASA APOD API...")
        session = get_http_session()
        response = session.get("https://api.nasa.gov/planetary/apod", params=params)

        if response.status_code != 200:
            logger.error("NASA API error (status %s): %s", response.status_code, response.text)
            raise RuntimeError("Failed to retrieve NASA APOD.")

        data = response.json()
        logger.debug("APOD API response received: %s", data.get("title", "No title"))

        if data.get("media_type") != "image":
            logger.warning("APOD media type is '%s', not 'image'", data.get("media_type"))
            raise RuntimeError("APOD is not an image today.")

        image_url = data.get("hdurl") or data.get("url")
        if not image_url:
            raise RuntimeError("APOD response did not include an image URL.")

        logger.info("APOD image URL: %s", image_url)
        logger.debug("Using %s", "HD URL" if data.get("hdurl") else "standard URL")

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
            logger.debug(
                "Vertical orientation detected, dimensions: %sx%s",
                dimensions[0],
                dimensions[1],
            )

        # Use adaptive image loader for memory-efficient processing.
        image = self.image_loader.from_url(image_url, dimensions, timeout_ms=40000)
        if not image:
            logger.error("Failed to load APOD image")
            raise RuntimeError("Failed to load APOD image.")

        image = self._add_metadata_overlays(image, data, settings)

        logger.info("=== APOD Plugin: Image generation complete ===")
        return image

    def _add_metadata_overlays(self, image, data, settings):
        """Return image with enabled APOD metadata overlays."""
        overlays = []

        title_text = self._normalize_apod_title(data.get("title"))
        if settings.get("showApodTitle", "false") == "true" and title_text:
            overlays.append(
                {
                    "text": title_text,
                    "position": self._normalize_title_position(
                        settings.get("apodTitlePosition", "bottom-left")
                    ),
                    "kind": "title",
                }
            )

        if settings.get("showApodDate", "true") != "false":
            date_text = self._format_apod_date(data.get("date"), settings.get("apodDateFormat", "long"))
            if date_text:
                overlays.append(
                    {
                        "text": date_text,
                        "position": self._normalize_date_position(
                            settings.get("apodDatePosition", "bottom-right")
                        ),
                        "kind": "date",
                    }
                )

        if not overlays:
            return image

        original_mode = image.mode
        canvas = image.convert("RGBA")
        draw = ImageDraw.Draw(canvas, "RGBA")
        occupied_boxes = []

        for overlay in overlays:
            occupied_boxes.append(
                self._draw_overlay_label(
                    draw,
                    canvas.size,
                    overlay["text"],
                    overlay["position"],
                    overlay["kind"],
                    occupied_boxes,
                )
            )

        if original_mode in ("1", "L", "RGB"):
            return canvas.convert(original_mode)
        return canvas

    def _draw_overlay_label(self, draw, canvas_size, text, position, kind, occupied_boxes):
        """Draw one translucent metadata label and return its bounding box."""
        width, height = canvas_size
        min_dim = min(width, height)

        if kind == "title":
            font_size = max(15, min(46, int(min_dim / 16)))
            max_width = int(width * 0.74)
            max_lines = 2
        else:
            font_size = max(14, min(42, int(min_dim / 18)))
            max_width = int(width * 0.58)
            max_lines = 1

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
