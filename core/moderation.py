"""Moderation core: ask Jev about a comment and turn the answers into an action.

Everything here is platform-independent. Adapters build a `Comment`, call
`Moderator.check`, and execute the returned `Decision`.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from core.jev_client import JevClient
from core.models import ActionType, Category, Comment, Decision, Verdict
from core.normalizer import normalize
from core.rules import (
    BASE_RULE,
    EVERYTHING,
    PROHIBITION,
    Rule,
    build_parse_questions,
    parse_rules_response,
    rules_from_json,
    rules_from_text,
    split_rules,
)

CATEGORY_CRITERIA = {
    Category.SPAM.value: (
        "Спам: реклама заработка, казино, ставок, наркотиков, «пиши в лс», продажа чего-либо, "
        "мошенничество, продвижение своих каналов или сервисов. В том числе замаскированное "
        "подменой букв, цифрами или пробелами."
    ),
    Category.RULE_VIOLATION.value: "Не спам, но нарушает одно из правил в `rules`.",
    Category.USEFUL.value: (
        "Не нарушает правила и полезен автору канала: содержательный вопрос, дельное замечание, "
        "найденная ошибка, предложение о сотрудничестве."
    ),
    Category.NORMAL.value: "Обычный допустимый комментарий: реакция, мнение, благодарность, шутка.",
}

ACTION_MAP = {
    "ban": ActionType.DELETE_AND_BAN,
    "mute": ActionType.DELETE_AND_MUTE,
    "warn": ActionType.DELETE_AND_EXPLAIN,
    "delete": ActionType.DELETE_SILENT,
    "review": ActionType.SEND_TO_REVIEW,
}

# Comments without letters or links (emoji, "+", "!!!") are never sent to Jev.
MIN_LETTERS = 2

# Repeat offenders: the Nth deleted comment in a chat escalates the action.
MUTE_AT = 3
BAN_AT = 5
MUTE_HOURS = 24

CONTEXT_LIMIT = 600


@dataclass
class Thresholds:
    review_below: float = 0.75  # violation with lower confidence goes to the admin
    ban_confidence: float = 0.9  # spam this sure + bot-like author -> ban
    ban_bot_probability: float = 0.7
    silent_bot_probability: float = 0.3  # above this the author is not clearly human
    useful_confidence: float = 0.7


MODES = {
    "soft": Thresholds(review_below=0.85, ban_confidence=0.95, ban_bot_probability=0.8),
    "normal": Thresholds(),
    "strict": Thresholds(review_below=0.55, ban_confidence=0.85, ban_bot_probability=0.6),
}


@dataclass
class ChatConfig:
    rules: list[Rule] = field(default_factory=list)  # what the admin wrote, all kinds
    mode: str = "normal"
    lockdown: bool = False  # delete everything from non-admins
    escalation: bool = True

    @property
    def prohibitions(self) -> list[Rule]:
        """Rules Jev may pick as violated. Index 0 is always the built-in spam rule."""
        return [BASE_RULE, *(r for r in self.rules if r.kind == PROHIBITION)]

    @property
    def blanket(self) -> Rule | None:
        return next((r for r in self.rules if r.kind == EVERYTHING), None)

    @property
    def thresholds(self) -> Thresholds:
        return MODES.get(self.mode, MODES["normal"])

    @classmethod
    def from_row(cls, row: Mapping | None) -> "ChatConfig":
        if row is None:
            return cls()
        if row["rules_json"]:
            rules = rules_from_json(row["rules_json"])
        else:
            rules = rules_from_text(row["rules"] or "")
        return cls(
            rules=rules,
            mode=row["mode"] or "normal",
            lockdown=bool(row["lockdown"]),
            escalation=bool(row["escalation"]),
        )


def needs_check(text: str) -> bool:
    letters = sum(ch.isalpha() for ch in text)
    return letters >= MIN_LETTERS or "http" in text or "t.me" in text or "@" in text


def _clip(text: str | None) -> str | None:
    if not text:
        return None
    return text if len(text) <= CONTEXT_LIMIT else text[:CONTEXT_LIMIT] + "…"


def build_state(comment: Comment, rules: list[Rule]) -> dict:
    normalized = normalize(comment.text)
    a = comment.author
    author = {
        "name": a.display_name,
        "username": a.username or "нет",
        "telegram_premium": a.is_premium,
        "first_message_in_this_chat": a.previous_messages == 0,
        "previously_deleted_messages": a.previous_deletions,
    }
    # Everything the admin wrote goes in: permissions and context help Jev too.
    texts = [BASE_RULE.text, *(r.text for r in rules if r.kind != EVERYTHING)]
    state = {"rules": texts, "comment": comment.text, "author": author}
    if normalized != comment.text:
        state["comment_with_letters_restored"] = normalized
    if comment.post_text:
        state["post_the_comment_is_under"] = _clip(comment.post_text)
    if comment.reply_to_text:
        state["comment_being_replied_to"] = _clip(comment.reply_to_text)
    return state


def build_questions(prohibitions: list[Rule]) -> dict:
    rule_options = {f"r{i}": rule.text for i, rule in enumerate(prohibitions)}
    rule_options["none"] = "Комментарий не нарушает ни одно правило"
    return {
        "category": {
            "type": "choice",
            "instructions": "К какой категории относится `comment` с учётом `rules`?",
            "criteria": CATEGORY_CRITERIA,
        },
        "rule": {
            "type": "choice",
            "instructions": "Какое правило из `rules` нарушает `comment`?",
            "criteria": rule_options,
        },
        "is_bot": {
            "type": "noul",
            "instructions": "`comment` написан спам-ботом или спамером, а не обычным подписчиком?",
            "criteria": {
                "true": "Спам-бот, рекламщик или мошенник",
                "false": "Живой подписчик, даже если он грубит или нарушает правила",
            },
        },
    }


def parse_verdict(response: dict) -> Verdict:
    answers = response["answers"]
    rule_choice = answers["rule"]["choice"]
    return Verdict(
        category=Category(answers["category"]["choice"]),
        confidence=float(answers["category"]["confidence"]),
        bot_probability=float(answers["is_bot"]["noul"]),
        rule_index=None if rule_choice == "none" else int(rule_choice[1:]),
        input_tokens=int(response.get("usage", {}).get("input_tokens", 0)),
    )


def _matched_rule(verdict: Verdict, prohibitions: list[Rule]) -> Rule | None:
    i = verdict.rule_index
    return prohibitions[i] if i is not None and 0 <= i < len(prohibitions) else None


def _auto_action(verdict: Verdict, t: Thresholds) -> ActionType:
    if (
        verdict.category == Category.SPAM
        and verdict.confidence >= t.ban_confidence
        and verdict.bot_probability >= t.ban_bot_probability
    ):
        return ActionType.DELETE_AND_BAN
    if verdict.bot_probability > t.silent_bot_probability:
        # A bot, or we are not sure it is a human: do not teach spammers what we catch.
        return ActionType.DELETE_SILENT
    return ActionType.DELETE_AND_EXPLAIN


def decide(
    verdict: Verdict,
    prohibitions: list[Rule],
    t: Thresholds = Thresholds(),
    escalation: bool = True,
    previous_deletions: int = 0,
) -> Decision:
    reason = explain(verdict, prohibitions)
    if verdict.category in (Category.SPAM, Category.RULE_VIOLATION):
        if verdict.confidence < t.review_below:
            return Decision(ActionType.SEND_TO_REVIEW, verdict, reason)
        rule = _matched_rule(verdict, prohibitions)
        if rule and rule.action:
            action = ACTION_MAP[rule.action]
        else:
            action = _auto_action(verdict, t)
        if action == ActionType.DELETE_AND_EXPLAIN and verdict.bot_probability > t.silent_bot_probability:
            action = ActionType.DELETE_SILENT  # never explain to a probable spammer
        extra = {}
        if escalation and action in (
            ActionType.DELETE_SILENT,
            ActionType.DELETE_AND_EXPLAIN,
            ActionType.DELETE_AND_MUTE,
        ):
            strikes = previous_deletions + 1
            if strikes >= BAN_AT:
                action, reason = ActionType.DELETE_AND_BAN, f"{reason} (повторные нарушения)"
            elif strikes >= MUTE_AT and action != ActionType.DELETE_AND_MUTE:
                action, reason = ActionType.DELETE_AND_MUTE, f"{reason} (повторные нарушения)"
        if action == ActionType.DELETE_AND_MUTE:
            extra["mute_hours"] = MUTE_HOURS
        return Decision(action, verdict, reason, extra)
    if verdict.category == Category.USEFUL and verdict.confidence >= t.useful_confidence:
        return Decision(ActionType.FORWARD_USEFUL, verdict, reason)
    return Decision(ActionType.NONE, verdict, reason)


def explain(verdict: Verdict, prohibitions: list[Rule]) -> str:
    """Reason text from templates: Jev picks the rule, the bot quotes it."""
    rule = _matched_rule(verdict, prohibitions)
    if rule and rule is not BASE_RULE and verdict.category in (Category.SPAM, Category.RULE_VIOLATION):
        return f"нарушено правило «{rule.text.rstrip('.!; ')}»"
    if verdict.category == Category.SPAM:
        return "спам или реклама"
    if verdict.category == Category.RULE_VIOLATION:
        return "нарушение правил чата"
    if verdict.category == Category.USEFUL:
        return "полезный комментарий"
    return ""


def _blanket_decision(rule: Rule | None, reason: str) -> Decision:
    action = ACTION_MAP[(rule.action if rule and rule.action else "delete")]
    verdict = Verdict(Category.RULE_VIOLATION, 1.0, 0.0)
    extra = {"mute_hours": MUTE_HOURS} if action == ActionType.DELETE_AND_MUTE else {}
    return Decision(action, verdict, reason, extra)


class Moderator:
    def __init__(self, jev: JevClient):
        self.jev = jev

    async def check(self, comment: Comment, config: ChatConfig) -> Decision:
        if config.lockdown:
            return _blanket_decision(None, "включён режим тишины")
        if config.blanket:
            return _blanket_decision(config.blanket, f"правило «{config.blanket.text.rstrip('.!; ')}»")
        if not needs_check(comment.text):
            return Decision(ActionType.NONE)
        prohibitions = config.prohibitions
        response = await self.jev.ask(build_state(comment, config.rules), build_questions(prohibitions))
        return decide(
            parse_verdict(response),
            prohibitions,
            config.thresholds,
            config.escalation,
            comment.author.previous_deletions,
        )

    async def parse_rules(self, admin_text: str) -> list[Rule]:
        """Classify every sentence of the admin's text: what it is and what to do about it."""
        sentences = split_rules(admin_text)
        if not sentences:
            return []
        response = await self.jev.ask({"rules_text": admin_text}, build_parse_questions(sentences))
        return parse_rules_response(sentences, response)
