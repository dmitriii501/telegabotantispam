import pytest

from core.models import ActionType, Author, Comment
from core.moderation import ChatConfig, Moderator
from core.normalizer import example_key
from core.storage import Storage


def test_example_key_ignores_spacing_punctuation_case_and_obfuscation():
    a = example_key("Зaрaбoтoк без вложений, пиши в лс!")
    assert a == example_key("заработок  БЕЗ вложений пиши в лс")
    assert a == example_key("з а р а б о т о к без вложений — пиши в лс...")
    assert a != example_key("совсем другой длинный текст комментария")


def test_short_texts_are_never_remembered():
    assert example_key("ок") is None
    assert example_key("спасибо!") is None


@pytest.fixture
async def storage(tmp_path):
    s = Storage(str(tmp_path / "m.db"))
    await s.open()
    yield s
    await s.close()


async def log(storage, text):
    return await storage.add_log(
        chat_id=1, message_id=1, user_id=5, user_name="u", text=text,
        category="spam", confidence=0.9, bot_probability=0.8, action="delete_silent", reason="", tokens=1,
    )


async def test_admin_decisions_are_remembered(storage):
    text = "Предлагаю сотрудничество по рекламе"
    key = example_key(text)
    assert await storage.example_kind(1, key) is None
    await storage.set_feedback(await log(storage, text), "not_spam")
    assert await storage.example_kind(1, key) == "allow"
    await storage.set_feedback(await log(storage, text), "confirmed")
    assert await storage.example_kind(1, key) == "remove"
    assert await storage.example_kind(2, key) is None  # other chats are unaffected


class NoJev:
    async def ask(self, *args):
        raise AssertionError("Jev must not be called for remembered comments")


def comment(text):
    return Comment(1, 1, text, Author(1, "Вася"))


async def test_remembered_comments_skip_jev():
    moderator = Moderator(NoJev())
    allowed = await moderator.check(comment("Предлагаю сотрудничество по рекламе"), ChatConfig(), "allow")
    assert allowed.action == ActionType.NONE and allowed.verdict is None
    removed = await moderator.check(comment("Заработок без вложений, пиши в лс"), ChatConfig(), "remove")
    assert removed.action == ActionType.DELETE_SILENT
    assert removed.reason == "такой комментарий уже удаляли"


async def test_tension_parses_jev_score():
    class Jev:
        async def ask(self, state, questions):
            assert state["discussion"][0]["author"] == "Анна"
            return {"answers": {"tension": {"type": "score", "score": 1.8, "confidence": 0.9}}}

    assert await Moderator(Jev()).tension([{"author": "Анна", "text": "привет"}]) == (1.8, 0.9)
