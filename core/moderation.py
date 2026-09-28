"""Moderation core: ask Jev about a comment and turn the answers into an action.

Everything here is platform-independent. Adapters build a `Comment`, call
`Moderator.check`, and execute the returned `Decision`.
"""

from dataclasses import dataclass

from core.jev_client import JevClient
from core.models import ActionType, Category, Comment, Decision, Verdict
from core.normalizer import normalize
from core.rules import MENTION_QUESTION, TOPICS, rule_list

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

# Comments without letters or links (emoji, "+", "!!!") are never sent to Jev.
MIN_LETTERS = 2


@dataclass
class Thresholds:
    review_below: float = 0.75  # violation with lower confidence goes to the admin
    ban_confidence: float = 0.9  # spam this sure + bot-like author -> ban
    ban_bot_probability: float = 0.7
    silent_bot_probability: float = 0.3  # above this the author is not clearly human
    useful_confidence: float = 0.7


def needs_check(text: str) -> bool:
    letters = sum(ch.isalpha() for ch in text)
    return letters >= MIN_LETTERS or "http" in text or "t.me" in text or "@" in text


def build_state(comment: Comment, rules: list[str]) -> dict:
    normalized = normalize(comment.text)
    a = comment.author
    author = {
        "name": a.display_name,
        "username": a.username or "нет",
        "telegram_premium": a.is_premium,
        "first_message_in_this_chat": a.previous_messages == 0,
        "previously_deleted_messages": a.previous_deletions,
    }
    state = {"rules": rules, "comment": comment.text, "author": author}
    if normalized != comment.text:
        state["comment_with_letters_restored"] = normalized
    return state


def build_questions(rules: list[str]) -> dict:
    rule_options = {f"r{i}": text for i, text in enumerate(rules)}
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


def decide(verdict: Verdict, rules: list[str], t: Thresholds = Thresholds()) -> Decision:
    reason = explain(verdict, rules)
    if verdict.category in (Category.SPAM, Category.RULE_VIOLATION):
        if verdict.confidence < t.review_below:
            action = ActionType.SEND_TO_REVIEW
        elif (
            verdict.category == Category.SPAM
            and verdict.confidence >= t.ban_confidence
            and verdict.bot_probability >= t.ban_bot_probability
        ):
            action = ActionType.DELETE_AND_BAN
        elif verdict.bot_probability > t.silent_bot_probability:
            # A bot, or we are not sure it is a human: do not teach spammers what we catch.
            action = ActionType.DELETE_SILENT
        else:
            action = ActionType.DELETE_AND_EXPLAIN
        return Decision(action, verdict, reason)
    if verdict.category == Category.USEFUL and verdict.confidence >= t.useful_confidence:
        return Decision(ActionType.FORWARD_USEFUL, verdict, reason)
    return Decision(ActionType.NONE, verdict, reason)


def explain(verdict: Verdict, rules: list[str]) -> str:
    """Reason text from templates: Jev picks the rule, the bot quotes it."""
    if verdict.category == Category.SPAM:
        return "спам или реклама"
    if verdict.category == Category.RULE_VIOLATION:
        if verdict.rule_index is not None and 0 < verdict.rule_index < len(rules):
            return f"нарушено правило «{rules[verdict.rule_index]}»"
        return "нарушение правил чата"
    if verdict.category == Category.USEFUL:
        return "полезный комментарий"
    return ""


class Moderator:
    def __init__(self, jev: JevClient, thresholds: Thresholds = Thresholds()):
        self.jev = jev
        self.thresholds = thresholds

    async def check(self, comment: Comment, admin_rules_text: str) -> Decision:
        if not needs_check(comment.text):
            return Decision(ActionType.NONE)
        rules = rule_list(admin_rules_text)
        response = await self.jev.ask(build_state(comment, rules), build_questions(rules))
        return decide(parse_verdict(response), rules, self.thresholds)

    async def preview_rules(self, admin_rules_text: str) -> list[tuple[str, str]]:
        """How Jev understood the rules: [(label, "allowed" | "forbidden" | "unspecified")]."""
        questions = {}
        for key, (question, label) in TOPICS.items():
            questions[f"{key}_allowed"] = {"type": "noul", "instructions": question}
            questions[f"{key}_mentioned"] = {"type": "noul", "instructions": MENTION_QUESTION.format(label=label)}
        response = await self.jev.ask({"rules": admin_rules_text}, questions)
        answers = response["answers"]
        result = []
        for key, (_, label) in TOPICS.items():
            if answers[f"{key}_mentioned"]["noul"] < 0.5:
                status = "unspecified"
            elif answers[f"{key}_allowed"]["noul"] >= 0.5:
                status = "allowed"
            else:
                status = "forbidden"
            result.append((label, status))
        return result
