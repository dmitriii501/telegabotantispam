import pytest

from core.models import ActionType, Author, Category, Comment, Verdict
from core.moderation import Moderator, build_state, decide, needs_check, parse_verdict
from core.rules import rule_list

RULES = rule_list("мат можно, рекламу других каналов нельзя")


def v(category, confidence=0.95, bot=0.1, rule=None):
    return Verdict(Category(category), confidence, bot, rule)


def test_obvious_spam_bot_is_banned():
    assert decide(v("spam", 0.99, 0.9), RULES).action == ActionType.DELETE_AND_BAN


def test_spam_from_human_gets_explanation():
    assert decide(v("spam", 0.95, 0.1), RULES).action == ActionType.DELETE_AND_EXPLAIN


def test_unsure_author_is_deleted_silently():
    assert decide(v("rule_violation", 0.9, 0.5), RULES).action == ActionType.DELETE_SILENT


def test_low_confidence_goes_to_review():
    assert decide(v("spam", 0.6, 0.9), RULES).action == ActionType.SEND_TO_REVIEW


def test_useful_is_forwarded():
    assert decide(v("useful", 0.8), RULES).action == ActionType.FORWARD_USEFUL
    assert decide(v("useful", 0.5), RULES).action == ActionType.NONE


def test_normal_is_left_alone():
    assert decide(v("normal"), RULES).action == ActionType.NONE


def test_explanation_quotes_admin_rule():
    d = decide(v("rule_violation", 0.95, 0.1, rule=2), RULES)
    assert d.reason == "нарушено правило «рекламу других каналов нельзя»"


def test_needs_check_skips_emoji():
    assert not needs_check("👍👍")
    assert not needs_check("+")
    assert needs_check("ок норм")
    assert needs_check("t.me/x")


def comment(text):
    return Comment(1, 1, text, Author(1, "Вася", previous_messages=0))


def test_state_contains_restored_text_only_when_obfuscated():
    assert build_state(comment("зaрaбoтoк"), RULES)["comment_with_letters_restored"] == "заработок"
    assert "comment_with_letters_restored" not in build_state(comment("привет"), RULES)
    assert build_state(comment("привет"), RULES)["author"]["first_message_in_this_chat"] is True


def test_parse_verdict():
    response = {
        "answers": {
            "category": {"type": "choice", "choice": "rule_violation", "confidence": 0.9},
            "rule": {"type": "choice", "choice": "r2", "confidence": 0.8},
            "is_bot": {"type": "noul", "noul": 0.2},
        },
        "usage": {"input_tokens": 700},
    }
    verdict = parse_verdict(response)
    assert verdict.category == Category.RULE_VIOLATION
    assert verdict.rule_index == 2
    assert verdict.input_tokens == 700


class FakeJev:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    async def ask(self, state, questions):
        self.calls += 1
        return self.response


@pytest.mark.asyncio
async def test_moderator_does_not_call_jev_for_emoji():
    jev = FakeJev({})
    decision = await Moderator(jev).check(comment("🔥🔥🔥"), "")
    assert decision.action == ActionType.NONE
    assert jev.calls == 0
