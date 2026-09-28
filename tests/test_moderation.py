import pytest

from core.models import ActionType, Author, Category, Comment, Verdict
from core.moderation import (
    MODES,
    ChatConfig,
    Moderator,
    build_questions,
    build_state,
    decide,
    needs_check,
    parse_verdict,
)
from core.rules import BASE_RULE, EVERYTHING, PERMISSION, PROHIBITION, Rule

RULES = [
    Rule("Канал про крипту", "context"),
    Rule("Мат можно", PERMISSION),
    Rule("Рекламу других каналов нельзя", PROHIBITION, "ban"),
    Rule("Политика запрещена", PROHIBITION, None),
    Rule("Оскорбления — предупреждать", PROHIBITION, "warn"),
]
CONFIG = ChatConfig(rules=RULES)
P = CONFIG.prohibitions  # [base, ads(ban), politics(auto), insults(warn)]


def v(category, confidence=0.95, bot=0.1, rule=None):
    return Verdict(Category(category), confidence, bot, rule)


def test_prohibitions_start_with_base_rule_and_skip_permissions():
    assert P[0] is BASE_RULE
    assert [r.text for r in P[1:]] == [
        "Рекламу других каналов нельзя",
        "Политика запрещена",
        "Оскорбления — предупреждать",
    ]


def test_obvious_spam_bot_is_banned():
    assert decide(v("spam", 0.99, 0.9, 0), P).action == ActionType.DELETE_AND_BAN


def test_spam_from_human_gets_explanation():
    assert decide(v("spam", 0.95, 0.1, 0), P).action == ActionType.DELETE_AND_EXPLAIN


def test_unsure_author_is_deleted_silently():
    assert decide(v("rule_violation", 0.9, 0.5, 2), P).action == ActionType.DELETE_SILENT


def test_low_confidence_goes_to_review():
    assert decide(v("spam", 0.6, 0.9, 0), P).action == ActionType.SEND_TO_REVIEW


def test_rule_action_overrides_automatic_choice():
    assert decide(v("rule_violation", 0.95, 0.1, 1), P).action == ActionType.DELETE_AND_BAN
    assert decide(v("rule_violation", 0.95, 0.1, 3), P).action == ActionType.DELETE_AND_EXPLAIN


def test_explanation_is_never_sent_to_probable_spammer():
    assert decide(v("rule_violation", 0.95, 0.6, 3), P).action == ActionType.DELETE_SILENT


def test_useful_is_forwarded():
    assert decide(v("useful", 0.8), P).action == ActionType.FORWARD_USEFUL
    assert decide(v("useful", 0.5), P).action == ActionType.NONE


def test_normal_is_left_alone():
    assert decide(v("normal"), P).action == ActionType.NONE


def test_explanation_quotes_admin_rule():
    d = decide(v("rule_violation", 0.95, 0.1, 2), P)
    assert d.reason == "нарушено правило «Политика запрещена»"


def test_modes_change_review_threshold():
    borderline = v("rule_violation", 0.7, 0.1, 2)
    assert decide(borderline, P, MODES["soft"]).action == ActionType.SEND_TO_REVIEW
    assert decide(borderline, P, MODES["normal"]).action == ActionType.SEND_TO_REVIEW
    assert decide(borderline, P, MODES["strict"]).action == ActionType.DELETE_AND_EXPLAIN


def test_escalation_mutes_then_bans_repeat_offenders():
    args = (v("rule_violation", 0.95, 0.1, 3), P)
    assert decide(*args, previous_deletions=1).action == ActionType.DELETE_AND_EXPLAIN
    mute = decide(*args, previous_deletions=2)
    assert mute.action == ActionType.DELETE_AND_MUTE and mute.extra["mute_hours"] == 24
    assert decide(*args, previous_deletions=4).action == ActionType.DELETE_AND_BAN


def test_escalation_can_be_switched_off():
    d = decide(v("rule_violation", 0.95, 0.1, 3), P, escalation=False, previous_deletions=10)
    assert d.action == ActionType.DELETE_AND_EXPLAIN


def test_needs_check_skips_emoji():
    assert not needs_check("👍👍")
    assert not needs_check("+")
    assert needs_check("ок норм")
    assert needs_check("t.me/x")


def comment(text, **kwargs):
    return Comment(1, 1, text, Author(1, "Вася", previous_messages=0), **kwargs)


def test_state_contains_restored_text_only_when_obfuscated():
    assert build_state(comment("зaрaбoток"), RULES)["comment_with_letters_restored"] == "заработок"
    assert "comment_with_letters_restored" not in build_state(comment("привет"), RULES)
    assert build_state(comment("привет"), RULES)["author"]["first_message_in_this_chat"] is True


def test_state_includes_all_admin_rules_and_context():
    state = build_state(comment("привет", post_text="Пост про биткоин", reply_to_text="Вася: чушь"), RULES)
    assert "Мат можно" in state["rules"] and "Канал про крипту" in state["rules"]
    assert state["post_the_comment_is_under"] == "Пост про биткоин"
    assert state["comment_being_replied_to"] == "Вася: чушь"
    assert "post_the_comment_is_under" not in build_state(comment("привет"), RULES)


def test_rule_question_offers_only_prohibitions():
    options = build_questions(P)["rule"]["criteria"]
    assert set(options) == {"r0", "r1", "r2", "r3", "none"}


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
    def __init__(self, response=None):
        self.response = response
        self.calls = 0

    async def ask(self, state, questions):
        self.calls += 1
        return self.response


async def test_moderator_does_not_call_jev_for_emoji():
    jev = FakeJev()
    decision = await Moderator(jev).check(comment("🔥🔥🔥"), CONFIG)
    assert decision.action == ActionType.NONE
    assert jev.calls == 0


async def test_lockdown_deletes_everything_without_asking_jev():
    jev = FakeJev()
    decision = await Moderator(jev).check(comment("🔥"), ChatConfig(rules=RULES, lockdown=True))
    assert decision.action == ActionType.DELETE_SILENT
    assert decision.verdict is not None
    assert jev.calls == 0


async def test_blanket_rule_uses_its_action():
    config = ChatConfig(rules=[Rule("Удаляй всё", EVERYTHING, "ban")])
    decision = await Moderator(FakeJev()).check(comment("привет"), config)
    assert decision.action == ActionType.DELETE_AND_BAN
    assert decision.reason == "правило «Удаляй всё»"


async def test_moderator_uses_jev_answers():
    response = {
        "answers": {
            "category": {"type": "choice", "choice": "rule_violation", "confidence": 0.99},
            "rule": {"type": "choice", "choice": "r1", "confidence": 1.0},
            "is_bot": {"type": "noul", "noul": 0.05},
        },
        "usage": {"input_tokens": 900},
    }
    jev = FakeJev(response)
    decision = await Moderator(jev).check(comment("купите мой канал t.me/x"), CONFIG)
    assert jev.calls == 1
    assert decision.action == ActionType.DELETE_AND_BAN


def test_config_from_row_supports_old_rows_without_json():
    row = {"rules_json": None, "rules": "мат можно. без политики", "mode": "strict", "lockdown": 0, "escalation": 1}
    config = ChatConfig.from_row(row)
    assert [r.text for r in config.rules] == ["мат можно", "без политики"]
    assert config.mode == "strict" and not config.lockdown
