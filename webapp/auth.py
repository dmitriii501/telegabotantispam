"""Validation of Telegram Mini App `initData`.

Telegram signs the data it gives to a Mini App with a key derived from the bot
token, so the server can trust who opened the page without any passwords.
Spec: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE = 24 * 3600


def validate_init_data(init_data: str, bot_token: str, max_age: int = MAX_AGE, now: float | None = None) -> dict | None:
    """Return the Telegram user (dict with `id`, `first_name`, ...) or None if the data is not genuine."""
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
        user = json.loads(pairs["user"])
    except (ValueError, KeyError):
        return None
    if (now if now is not None else time.time()) - auth_date > max_age:
        return None
    return user if isinstance(user, dict) and isinstance(user.get("id"), int) else None
