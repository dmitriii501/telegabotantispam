"""Simple local protection used only while Jev is unavailable.

High precision, low recall on purpose: it deletes a comment only when the
text (with look-alike letters restored) contains a well-known spam phrase AND
something that leads away from the chat (a link, a username, "пиши в лс").
Everything else passes, exactly as before, and the owner is told about the outage.
"""

import re

from core.normalizer import normalize

SPAM_PHRASES = (
    "заработок без вложений", "заработок на дому", "пассивный доход", "быстрый заработок",
    "удалённая работа", "удаленная работа", "от 50 000", "от 50к", "в день без опыта",
    "казино", "ставки на спорт", "букмекер", "1xbet", "1хбет", "мелбет", "вулкан", "слоты",
    "закладки", "закладку", "скидка на курс", "инвестиции с гарантией", "гарантированный доход",
    "крипто сигналы", "криптосигналы", "х100", "x100", "продам аккаунт", "взлом", "накрутка",
    "займ без отказа", "кредит без проверки", "порно", "интим услуги", "эскорт",
)
LEADS_AWAY = re.compile(r"(https?://|www\.|t\.me/|@\w{4,}|\+?\d[\d\s\-()]{8,}\d|пиши\w* в (лс|личку|л\.с)|в лс|в личку|(в|смотри\w*) (профил|шапк)|ссылк\w* в (профил|шапк|био))", re.I)


def looks_like_spam(text: str, links: list[str] | None = None) -> bool:
    restored = normalize(text).lower()
    has_phrase = any(phrase in restored for phrase in SPAM_PHRASES)
    leads_away = bool(links) or bool(LEADS_AWAY.search(restored))
    return has_phrase and leads_away
