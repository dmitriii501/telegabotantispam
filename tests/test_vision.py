"""Jev looks at pictures: preparing them, sending them, and what happens when the API refuses."""

import base64
import io
import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image, ImageDraw

from adapters.telegram.bot import TelegramAdapter
from core.images import GREY, MAX_SIDE, MIN_SIDE, image_key, prepare, privacy_wording, vision_enabled
from core.jev_client import IMAGE_REJECTIONS_TO_PAUSE, JevClient, JevError, JevRejected
from core.models import ActionType, Author, Comment
from core.moderation import IMAGE_REF, ChatConfig, Moderator, build_questions, build_state
from tests.test_handlers import CHAT, SPAM_BOT, answer
from tests.test_ocr import FakeOcr, _async, photo, sticker


def png(width, height, color=(200, 30, 30, 255), text=None):
    img = Image.new("RGBA", (width, height), color)
    if text:
        ImageDraw.Draw(img).text((10, 10), text, fill=(255, 255, 255, 255))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def opened(data):
    return Image.open(io.BytesIO(data))


# ------------------------------------------------------------------------------ preparing


@pytest.mark.parametrize("size", [(4000, 3000), (3000, 40), (50, 40), (500, 500), (1, 1), (280, 280)])
def test_every_picture_lands_inside_the_limits_of_the_api(size):
    data = prepare(png(*size))
    with opened(data) as img:
        assert img.format == "JPEG"
        assert MIN_SIDE <= min(img.size) and max(img.size) <= max(MAX_SIDE, MIN_SIDE)
        assert 78_400 <= img.width * img.height <= 2_458_624
    assert len(data) < 16 * 1024 * 1024


def test_a_narrow_strip_is_padded_not_stretched():
    with opened(prepare(png(1000, 60))) as img:
        assert img.width == 1000 and img.height == MIN_SIDE
        assert img.getpixel((500, 5)) == pytest.approx(GREY, abs=4), "the padding is neutral grey"


def test_a_transparent_sticker_goes_on_grey_not_black():
    with opened(prepare(png(512, 512, color=(0, 0, 0, 0)))) as img:
        assert img.getpixel((10, 10)) == pytest.approx(GREY, abs=4)


def test_exif_rotation_is_applied_because_the_api_ignores_it():
    img = Image.new("RGB", (800, 400), (255, 255, 255))
    img.paste((255, 0, 0), (0, 0, 400, 400))  # the left half is red
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise to display
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif)
    with opened(prepare(out.getvalue())) as rotated:
        assert rotated.height > rotated.width, "the portrait orientation the camera meant"


def test_garbage_is_not_a_picture():
    assert prepare(b"not an image") is None and prepare(b"") is None


def test_the_same_picture_has_the_same_key_even_after_recompression():
    base = png(900, 600, text="WIN BIG")
    again = io.BytesIO()
    opened(base).convert("RGB").save(again, "JPEG", quality=60)
    assert image_key(prepare(base)) == image_key(prepare(again.getvalue()))
    assert image_key(prepare(base)) != image_key(prepare(png(900, 600, color=(10, 200, 10, 255))))
    assert image_key(b"junk") is None


def test_the_switch_and_the_privacy_wording_follow_it(monkeypatch):
    monkeypatch.delenv("JEV_IMAGES", raising=False)
    assert vision_enabled() and "отправляются" in privacy_wording()[1]
    monkeypatch.setenv("JEV_IMAGES", "0")
    assert not vision_enabled() and "никуда не отправляются" in privacy_wording()[1]


# ---------------------------------------------------------------------------- over the wire


async def fake_api(handler):
    app = web.Application()
    app.router.add_post("/v1/systemone", handler)
    server = TestServer(app)
    await server.start_server()
    return server, f"http://{server.host}:{server.port}/v1/systemone"


async def test_the_request_carries_the_header_and_the_assets():
    seen = {}

    async def handler(request):
        seen["header"], seen["body"] = request.headers.get("TypeSafe-Danger-Zone-Images"), await request.json()
        return web.json_response(answer())

    server, url = await fake_api(handler)
    client = JevClient("k", api_url=url, retries=1)
    try:
        await client.ask({"picture": "ref"}, {"q": {}}, images={"ref": b"\xff\xd8jpeg"})
        assert seen["header"] == "true"
        asset = seen["body"]["assets"]["ref"]
        assert asset["type"] == "image" and asset["format"] == "jpg"
        assert base64.b64decode(asset["data_base64"]) == b"\xff\xd8jpeg"
        await client.ask({"x": 1}, {"q": {}})
        assert seen["header"] is None and "assets" not in seen["body"], "text-only requests stay as they were"
    finally:
        await client.close()
        await server.close()


async def test_a_refused_picture_is_a_rejection_that_does_not_open_the_breaker():
    async def handler(request):
        return web.json_response({"detail": "bad image"}, status=400)

    server, url = await fake_api(handler)
    client = JevClient("k", api_url=url, retries=1, breaker_threshold=2)
    try:
        for _ in range(4):
            with pytest.raises(JevRejected):
                await client.ask({"picture": "r"}, {}, images={"r": b"x"})
        assert client.healthy, "a bad picture says nothing about the service"
        with pytest.raises(JevError) as plain:  # without a picture a 400 is still an ordinary error
            await client.ask({}, {})
        assert not isinstance(plain.value, JevRejected)
    finally:
        await client.close()
        await server.close()


async def test_pictures_are_paused_after_repeated_refusals_and_the_text_still_goes():
    client = JevClient("k", retries=1)
    assert client.vision
    for _ in range(IMAGE_REJECTIONS_TO_PAUSE):
        client._note_image_refusal()
    assert not client.vision and client.healthy, "only pictures wait, the service keeps working"
    assert not JevClient("k", vision=False).vision


# ----------------------------------------------------------------------------- moderation


class SeeingJev:
    vision = True

    def __init__(self, *responses, refuse=False):
        self.responses, self.refuse, self.calls = list(responses) or [answer()], refuse, []

    async def ask(self, state, questions, key=None, images=None):
        self.calls.append((state, images))
        if images and self.refuse:
            raise JevRejected("HTTP 400")
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def newcomer_comment(text="", image=b"jpeg"):
    return Comment(CHAT, 1, text, Author(9, "Bot", "bot9", False, 0, 0), image=image)


def test_the_state_points_the_question_at_the_picture():
    state = build_state(newcomer_comment(), [])
    assert state["picture"] == IMAGE_REF and "`picture`" in state["comment"]
    assert "picture" not in build_state(newcomer_comment(image=None, text="привет всем"), [])


def test_the_questions_mention_the_picture_only_when_there_is_one():
    with_picture = build_questions([], picture=True)
    assert all("`picture`" in with_picture[name]["instructions"] for name in ("category", "rule", "is_bot"))
    assert not any("picture" in q["instructions"] for q in build_questions([]).values())


async def test_a_picture_without_any_text_is_still_sent_to_jev():
    jev = SeeingJev(answer("spam", 0.99, bot=0.95))
    decision = await Moderator(jev).check(newcomer_comment(), ChatConfig())
    assert decision.action != ActionType.NONE
    assert jev.calls[0][1] == {IMAGE_REF: b"jpeg"}


async def test_a_picture_is_never_grounds_for_an_automatic_ban():
    jev = SeeingJev(answer("spam", 0.99, bot=0.95))
    decision = await Moderator(jev).check(newcomer_comment(), ChatConfig())
    assert decision.action == ActionType.DELETE_SILENT and decision.reason.endswith("(по тексту на картинке)")


async def test_when_the_picture_is_refused_the_text_is_judged_alone():
    jev = SeeingJev(answer("normal", 0.9), refuse=True)
    decision = await Moderator(jev).check(newcomer_comment("Подскажите, где купить такой же?"), ChatConfig())
    assert decision.action == ActionType.NONE
    assert [bool(images) for _, images in jev.calls] == [True, False]
    assert "picture" not in jev.calls[1][0]


async def test_a_refused_picture_with_no_text_is_let_through():
    jev = SeeingJev(answer("spam", 0.99), refuse=True)
    decision = await Moderator(jev).check(newcomer_comment(), ChatConfig())
    assert decision.action == ActionType.NONE and len(jev.calls) == 1


# ---------------------------------------------------------------------------------- the bot


def seeing_adapter(env, jev, ocr=None):
    env.bot.download = MagicMock(side_effect=lambda file_id: _async(io.BytesIO(png(600, 400, text="ЗАРАБОТОК"))))
    adapter = TelegramAdapter(env.bot, Moderator(jev), env.storage, ocr or FakeOcr())
    adapter.is_admin = MagicMock(side_effect=lambda *a: _async(False))
    return adapter


async def test_a_newcomers_spam_picture_is_removed_after_jev_looked_at_it(env):
    jev = SeeingJev(SPAM_BOT)
    adapter = seeing_adapter(env, jev)
    m = photo(unique="v1")
    await adapter.handle_comment(m)
    m.delete.assert_awaited_once()
    env.bot.ban_chat_member.assert_not_awaited()
    (state, images), = jev.calls
    assert state["picture"] in images and opened(images[state["picture"]]).format == "JPEG"
    assert adapter.ocr.calls == 0, "the local reader is not needed when the AI sees the picture"
    async with env.storage.db.execute("SELECT text FROM log ORDER BY id DESC LIMIT 1") as cur:
        assert "ИИ смотрел картинку" in (await cur.fetchone())["text"]


async def test_the_admin_decision_about_a_picture_is_remembered_by_its_fingerprint(env):
    jev = SeeingJev(SPAM_BOT)
    adapter = seeing_adapter(env, jev)
    await adapter.handle_comment(photo(unique="v1", user_id=5, i=1))
    again = photo(unique="v2", user_id=6, i=2)  # another newcomer, the same picture under another file id
    await adapter.handle_comment(again)
    again.delete.assert_awaited_once()
    assert len(jev.calls) == 1, "the second copy is recognised without asking Jev again"


async def test_if_the_api_stops_taking_pictures_the_local_reader_steps_in(env):
    jev = SeeingJev(answer(), refuse=True)
    adapter = seeing_adapter(env, jev)
    for _ in range(IMAGE_REJECTIONS_TO_PAUSE):
        jev.vision = True
    jev.vision = False  # as the real client reports after repeated refusals
    await adapter.handle_comment(sticker(unique="s1"))
    assert adapter.ocr.calls == 1 and jev.calls[0][1] is None


async def test_pictures_stay_off_when_the_owner_switched_them_off(env):
    jev = SeeingJev(answer())
    adapter = seeing_adapter(env, jev)
    await env.storage.set_setting(CHAT, "image_ocr", 0)
    await adapter.handle_comment(photo(unique="v9"))
    assert jev.calls == [] and env.bot.download.call_count == 0


async def test_an_oversized_download_failure_just_skips_the_picture(env):
    jev = SeeingJev(answer())
    adapter = seeing_adapter(env, jev)
    env.bot.download = MagicMock(side_effect=RuntimeError("network"))
    m = photo(unique="v7")
    await adapter.handle_comment(m)
    m.delete.assert_not_awaited() and jev.calls == []


async def test_the_json_the_client_sends_is_serialisable():
    json.dumps({"assets": {IMAGE_REF: {"data_base64": base64.b64encode(b"x").decode()}}})
