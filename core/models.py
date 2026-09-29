"""Platform-independent data types shared by the core and all adapters."""

from dataclasses import dataclass, field
from enum import Enum


@dataclass
class Profile:
    """What is known about a newcomer's profile; None fields mean Telegram did not tell us."""

    bio: str | None = None
    has_photo: bool | None = None
    avatar_match: bool = False  # the picture looks like one used by a spammer we already banned


@dataclass
class Author:
    user_id: int
    display_name: str
    username: str | None = None
    is_premium: bool = False
    # Filled in from storage by the adapter before moderation.
    previous_messages: int = 0
    previous_deletions: int = 0
    profile: Profile | None = None


@dataclass
class Comment:
    chat_id: int
    message_id: int
    text: str
    author: Author
    platform: str = "telegram"
    # Context the rules may refer to: "не по теме поста", "оскорбляет собеседника".
    post_text: str | None = None
    reply_to_text: str | None = None
    # Links the text hides or shows: «слово» → https://..., or a plain url.
    links: list[str] = field(default_factory=list)
    # How long after the channel post this comment was written (None: unknown).
    seconds_after_post: int | None = None
    # Text read from a picture or sticker: noisy, so it is a hint and never grounds for an automatic ban.
    image_text: str | None = None


class Category(str, Enum):
    SPAM = "spam"
    RULE_VIOLATION = "rule_violation"
    USEFUL = "useful"
    NORMAL = "normal"


@dataclass
class Verdict:
    category: Category
    confidence: float
    bot_probability: float
    # Index into the prohibitions list of the chat config, or None when no rule applies.
    rule_index: int | None = None
    input_tokens: int = 0
    # Chance that the comment breaks the rules, from Jev's probabilities (spam + violation).
    violation_probability: float = 0.0
    # Analytics: filled in when the request carried the extra questions.
    kind: str | None = None
    lead: float = 0.0  # wants to buy, asks the price, asks to be contacted
    needs_answer: float = 0.0  # the author waits for a reply from the owner
    sentiment: float | None = None  # 0 negative .. 2 positive
    profile_bait: float = 0.0  # the profile of a newcomer looks like that of a lure bot


class ActionType(str, Enum):
    NONE = "none"
    DELETE_AND_BAN = "delete_and_ban"  # obvious spam bot: silent
    DELETE_AND_MUTE = "delete_and_mute"  # repeat offender or rule says "mute"
    DELETE_SILENT = "delete_silent"  # violation, author looks like a bot or unsure
    DELETE_AND_EXPLAIN = "delete_and_explain"  # violation by a real person
    SEND_TO_REVIEW = "send_to_review"  # model is not sure: ask the admin
    FORWARD_USEFUL = "forward_useful"  # useful comment: show to the owner


DELETING_ACTIONS = (
    ActionType.DELETE_AND_BAN,
    ActionType.DELETE_AND_MUTE,
    ActionType.DELETE_SILENT,
    ActionType.DELETE_AND_EXPLAIN,
)


@dataclass
class Decision:
    action: ActionType
    verdict: Verdict | None = None
    reason: str = ""  # human-readable reason, built from templates
    extra: dict = field(default_factory=dict)
