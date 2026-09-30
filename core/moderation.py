"""Moderation core: ask Jev about a comment and turn the answers into an action.

Everything here is platform-independent. Adapters build a `Comment`, call
`Moderator.check`, and execute the returned `Decision`.
"""

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from core.jev_client import JevClient
from core.models import ActionType, Category, Comment, Decision, Verdict
from core.fallback import looks_like_spam
from core.links import any_match, extract_links, outside
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

FLOOD_MUTE_MINUTES = 30
MOSCOW = timezone(timedelta(hours=3))
NEWCOMER_MESSAGES = 3  # fewer messages than this in the chat means a newcomer
FIRST_COMMENT_WINDOW = 120  # seconds after a post during which a newcomer's comment is "one of the first"

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
    suspect: float = 0.4  # "normal" comments this likely to break a rule go to the admin
    profile_review: float = 0.75  # a newcomer's profile this bait-like sends the message to the admin
    avatar_deletes: bool = True  # a picture of a known spammer removes the message (False: ask the admin)


MODES = {
    "soft": Thresholds(
        review_below=0.85, ban_confidence=0.95, ban_bot_probability=0.8, suspect=0.5,
        profile_review=0.85, avatar_deletes=False,
    ),
    "normal": Thresholds(),
    "strict": Thresholds(
        review_below=0.55, ban_confidence=0.85, ban_bot_probability=0.6, suspect=0.3, profile_review=0.6
    ),
}


@dataclass
class ChatConfig:
    rules: list[Rule] = field(default_factory=list)  # what the admin wrote, all kinds
    mode: str = "normal"
    lockdown: bool = False  # delete everything from non-admins
    escalation: bool = True
    conflicts: bool = True  # warn the owner when a discussion heats up
    antiflood: bool = True  # mute people who post many messages in seconds
    analytics: bool = True  # ask Jev for type, tone and leads of every comment
    observe: bool = False  # dry run: decide and log, but change nothing
    flood_messages: int = 6  # this many messages ...
    flood_window: int = 20  # ... within this many seconds trigger the antiflood mute
    flood_mute_minutes: int = 30
    links_mode: str = "ai"  # "ai": Jev decides; "block": only allowed links; "newcomers": newcomers may not post links
    allowed_domains: list[str] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)
    profile_check: bool = True  # look at the profile of a newcomer
    captcha: bool = False  # ask newcomers to press a button
    clean_service: bool = False  # delete "joined", "left" and similar service messages
    antiraid: bool = True  # warn the owner about mass joins
    first_strict: bool = True  # newcomers' comments in the first minutes under a post are judged strictly
    image_ocr: bool = True  # read text on pictures and stickers of newcomers
    night_mode: bool = False  # at night: strict thresholds, and newcomers may not post links
    night_from: int = 0  # Moscow hours, the night lasts from `night_from` up to `night_to`
    night_to: int = 7

    def night_active(self, now: datetime | None = None) -> bool:
        if not self.night_mode:
            return False
        hour = ((now or datetime.now(timezone.utc)).astimezone(MOSCOW)).hour
        if self.night_from <= self.night_to:
            return self.night_from <= hour < self.night_to
        return hour >= self.night_from or hour < self.night_to

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
            lockdown=_lockdown_active(row),
            escalation=bool(row["escalation"]),
            conflicts=_column(row, "conflicts", 1) == 1,
            antiflood=_column(row, "antiflood", 1) == 1,
            analytics=_column(row, "analytics", 1) == 1,
            observe=_column(row, "observe", 0) == 1,
            flood_messages=int(_column(row, "flood_messages", 6)),
            flood_window=int(_column(row, "flood_window", 20)),
            flood_mute_minutes=int(_column(row, "flood_mute", 30)),
            links_mode=_column(row, "links_mode", "ai"),
            allowed_domains=_json_list(_column(row, "allowed_domains", "[]")),
            blocked_domains=_json_list(_column(row, "blocked_domains", "[]")),
            profile_check=_column(row, "profile_check", 1) == 1,
            captcha=_column(row, "captcha", 0) == 1,
            clean_service=_column(row, "clean_service", 0) == 1,
            antiraid=_column(row, "antiraid", 1) == 1,
            first_strict=_column(row, "first_strict", 1) == 1,
            image_ocr=_column(row, "image_ocr", 1) == 1,
            night_mode=_column(row, "night_mode", 0) == 1,
            night_from=int(_column(row, "night_from", 0)),
            night_to=int(_column(row, "night_to", 7)),
        )


TENSION_QUESTION = {
    "type": "score",
    "instructions": "Насколько накалено обсуждение в `discussion`?",
    "criteria": [
        "Спокойное обсуждение, участники согласны или вежливо делятся мнениями",
        "Спор: мнения расходятся, тон резкий, но без взаимных оскорблений",
        "Ссора: участники оскорбляют друг друга или переходят на личности",
    ],
}
TENSION_ALERT = 1.4  # score at or above this warns the owner
TENSION_MIN_CONFIDENCE = 0.5


def _json_list(value) -> list[str]:
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _lockdown_active(row: Mapping) -> bool:
    """On, and either without an end time or before it (a freeze after a raid ends by itself)."""
    if not row["lockdown"]:
        return False
    until = _column(row, "lockdown_until", 0)
    return until == 0 or until > time.time()


def _column(row: Mapping, name: str, default):
    try:
        value = row[name]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def needs_check(text: str, links: list[str] | None = None) -> bool:
    letters = sum(ch.isalpha() for ch in text)
    return letters >= MIN_LETTERS or bool(links) or "http" in text or "t.me" in text or "@" in text


def _clip(text: str | None) -> str | None:
    if not text:
        return None
    return text if len(text) <= CONTEXT_LIMIT else text[:CONTEXT_LIMIT] + "…"


def build_state(comment: Comment, rules: list[Rule], allowed_domains: list[str] | None = None) -> dict:
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
    if a.profile:
        profile = {}
        if a.profile.bio is not None:
            profile["bio"] = _clip(a.profile.bio) or "пусто"
        if a.profile.has_photo is not None:
            profile["has_profile_photo"] = a.profile.has_photo
        if a.profile.avatar_match:
            profile["avatar_matches_a_banned_spammer"] = True
        if profile:
            author["profile"] = profile
    shown = comment.text
    if comment.image_text:
        note = f"[Картинка или стикер. Текст на ней распознан неточно, с ошибками]: {comment.image_text}"
        shown = f"{comment.text}\n{note}" if comment.text else note
    state = {"rules": texts, "comment": shown, "author": author}
    if normalized != comment.text:
        state["comment_with_letters_restored"] = normalized
    if comment.links:
        state["links_in_the_message"] = comment.links[:10]
    if allowed_domains:
        state["links_this_chat_allows_and_owns"] = allowed_domains[:30]
    if comment.post_text:
        state["post_the_comment_is_under"] = _clip(comment.post_text)
    if comment.reply_to_text:
        state["comment_being_replied_to"] = _clip(comment.reply_to_text)
    return state


KIND_CRITERIA = {
    "question": "Спрашивает о чём-то по делу и ждёт ответа",
    "complaint": "Жалуется, выражает недовольство или претензию",
    "praise": "Хвалит, благодарит, выражает одобрение",
    "suggestion": "Предлагает идею, улучшение или тему для будущих постов",
    "bug": "Указывает на ошибку, неточность или опечатку в посте или продукте",
    "chat": "Обычное общение, эмоции, реакция, шутка, спор с другими",
}
PROFILE_QUESTION = {
    "type": "noul",
    "instructions": (
        "Профиль автора в `author` (имя, ник, описание профиля) выглядит как профиль бота-заманухи: "
        "имя в стиле знакомств или с эмодзи-приманками, ник из случайных цифр, в описании ссылка "
        "или призыв написать в личку?"
    ),
    "criteria": {
        "true": "Приманка: провокационное имя или эмодзи, ник из цифр, ссылка или призыв в описании",
        "false": "Обычный профиль живого человека",
    },
}
ANALYTICS_QUESTIONS = {
    "kind": {
        "type": "choice",
        "instructions": "Какого типа `comment`?",
        "criteria": KIND_CRITERIA,
    },
    "lead": {
        "type": "noul",
        "instructions": "Автор `comment` хочет купить, узнать цену или условия, записаться или просит связаться с ним?",
    },
    "needs_answer": {
        "type": "noul",
        "instructions": "Автор `comment` ждёт ответа от владельца канала?",
    },
    "sentiment": {
        "type": "score",
        "instructions": "Какое отношение к посту или автору канала выражает `comment`?",
        "criteria": ["Негативное", "Нейтральное", "Позитивное"],
    },
}


def build_questions(prohibitions: list[Rule], analytics: bool = False, profile: bool = False) -> dict:
    rule_options = {f"r{i}": rule.text for i, rule in enumerate(prohibitions)}
    rule_options["none"] = "Комментарий не нарушает ни одно правило"
    questions = {
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
    if analytics:
        questions.update(ANALYTICS_QUESTIONS)
    if profile:
        questions["profile_bait"] = PROFILE_QUESTION
    return questions


def parse_verdict(response: dict) -> Verdict:
    answers = response["answers"]
    category = answers["category"]
    rule_choice = answers["rule"]["choice"]
    chosen = Category(category["choice"])
    probabilities = category.get("probabilities") or {}
    if probabilities:
        violation = probabilities.get(Category.SPAM.value, 0.0) + probabilities.get(Category.RULE_VIOLATION.value, 0.0)
    else:
        violation = float(category["confidence"]) if chosen in (Category.SPAM, Category.RULE_VIOLATION) else 0.0
    kind = answers.get("kind", {}).get("choice")
    return Verdict(
        category=chosen,
        confidence=float(category["confidence"]),
        bot_probability=float(answers["is_bot"]["noul"]),
        rule_index=None if rule_choice == "none" else int(rule_choice[1:]),
        input_tokens=int(response.get("usage", {}).get("input_tokens", 0)),
        violation_probability=min(float(violation), 1.0),
        kind=kind if kind in KIND_CRITERIA else None,
        lead=float(answers.get("lead", {}).get("noul", 0.0)),
        needs_answer=float(answers.get("needs_answer", {}).get("noul", 0.0)),
        sentiment=float(answers["sentiment"]["score"]) if "sentiment" in answers else None,
        profile_bait=float(answers.get("profile_bait", {}).get("noul", 0.0)),
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


def avatar_decision(t: Thresholds) -> Decision:
    """A newcomer whose picture belongs to a spammer we already banned: no need to ask the AI."""
    verdict = Verdict(Category.SPAM, 1.0, 1.0)
    reason = "у аккаунта аватарка, как у известного спамера"
    if t.avatar_deletes:
        return Decision(ActionType.DELETE_SILENT, verdict, reason)
    return Decision(ActionType.SEND_TO_REVIEW, verdict, reason)


def decide(
    verdict: Verdict,
    prohibitions: list[Rule],
    t: Thresholds = Thresholds(),
    escalation: bool = True,
    previous_deletions: int = 0,
    newcomer: bool = False,
    image_evidence: bool = False,
) -> Decision:
    reason = explain(verdict, prohibitions)
    if newcomer and verdict.profile_bait > verdict.bot_probability:
        # A lure-like profile makes a spam bot more likely, whatever the comment itself looks like.
        verdict = replace(verdict, bot_probability=verdict.profile_bait)
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
        if image_evidence and action == ActionType.DELETE_AND_BAN:
            # Text read from a picture is noisy: remove the message, but never ban on that alone.
            action, reason = ActionType.DELETE_SILENT, f"{reason} (по тексту на картинке)"
        if action == ActionType.DELETE_AND_MUTE:
            extra["mute_hours"] = MUTE_HOURS
        return Decision(action, verdict, reason, extra)
    if newcomer and verdict.profile_bait >= t.profile_review:
        return Decision(ActionType.SEND_TO_REVIEW, verdict, "подозрительный профиль новичка")
    if verdict.violation_probability >= t.suspect:
        # Jev calls it fine, but not by much: let the admin decide instead of missing a violation.
        return Decision(ActionType.SEND_TO_REVIEW, verdict, "возможное нарушение")
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

    async def check(
        self, comment: Comment, config: ChatConfig, known: str | None = None, now: datetime | None = None
    ) -> Decision:
        """`known` is what the admin (or the bot itself) already decided about this exact text."""
        if config.lockdown or config.blanket:
            return self._blanket(config)
        newcomer = config.profile_check and comment.author.previous_messages < NEWCOMER_MESSAGES
        # Spam bots race for the first place under a new post, so those newcomers are judged strictly.
        rushed = (
            config.first_strict
            and comment.author.previous_messages < NEWCOMER_MESSAGES
            and comment.seconds_after_post is not None
            and comment.seconds_after_post <= FIRST_COMMENT_WINDOW
        )
        night = config.night_active(now)
        thresholds = MODES["strict"] if rushed or night else config.thresholds
        if newcomer and comment.author.profile and comment.author.profile.avatar_match:
            return avatar_decision(thresholds)
        if not needs_check(comment.text + (comment.image_text or ""), comment.links):
            return Decision(ActionType.NONE)
        if known == "allow":
            return Decision(ActionType.NONE, None, "админ уже разрешил такой комментарий")
        if known == "remove":
            return _blanket_decision(Rule("", PROHIBITION, "delete"), "такой комментарий уже удаляли")
        prohibitions = config.prohibitions
        by_links = self._link_decision(comment, config, prohibitions, night)
        if by_links:
            return by_links
        response = await self.jev.ask(
            build_state(comment, config.rules, config.allowed_domains),
            build_questions(prohibitions, config.analytics, profile=newcomer),
        )
        return decide(
            parse_verdict(response),
            prohibitions,
            thresholds,
            config.escalation,
            comment.author.previous_deletions,
            newcomer=newcomer,
            image_evidence=bool(comment.image_text),
        )

    def _link_decision(
        self, comment: Comment, config: ChatConfig, prohibitions: list[Rule], night: bool = False
    ) -> Decision | None:
        """The owner's link lists are enforced by code, so they work the same with or without the AI."""
        links = extract_links(comment.text, comment.links)
        if not links:
            return None
        newcomer = comment.author.previous_messages < NEWCOMER_MESSAGES
        if config.blocked_domains and any_match(links, config.blocked_domains):
            rules, reason, action = [Rule("", PROHIBITION, "delete")], "ссылка из чёрного списка этого чата", "delete"
        elif outside(links, config.allowed_domains) and (
            config.links_mode == "block" or ((config.links_mode == "newcomers" or night) and newcomer)
        ):
            reason = (
                "в этом чате можно публиковать только разрешённые ссылки"
                if config.links_mode == "block"
                else "ночью новичкам нельзя публиковать ссылки"
                if night and config.links_mode != "newcomers"
                else "новичкам пока нельзя публиковать ссылки"
            )
            rules, action = [Rule("", PROHIBITION, "warn")], "warn"
        else:
            return None
        verdict = Verdict(Category.RULE_VIOLATION, 1.0, 0.0, rule_index=0)
        decision = decide(
            verdict, rules, config.thresholds, config.escalation, comment.author.previous_deletions
        )
        note = " (повторные нарушения)" if "повторные" in decision.reason else ""
        return replace(decision, reason=reason + note)

    def fallback(self, comment: Comment, config: ChatConfig) -> Decision:
        """Local protection for the time Jev is down: only well-known spam, only with a lead-away."""
        if config.blanket or config.lockdown:
            return self._blanket(config)
        if looks_like_spam(comment.text, comment.links):
            return _blanket_decision(Rule("", PROHIBITION, "delete"), "спам (ИИ был недоступен)")
        return Decision(ActionType.NONE)

    @staticmethod
    def _blanket(config: ChatConfig) -> Decision:
        if config.lockdown:
            return _blanket_decision(None, "сейчас удаляются все сообщения участников")
        return _blanket_decision(config.blanket, f"правило «{config.blanket.text.rstrip('.!; ')}»")

    @staticmethod
    def flood_decision(minutes: int = FLOOD_MUTE_MINUTES) -> Decision:
        verdict = Verdict(Category.RULE_VIOLATION, 1.0, 0.0)
        return Decision(ActionType.DELETE_AND_MUTE, verdict, "флуд: слишком много сообщений подряд", {"mute_hours": minutes / 60})

    async def parse_rules(self, admin_text: str) -> list[Rule]:
        """Classify every sentence of the admin's text: what it is and what to do about it."""
        sentences = split_rules(admin_text)
        if not sentences:
            return []
        response = await self.jev.ask({"rules_text": admin_text}, build_parse_questions(sentences))
        return parse_rules_response(sentences, response)

    async def tension(self, messages: list[dict]) -> tuple[float, float]:
        """How heated a discussion is: (score 0..2, confidence). `messages` are {"author", "text"} dicts."""
        response = await self.jev.ask({"discussion": messages}, {"tension": TENSION_QUESTION})
        answer = response["answers"]["tension"]
        return float(answer["score"]), float(answer["confidence"])
