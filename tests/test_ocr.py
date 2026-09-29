"""Reading text from pictures and stickers of newcomers."""

import io
import shutil
from unittest.mock import MagicMock

import pytest
from PIL import Image, ImageDraw, ImageFont

from adapters.telegram.bot import TelegramAdapter
from core.models import ActionType, Category, Verdict
from core.moderation import MODES, decide
from core.ocr import OcrEngine, clean
from core.rules import BASE_RULE, Rule
from tests.test_handlers import CHAT, OWNER, SPAM_BOT, answer, env, msg, sent_to  # noqa: F401

# ------------------------------------------------------------------------------ engine

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")


def picture(text, bg=(255, 255, 255, 255), fg=(0, 0, 0, 255)):
    img = Image.new("RGBA", (700, 160), bg)
    ImageDraw.Draw(img).text((20, 50), text, font=ImageFont.truetype(FONT, 40), fill=fg)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


@needs_tesseract
async def test_engine_reads_plain_and_transparent_pictures():
    engine = OcrEngine()
    assert "PRIZE" in await engine.read(picture("CLAIM YOUR PRIZE"))
    assert "ЗАРАБОТОК" in await engine.read(picture("ПИШИ В ЛС ЗАРАБОТОК", bg=(0, 0, 0, 0), fg=(255, 255, 255, 255)))
    assert "ЗАРАБОТОК" in await engine.read(picture("ПИШИ В ЛС ЗАРАБОТОК", bg=(0, 0, 0, 0)))


@needs_tesseract
async def test_engine_gives_nothing_for_no_text_or_garbage():
    engine = OcrEngine()
    assert await engine.read(picture("")) == "" and await engine.read(b"not an image") == ""


async def test_engine_without_tesseract_reads_nothing(monkeypatch):
    monkeypatch.setattr("core.ocr.shutil.which", lambda name: None)
    engine = OcrEngine()
    assert not engine.available and await engine.read(picture("HELLO")) == ""


def test_clean_drops_noise_and_tidies_whitespace():
    assert clean("  ПИШИ   В ЛС \n\n\n ЗАРАБОТОК  ") == "ПИШИ В ЛС\nЗАРАБОТОК"
    assert clean("@ | ) ~") == "" and clean("CU") == ""


# --------------------------------------------------------------------------- decisions


def test_text_read_from_a_picture_never_grounds_an_automatic_ban():
    spam = Verdict(Category.SPAM, 0.99, 0.95, 0)
    assert decide(spam, [BASE_RULE], MODES["normal"]).action == ActionType.DELETE_AND_BAN
    d = decide(spam, [BASE_RULE], MODES["normal"], image_evidence=True)
    assert d.action == ActionType.DELETE_SILENT and d.reason.endswith("(по тексту на картинке)")
    owner_rule = [Rule("Рекламу нельзя — бан", "prohibition", "ban")]
    assert decide(Verdict(Category.RULE_VIOLATION, 0.99, 0.1, 0), owner_rule, image_evidence=True).action == ActionType.DELETE_SILENT


# ------------------------------------------------------------------------------ the bot


class FakeOcr:
    available = True

    def __init__(self, text="ПИШИ В ЛС ЗАРАБОТОК БЕЗ ВЛОЖЕНИЙ"):
        self.text, self.calls = text, 0

    async def read(self, data):
        self.calls += 1
        return self.text


def sticker(unique="u1", animated=False, thumbnail=True, user_id=5, i=0):
    m = msg("", user_id=user_id, message_id=8000 + i)
    m.text, m.content_type = None, "sticker"
    m.sticker = MagicMock(file_id=f"f-{unique}", file_unique_id=unique, is_animated=animated, is_video=False)
    m.sticker.thumbnail = MagicMock(file_id=f"t-{unique}", file_unique_id=f"th-{unique}") if thumbnail else None
    return m


def photo(caption=None, unique="p1", user_id=5, i=0):
    m = msg("", user_id=user_id, message_id=8500 + i)
    m.text, m.caption, m.content_type = None, caption, "photo"
    m.photo = [MagicMock(file_id="small", file_unique_id="s"), MagicMock(file_id=f"big-{unique}", file_unique_id=unique)]
    return m


def build(env, ocr, *responses):
    env.bot.download = MagicMock(side_effect=lambda file_id: _async(io.BytesIO(b"image")))
    jev_adapter = TelegramAdapter(env.bot, __import__("core.moderation", fromlist=["Moderator"]).Moderator(_Jev(responses)), env.storage, ocr)
    from unittest.mock import AsyncMock

    jev_adapter.is_admin = AsyncMock(return_value=False)
    return jev_adapter


async def _async(value):
    return value


class _Jev:
    def __init__(self, responses):
        self.responses, self.calls = list(responses) or [answer()], []

    async def ask(self, state, questions):
        self.calls.append((state, questions))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


async def test_a_newcomers_spam_sticker_is_removed_but_not_banned(env):
    ocr = FakeOcr()
    adapter = build(env, ocr, SPAM_BOT)
    m = sticker()
    await adapter.handle_comment(m)
    m.delete.assert_awaited_once()
    env.bot.ban_chat_member.assert_not_awaited()
    state, _ = adapter.moderator.jev.calls[0]
    assert "Текст на ней распознан неточно" in state["comment"] and "ЗАРАБОТОК" in state["comment"]
    async with env.storage.db.execute("SELECT text, reason FROM log ORDER BY id DESC LIMIT 1") as cur:
        row = await cur.fetchone()
    assert row["text"].startswith("[стикер] с надписью: ПИШИ В ЛС") and "по тексту на картинке" in row["reason"]


async def test_each_picture_is_read_once(env):
    ocr = FakeOcr()
    adapter = build(env, ocr, answer())
    await adapter.handle_comment(sticker(user_id=5, i=1))
    await adapter.handle_comment(sticker(user_id=6, i=2))  # the same sticker sent by another newcomer
    assert ocr.calls == 1 and env.bot.download.call_count == 1
    assert await env.storage.get_ocr("u1") == "ПИШИ В ЛС ЗАРАБОТОК БЕЗ ВЛОЖЕНИЙ"


async def test_a_picture_without_text_costs_nothing_and_stays_unread_next_time(env):
    ocr = FakeOcr(text="")
    adapter = build(env, ocr, answer())
    m = sticker()
    await adapter.handle_comment(m)
    m.delete.assert_not_awaited()
    assert adapter.moderator.jev.calls == [] and await env.storage.get_ocr("u1") == ""
    await adapter.handle_comment(sticker(user_id=6, i=3))
    assert ocr.calls == 1, "'no text' is remembered too"


async def test_only_newcomers_pictures_are_read(env):
    ocr = FakeOcr()
    adapter = build(env, ocr, answer())
    for _ in range(3):
        await env.storage.count_message(CHAT, 7)
    await adapter.handle_comment(sticker(user_id=7))
    assert ocr.calls == 0


@pytest.mark.parametrize("setup", ["ocr_off", "unavailable", "edited", "lockdown"])
async def test_reading_is_skipped_when_switched_off_or_pointless(env, setup):
    ocr = FakeOcr()
    if setup == "ocr_off":
        await env.storage.set_setting(CHAT, "image_ocr", 0)
    if setup == "lockdown":
        await env.storage.set_setting(CHAT, "lockdown", 1)
    if setup == "unavailable":
        ocr.available = False
    adapter = build(env, ocr, answer())
    await adapter.handle_comment(sticker(), edited=(setup == "edited"))
    assert ocr.calls == 0


async def test_animated_stickers_are_read_through_their_preview(env):
    ocr = FakeOcr()
    adapter = build(env, ocr, answer())
    await adapter.handle_comment(sticker(unique="a1", animated=True))
    env.bot.download.assert_called_once_with("t-a1")
    await adapter.handle_comment(sticker(unique="a2", animated=True, thumbnail=False, user_id=6, i=1))
    assert ocr.calls == 1, "no preview, nothing to read"


async def test_photo_caption_and_picture_text_go_to_jev_together(env):
    adapter = build(env, FakeOcr("YOU WON 500 DOLLARS"), answer())
    await adapter.handle_comment(photo(caption="Смотрите!"))
    env.bot.download.assert_called_once_with("big-p1")
    comment = adapter.moderator.jev.calls[0][0]["comment"]
    assert comment.startswith("Смотрите!") and "YOU WON 500 DOLLARS" in comment


async def test_a_picture_that_cannot_be_downloaded_is_just_skipped(env):
    ocr = FakeOcr()
    adapter = build(env, ocr, answer())
    env.bot.download = MagicMock(side_effect=RuntimeError("network"))
    m = sticker()
    await adapter.handle_comment(m)
    m.delete.assert_not_awaited()
    assert ocr.calls == 0 and await env.storage.get_ocr("u1") is None


async def test_the_owner_sees_what_was_read_in_trial_mode(env):
    await env.storage.set_setting(CHAT, "observe", 1)
    adapter = build(env, FakeOcr(), SPAM_BOT)
    await adapter.handle_comment(sticker())
    notice = sent_to(env.bot, OWNER)[-1].args[1]
    assert "Пробный режим" in notice and "с надписью" in notice and "ЗАРАБОТОК" in notice


async def test_a_doubtful_picture_goes_to_the_admin_with_its_text(env):
    doubtful = answer("normal", 0.55, probs={"normal": 0.55, "spam": 0.3, "rule_violation": 0.1, "useful": 0.05})
    adapter = build(env, FakeOcr("СКИДКА ТОЛЬКО СЕГОДНЯ"), doubtful)
    await adapter.handle_comment(sticker())
    review = sent_to(env.bot, OWNER)[-1].args[1]
    assert "Не уверен" in review and "СКИДКА ТОЛЬКО СЕГОДНЯ" in review
