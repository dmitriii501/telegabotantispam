"""Pictures for Jev, which has learned to look at them (an experimental feature of System One).

The API accepts only PNG and JPEG, with both sides between 280 and 1568 px, and never resizes by itself.
Telegram hands out photos, still stickers and thumbnails of every shape, so a picture is brought into
that range here, once, before it is sent. The cost is counted in tokens like text (about 1500 for 800x500).
"""

import io
import os
from typing import cast

from core.avatar import dhash

MAX_SIDE = 1024  # text on a screenshot stays readable, and a picture stays under ~2.5k tokens
MIN_SIDE = 280  # the API's lower limit for each side
GROW_BELOW = 400  # smaller pictures are enlarged: tiny lettering is read badly
QUALITY = 85
GREY = (128, 128, 128)  # transparent stickers go on mid-grey: white and black lettering both stay visible
FORMAT = "jpg"


def vision_enabled() -> bool:
    """JEV_IMAGES=0 turns pictures off (Tesseract then reads them on the server, nothing leaves it)."""
    return os.getenv("JEV_IMAGES", "1").strip().lower() not in ("0", "false", "no", "off", "")


def prepare(image_bytes: bytes) -> bytes | None:
    """The picture as a JPEG that the API accepts, or None when the file cannot be read."""
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(image_bytes)) as source:
            img = ImageOps.exif_transpose(source) or source  # the API does not apply EXIF rotation
            rgba = img.convert("RGBA")
        alpha = rgba.split()[3]
        flat = Image.new("RGB", rgba.size, GREY if cast("tuple[int, int]", alpha.getextrema())[0] < 250 else (255, 255, 255))
        flat.paste(rgba, mask=alpha)
        longest = max(flat.size)
        scale = MAX_SIDE / longest if longest > MAX_SIDE else GROW_BELOW / longest if longest < GROW_BELOW else 1.0
        if scale != 1.0:
            flat = flat.resize((max(1, round(flat.width * scale)), max(1, round(flat.height * scale))), Image.Resampling.LANCZOS)
        if min(flat.size) < MIN_SIDE:  # a narrow strip: pad it on a neutral canvas instead of stretching it
            canvas = Image.new("RGB", (max(flat.width, MIN_SIDE), max(flat.height, MIN_SIDE)), GREY)
            canvas.paste(flat, ((canvas.width - flat.width) // 2, (canvas.height - flat.height) // 2))
            flat = canvas
        out = io.BytesIO()
        flat.save(out, "JPEG", quality=QUALITY)
        return out.getvalue()
    except Exception:  # damaged file, unsupported format, Pillow missing
        return None


def image_key(prepared: bytes) -> str | None:
    """The bot's memory key of a picture: a fingerprint, so the same picture is recognised after recompression."""
    fingerprint = dhash(prepared)
    return f"img:{fingerprint}" if fingerprint else None


def privacy_wording() -> tuple[str, str]:
    """(what is kept, where it goes) about newcomers' pictures: the texts of /privacy and the site follow the setting."""
    if vision_enabled():
        return (
            "Картинки и стикеры новичков не сохраняются; остаётся только короткий отпечаток (хеш), по которому бот узнаёт повторную картинку.",
            "Картинки и стикеры новичков (только их) отправляются в тот же ИИ-сервис для оценки; у нас они не хранятся.",
        )
    return (
        "Текст, прочитанный с картинок и стикеров новичков.",
        "Картинки никуда не отправляются: текст на них читается на нашем сервере.",
    )
