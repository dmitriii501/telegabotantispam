"""Undo common spam obfuscation: mixed alphabets, digits for letters, hidden chars.

The model gets both the original and the normalized text, so normalization only
has to help, never to be perfect.
"""

import re
import unicodedata

# Latin and Greek look-alikes of Cyrillic letters.
_HOMOGLYPHS = {
    "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м",
    "o": "о", "p": "р", "t": "т", "x": "х", "y": "у", "u": "и", "n": "п",
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
    "α": "а", "ο": "о", "ρ": "р", "ε": "е", "κ": "к", "τ": "т",
}

# Digits and symbols used instead of Cyrillic letters inside words.
_LEETSPEAK = {
    "0": "о", "3": "з", "4": "ч", "6": "б", "@": "а", "$": "с",
}

_INVISIBLE = re.compile("[\u200b-\u200f\u2060-\u2064\ufeff\u00ad\u034f\u180e]")
_CYRILLIC = re.compile("[а-яё]", re.IGNORECASE)
_WORD = re.compile(r"[\w@$]+", re.UNICODE)
# "к а з и н о" or "к.а.з.и.н.о" -> "казино": 4+ single letters split by one separator.
_SPACED = re.compile(r"(?<!\w)(?:\w[ .\-_*]){3,}\w(?!\w)", re.UNICODE)


def _fix_word(word: str) -> str:
    if not _CYRILLIC.search(word):
        # Pure Latin/digit words are left alone: links, usernames, "iPhone 15".
        return word
    out = []
    for i, ch in enumerate(word):
        if ch in _LEETSPEAK:
            # Only between letters: "к0кс" -> "кокс", but "50к" stays "50к".
            prev_ok = i > 0 and word[i - 1].isalpha()
            next_ok = i + 1 < len(word) and word[i + 1].isalpha()
            out.append(_LEETSPEAK[ch] if prev_ok and next_ok else ch)
        else:
            out.append(_HOMOGLYPHS.get(ch, ch))
    return "".join(out)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE.sub("", text)
    text = _SPACED.sub(lambda m: re.sub(r"[ .\-_*]", "", m.group(0)), text)
    return _WORD.sub(lambda m: _fix_word(m.group(0)), text)


def is_obfuscated(text: str) -> bool:
    """True when normalization changed something: itself a spam signal."""
    return normalize(text) != unicodedata.normalize("NFKC", text)
