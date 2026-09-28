"""Channel rules written by the admin in plain language.

Jev does not generate text, so the rules are never "parsed" into a config.
The admin's own sentences are kept as a list: Jev picks which one a comment
breaks, and the bot quotes that sentence back in explanations.
"""

import re

# Common topics the bot asks Jev about to show the admin how it understood the rules.
# key -> (question for Jev, label for the admin)
TOPICS: dict[str, tuple[str, str]] = {
    "profanity": ("Разрешён ли мат согласно `rules`?", "Мат"),
    "ads": ("Разрешена ли реклама других каналов, товаров и услуг согласно `rules`?", "Реклама"),
    "links": ("Разрешены ли ссылки согласно `rules`?", "Ссылки"),
    "insults": ("Разрешены ли оскорбления других людей согласно `rules`?", "Оскорбления"),
    "politics": ("Разрешено ли обсуждение политики согласно `rules`?", "Политика"),
}
# Separate question: does the text mention the topic at all? Otherwise "not specified".
MENTION_QUESTION = "Говорится ли в `rules` что-либо про тему «{label}»?"

# Always on, whatever the admin wrote.
BASE_RULE = "Запрещён спам: реклама заработка, казино, наркотиков, мошенничество, массовые рассылки."

_SPLIT = re.compile(r"[\n;.!]+|,\s*(?=[^,]*\b(?:можно|нельзя|запрещ|разреш))", re.IGNORECASE)


def split_rules(text: str) -> list[str]:
    """Split free text into separate rules: 'мат можно, рекламу нельзя' -> 2 rules."""
    parts = [p.strip(" -•*\t") for p in _SPLIT.split(text)]
    return [p for p in parts if len(p) >= 3]


def rule_list(admin_text: str) -> list[str]:
    return [BASE_RULE, *split_rules(admin_text)]
