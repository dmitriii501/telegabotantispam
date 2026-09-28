"""Platform-independent data types shared by the core and all adapters."""

from dataclasses import dataclass, field
from enum import Enum


@dataclass
class Author:
    user_id: int
    display_name: str
    username: str | None = None
    is_premium: bool = False
    # Filled in from storage by the adapter before moderation.
    previous_messages: int = 0
    previous_deletions: int = 0


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
