"""Reading text from pictures with the Tesseract program (used through its command line).

Optional: without the `tesseract` program the engine reports `available == False`
and the bot simply does not read pictures. Results are noisy on stylised sticker
fonts, so callers must treat the text as a hint, never as proof.
"""

import asyncio
import io
import logging
import re
import shutil

log = logging.getLogger(__name__)

MAX_SIDE = 1024
MIN_SIDE = 400  # small pictures are enlarged: Tesseract reads tiny text badly
MIN_LETTERS = 4  # fewer letters than this is noise, not text


class OcrEngine:
    def __init__(self, languages: str = "rus+eng", timeout: float = 12.0):
        self.path = shutil.which("tesseract")
        self.languages = languages
        self.timeout = timeout
        self._slot = asyncio.Semaphore(1)  # one reading at a time: the server is small

    @property
    def available(self) -> bool:
        return self.path is not None

    @staticmethod
    def _prepare(image_bytes: bytes) -> bytes | None:
        try:
            from PIL import Image

            with Image.open(io.BytesIO(image_bytes)) as img:
                rgba = img.convert("RGBA")
                alpha = rgba.split()[3]
                # Transparent stickers go on mid-grey: both white and black lettering stay readable.
                background = (128, 128, 128) if alpha.getextrema()[0] < 250 else (255, 255, 255)
                flat = Image.new("RGB", rgba.size, background)
                flat.paste(rgba, mask=alpha)
            longest = max(flat.size)
            if longest > MAX_SIDE:
                scale = MAX_SIDE / longest
            elif longest < MIN_SIDE:
                scale = MIN_SIDE / longest
            else:
                scale = 1.0
            if scale != 1.0:
                flat = flat.resize((max(1, int(flat.width * scale)), max(1, int(flat.height * scale))), Image.LANCZOS)
            out = io.BytesIO()
            flat.convert("L").save(out, "PNG")
            return out.getvalue()
        except Exception:
            return None

    async def read(self, image_bytes: bytes) -> str:
        """The text found on the picture, cleaned up; an empty string when there is none or reading failed."""
        if not self.available:
            return ""
        prepared = await asyncio.get_running_loop().run_in_executor(None, self._prepare, image_bytes)
        if prepared is None:
            return ""
        async with self._slot:
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.path, "stdin", "stdout", "-l", self.languages, "--psm", "6",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(proc.communicate(prepared), self.timeout)
            except (asyncio.TimeoutError, OSError) as e:
                log.warning("OCR failed: %s", e)
                return ""
        return clean(out.decode("utf-8", "ignore"))


def clean(text: str) -> str:
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.replace("\r", "").splitlines())
    text = "\n".join(line for line in lines if line)
    return text if sum(ch.isalpha() for ch in text) >= MIN_LETTERS else ""
