"""Fingerprints of profile pictures.

Spam accounts reuse the same few pictures, so a small perceptual hash (dHash,
64 bits) is enough to recognise a picture again after resizing or recompression.
Only the hash is stored, never the picture.
"""

import io

MAX_DISTANCE = 6  # bits that may differ for two pictures to count as the same


def dhash(image_bytes: bytes) -> str | None:
    """16 hex characters describing the picture, or None when it cannot be read."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            small = img.convert("L").resize((9, 8), Image.LANCZOS)
            pixels = list(small.tobytes())  # 72 grey values (mode L)
    except Exception:  # damaged file, unsupported format, Pillow missing: no fingerprint, no harm
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return f"{bits:016x}"


def distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def similar(a: str, b: str, max_distance: int = MAX_DISTANCE) -> bool:
    return distance(a, b) <= max_distance
