"""Newcomer profile checks: fingerprints, the shared base of spammer avatars, and the bot's behaviour."""

import asyncio
import io
import random
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from PIL import Image, ImageDraw

from adapters.telegram import bot as botmod
from core.avatar import dhash, distance, similar
from core.models import ActionType, Author, Category, Comment, Profile, Verdict
from core.moderation import MODES, ChatConfig, Moderator, build_questions, build_state, decide
from core.rules import BASE_RULE
from core.storage import Storage
from tests.test_handlers import ADMIN_ID, CHAT, LINKED, OWNER, SPAM_BOT, answer, env, msg, sent_to  # noqa: F401

# ---------------------------------------------------------------------------- pictures


def picture(seed: int) -> Image.Image:
    rnd = random.Random(seed)
    img = Image.new("RGB", (192, 192), (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    draw = ImageDraw.Draw(img)
    for _ in range(8):
        x, y = rnd.randrange(150), rnd.randrange(150)
        draw.ellipse([x, y, x + rnd.randrange(20, 90), y + rnd.randrange(20, 90)],
                     fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    return img


def png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def jpeg(img: Image.Image, size=(96, 96), quality=45) -> bytes:
    buf = io.BytesIO()
    img.resize(size).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def test_same_picture_after_resizing_and_recompression_is_recognised():
    original = dhash(png(picture(1)))
    assert similar(original, dhash(jpeg(picture(1)))) and distance(original, original) == 0


def test_different_pictures_are_not_confused():
    hashes = [dhash(png(picture(seed))) for seed in range(1, 21)]
    close = [(i, j) for i in range(20) for j in range(i + 1, 20) if similar(hashes[i], hashes[j])]
    assert close == []


def test_unreadable_files_give_no_fingerprint():
    assert dhash(b"not an image") is None and dhash(b"") is None


# ------------------------------------------------------------------------------- storage


@pytest.fixture
async def storage(tmp_path):
    s = Storage(str(tmp_path / "p.db"))
    await s.open()
    yield s
    await s.close()


async def test_profile_cache_expires(storage):
    await storage.save_profile(7, "био", True, "0" * 16)
    assert (await storage.get_profile(7, 60))["bio"] == "био"
    await storage.db.execute("UPDATE profiles SET fetched_at = ? WHERE user_id = 7", (int(time.time()) - 500,))
    await storage.db.commit()
    assert await storage.get_profile(7, 60) is None and await storage.get_profile(7, 1000) is not None


async def test_avatar_base_matches_near_copies_and_can_forget(storage):
    h = dhash(png(picture(3)))
    assert not await storage.avatar_banned(h)
    await storage.add_avatar_ban(h)
    assert await storage.avatar_banned(dhash(jpeg(picture(3))))
    assert not await storage.avatar_banned(dhash(png(picture(4))))
    await storage.remove_avatar_ban(dhash(jpeg(picture(3))))
    assert not await storage.avatar_banned(h)


async def test_old_profiles_are_purged(storage):
    await storage.save_profile(7, "био", True, None)
    await storage.db.execute("UPDATE profiles SET fetched_at = ?", (int(time.time()) - 40 * 86400,))
    await storage.db.commit()
    await storage.purge_old(30)
    assert await storage.get_profile(7, 10**9) is None


# ------------------------------------------------------------------------------- decisions


def verdict(category="normal", bait=0.0, bot=0.1, confidence=0.95, rule=None):
    return Verdict(Category(category), confidence, bot, rule, profile_bait=bait)


def test_lure_profile_of_a_newcomer_goes_to_the_admin():
    d = decide(verdict(bait=0.9), [BASE_RULE], MODES["normal"], newcomer=True)
    assert d.action == ActionType.SEND_TO_REVIEW and d.reason == "подозрительный профиль новичка"


def test_profile_is_ignored_for_established_members():
    assert decide(verdict(bait=0.99), [BASE_RULE], MODES["normal"], newcomer=False).action == ActionType.NONE


def test_thresholds_follow_the_mode():
    v = verdict(bait=0.7)
    assert decide(v, [BASE_RULE], MODES["soft"], newcomer=True).action == ActionType.NONE
    assert decide(v, [BASE_RULE], MODES["normal"], newcomer=True).action == ActionType.NONE
    assert decide(v, [BASE_RULE], MODES["strict"], newcomer=True).action == ActionType.SEND_TO_REVIEW


def test_a_lure_profile_makes_a_spam_ban_more_likely():
    spam = verdict("spam", bait=0.9, bot=0.1, confidence=0.97, rule=0)
    assert decide(spam, [BASE_RULE], MODES["normal"], newcomer=True).action == ActionType.DELETE_AND_BAN
    assert decide(spam, [BASE_RULE], MODES["normal"], newcomer=False).action == ActionType.DELETE_AND_EXPLAIN


async def test_known_spammer_avatar_is_removed_without_asking_jev():
    class NoJev:
        async def ask(self, *a):
            raise AssertionError("Jev must not be called")

    author = Author(1, "Настя", previous_messages=0, profile=Profile(bio=None, has_photo=True, avatar_match=True))
    m = Moderator(NoJev())
    d = await m.check(Comment(1, 1, "привет всем", author), ChatConfig())
    assert d.action == ActionType.DELETE_SILENT and "аватарка" in d.reason
    d = await m.check(Comment(1, 1, "привет всем", author), ChatConfig(mode="soft"))
    assert d.action == ActionType.SEND_TO_REVIEW, "soft mode asks instead of deleting"


async def test_avatar_shortcut_is_off_when_profile_check_is_off():
    class Jev:
        calls = 0

        async def ask(self, *a):
            Jev.calls += 1
            return answer()

    author = Author(1, "Настя", previous_messages=0, profile=Profile(avatar_match=True))
    d = await Moderator(Jev()).check(Comment(1, 1, "привет всем", author), ChatConfig(profile_check=False))
    assert d.action == ActionType.NONE and Jev.calls == 1


async def test_established_members_with_a_matching_avatar_are_left_alone():
    class Jev:
        async def ask(self, *a):
            return answer()

    author = Author(1, "Настя", previous_messages=10, profile=Profile(avatar_match=True))
    d = await Moderator(Jev()).check(Comment(1, 1, "привет всем", author), ChatConfig())
    assert d.action == ActionType.NONE


def test_profile_question_only_for_newcomers_and_profile_reaches_jev():
    assert "profile_bait" in build_questions([BASE_RULE], profile=True)
    assert "profile_bait" not in build_questions([BASE_RULE])
    author = Author(1, "Настя", username="nastya_1", profile=Profile(bio="Пиши в личку", has_photo=False, avatar_match=True))
    profile = build_state(Comment(1, 1, "привет", author), [])["author"]["profile"]
    assert profile == {"bio": "Пиши в личку", "has_profile_photo": False, "avatar_matches_a_banned_spammer": True}
    plain = Author(1, "Иван")
    assert "profile" not in build_state(Comment(1, 1, "привет", plain), [])["author"]


# ---------------------------------------------------------------------------- in the bot


def telegram(env, bio="Пиши мне в лс", image=None, photo=True, bio_error=False):
    """Make the fake bot answer profile calls the way Telegram does."""
    if bio_error:
        env.bot.get_chat = AsyncMock(side_effect=[TelegramBadRequest(method=MagicMock(), message="chat not found")] * 50)
    else:
        env.bot.get_chat = AsyncMock(return_value=MagicMock(bio=bio, linked_chat_id=LINKED, title="Чат"))
    sizes = [MagicMock(file_id="small"), MagicMock(file_id="medium")]
    env.bot.get_user_profile_photos = AsyncMock(
        return_value=MagicMock(total_count=1 if photo else 0, photos=[sizes] if photo else [])
    )
    env.bot.download = AsyncMock(side_effect=lambda file_id: io.BytesIO(image or png(picture(9))))


async def test_a_banned_spam_bot_teaches_the_base_and_its_clone_is_removed_at_once(env):
    telegram(env)
    adapter, jev = env.build(SPAM_BOT, answer())
    first = msg("Заработок без вложений пиши в лс", user_id=20, message_id=1)
    await adapter.handle_comment(first)
    env.bot.ban_chat_member.assert_awaited_once_with(CHAT, 20)
    # a different account, the same picture (recompressed), a harmless first comment
    telegram(env, image=jpeg(picture(9)))
    clone = msg("Привет всем 🔥", user_id=21, message_id=2)
    await adapter.handle_comment(clone)
    clone.delete.assert_awaited_once()
    assert len(jev.calls) == 1, "the clone never reached Jev"
    async with env.storage.db.execute("SELECT reason FROM log ORDER BY id DESC LIMIT 1") as cur:
        assert "аватарка" in (await cur.fetchone())["reason"]


async def test_jev_sees_the_profile_of_a_newcomer(env):
    telegram(env, bio="Пиши мне в лс")
    adapter, jev = env.build(answer())
    await adapter.handle_comment(msg("Привет всем", user_id=30))
    state, questions = jev.calls[0]
    assert state["author"]["profile"] == {"bio": "Пиши мне в лс", "has_profile_photo": True}
    assert "profile_bait" in questions


async def test_unavailable_profile_data_does_not_break_moderation(env):
    telegram(env, bio_error=True, photo=False)
    adapter, jev = env.build(answer())
    m = msg("Привет всем", user_id=31)
    await adapter.handle_comment(m)
    m.delete.assert_not_awaited()
    assert len(jev.calls) == 1
    assert "bio" not in jev.calls[0][0]["author"].get("profile", {})


async def test_profiles_are_fetched_once_and_only_for_newcomers(env):
    telegram(env)
    adapter, _ = env.build(answer())
    await adapter.handle_comment(msg("Привет всем", user_id=32, message_id=1))
    await adapter.handle_comment(msg("Как дела у всех", user_id=32, message_id=2))
    assert env.bot.get_user_profile_photos.await_count == 1, "the second message reuses the cache"
    for _ in range(3):
        await env.storage.count_message(CHAT, 33)
    await adapter.handle_comment(msg("Я здесь давно", user_id=33, message_id=3))
    assert env.bot.get_user_profile_photos.await_count == 1, "established members are not looked up"


async def test_profile_check_can_be_switched_off(env):
    await env.storage.set_setting(CHAT, "profile_check", 0)
    telegram(env)
    adapter, jev = env.build(answer())
    await adapter.handle_comment(msg("Привет всем", user_id=34))
    env.bot.get_user_profile_photos.assert_not_awaited()
    assert "profile_bait" not in jev.calls[0][1]


async def test_a_slow_lookup_does_not_hold_the_message(env, monkeypatch):
    telegram(env)

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    env.bot.get_user_profile_photos = AsyncMock(side_effect=slow)
    monkeypatch.setattr(botmod, "PROFILE_TIMEOUT", 0.05)
    adapter, jev = env.build(answer())
    await adapter.handle_comment(msg("Привет всем", user_id=35))
    assert len(jev.calls) == 1


async def test_a_wrongly_banned_person_no_longer_counts_against_their_picture(env):
    telegram(env)
    adapter, _ = env.build(SPAM_BOT)
    await adapter.handle_comment(msg("Заработок без вложений пиши в лс", user_id=40, message_id=1))
    assert await env.storage.avatar_banned(dhash(png(picture(9))))
    async with env.storage.db.execute("SELECT * FROM log ORDER BY id LIMIT 1") as cur:
        entry = await cur.fetchone()
    await adapter.restore_comment(entry)
    assert not await env.storage.avatar_banned(dhash(png(picture(9))))


async def test_trial_verdict_buttons_teach_and_unteach_the_base(env):
    await env.storage.set_setting(CHAT, "observe", 1)
    telegram(env)
    adapter, _ = env.build(SPAM_BOT)
    await adapter.handle_comment(msg("Заработок без вложений пиши в лс", user_id=41, message_id=1))
    assert not await env.storage.avatar_banned(dhash(png(picture(9)))), "a dry run teaches nothing"
    async with env.storage.db.execute("SELECT id FROM log ORDER BY id LIMIT 1") as cur:
        log_id = (await cur.fetchone())["id"]

    def press(data):
        c = MagicMock()
        c.data, c.from_user.id = data, OWNER
        c.message.html_text, c.message.edit_text, c.answer = "x", AsyncMock(), AsyncMock()
        return c

    await adapter.on_trial_verdict(press(f"obs:ok:{log_id}"))
    assert await env.storage.avatar_banned(dhash(png(picture(9))))
    await env.storage.db.execute("UPDATE log SET feedback = NULL WHERE id = ?", (log_id,))
    await env.storage.db.commit()
    await adapter.on_trial_verdict(press(f"obs:no:{log_id}"))
    assert not await env.storage.avatar_banned(dhash(png(picture(9))))


# ------------------------------------------------------------------------------ /profile


async def profile_reply(env, target_id=50, **kwargs):
    telegram(env, **kwargs)
    adapter, _ = env.build()
    adapter.is_admin_message = AsyncMock(return_value=True)
    target = msg("что-то", user_id=target_id)
    m = msg("/profile")
    m.reply_to_message = target
    m.reply = AsyncMock()
    await adapter.on_profile(m)
    return adapter, m.reply.await_args.args[0]


async def test_profile_command_shows_what_the_bot_sees(env):
    _, text = await profile_reply(env, bio="Пиши мне в лс")
    assert "Описание профиля: Пиши мне в лс" in text and "Фото профиля: есть" in text and "нет совпадений" in text


async def test_profile_command_says_when_telegram_hides_data(env):
    _, text = await profile_reply(env, bio_error=True, photo=False)
    assert "Описание профиля: недоступно боту" in text and "Фото профиля: нет" in text


async def test_profile_command_flags_a_known_spammer_picture(env):
    await env.storage.add_avatar_ban(dhash(png(picture(9))))
    _, text = await profile_reply(env)
    assert "совпадает с аватаркой известного спамера" in text


async def test_profile_command_needs_a_reply_and_an_admin(env):
    adapter, _ = env.build()
    adapter.is_admin_message = AsyncMock(return_value=True)
    m = msg("/profile")
    m.reply_to_message = None
    m.reply = AsyncMock()
    await adapter.on_profile(m)
    assert "Ответьте" in m.reply.await_args.args[0]
    adapter.is_admin_message = AsyncMock(return_value=False)
    m.reply = AsyncMock()
    await adapter.on_profile(m)
    m.reply.assert_not_awaited()
