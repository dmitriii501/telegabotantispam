"""Links in comments: finding them, and per-chat allow and block lists.

A list entry is a domain ("example.com": also matches its subdomains) or one
Telegram channel ("t.me/mychannel": only that channel). Matching is
deterministic, so an owner can rely on it without asking the AI.
"""

import re
from dataclasses import dataclass

TLDS = (
    "com|ru|org|net|io|me|info|xyz|top|site|online|link|cc|club|bet|casino|shop|store|app|dev|co|to|ly|gg|tv|su|"
    "ua|by|kz|biz|pro|cloud|live|fun|vip|win|click|cfd|monster|shop|work|space|tech|one|ink|icu|bio|page|рф"
)
_TAIL = r"[^\s<>()\[\]«»\"']*"
_URL = re.compile(
    rf"(?i)(?:https?://|www\.)[^\s<>()\[\]«»\"']+"
    rf"|(?:t\.me|telegram\.me|telegra\.ph)/{_TAIL}"
    rf"|(?<![\w@./-])[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:{TLDS})(?:/{_TAIL})?(?![\w-])"
)
_ENTRY = re.compile(r"^[a-z0-9а-яё.-]+(?:/[a-z0-9_.+-]+)?$")
MAX_ENTRIES = 50
TELEGRAM_HOSTS = {"t.me", "telegram.me", "telegra.ph"}


@dataclass(frozen=True)
class Link:
    host: str
    path: str = ""

    @property
    def first_segment(self) -> str:
        return self.path.strip("/").split("/", 1)[0].lower()

    def __str__(self) -> str:
        return self.host + (self.path if self.path else "")


def _parse(raw: str) -> Link | None:
    url = raw.strip().rstrip(".,;:!?)»")
    url = re.sub(r"(?i)^https?://", "", url)
    url = re.sub(r"(?i)^www\.", "", url)
    host, _, path = url.partition("/")
    host = host.split("@")[-1].split(":")[0].lower()
    if not host or "." not in host:
        return None
    return Link(host, "/" + path.split("?")[0].split("#")[0] if path else "")


def extract_links(text: str, extra: list[str] | None = None) -> list[Link]:
    """Every link in the text and in the hidden-link descriptions (`«word» → https://...`)."""
    found: list[Link] = []
    for source in [text or "", *(extra or [])]:
        for match in _URL.finditer(source):
            link = _parse(match.group(0))
            if link and link not in found:
                found.append(link)
    return found


def normalize_entry(value: str) -> str | None:
    """Clean up what an owner typed ("https://www.Example.com/") into a list entry, or None if it is not one."""
    entry = re.sub(r"(?i)^https?://", "", value.strip().lower())
    entry = re.sub(r"^www\.", "", entry).rstrip("/")
    if entry.startswith("@"):  # "@mychannel" is understood as t.me/mychannel
        entry = "t.me/" + entry[1:]
    if entry.split("/")[0] in TELEGRAM_HOSTS and "/" in entry:
        entry = entry.split("/")[0] + "/" + entry.split("/")[1]
    return entry if _ENTRY.match(entry) and "." in entry.split("/")[0] else None


def matches(link: Link, entry: str) -> bool:
    host, _, segment = entry.partition("/")
    same_host = link.host == host or link.host.endswith("." + host)
    return same_host and (not segment or link.first_segment == segment)


def any_match(links: list[Link], entries: list[str]) -> bool:
    return any(matches(link, entry) for link in links for entry in entries)


def outside(links: list[Link], allowed: list[str]) -> list[Link]:
    return [link for link in links if not any(matches(link, entry) for entry in allowed)]
