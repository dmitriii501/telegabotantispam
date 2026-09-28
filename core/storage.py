"""SQLite storage: chat settings, moderation log, per-user counters, appeals, posts."""

import time

import aiosqlite

from core.normalizer import example_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    owner_id INTEGER,
    rules TEXT NOT NULL DEFAULT '',
    pending_rules TEXT,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER,
    user_id INTEGER,
    messages INTEGER NOT NULL DEFAULT 0,
    deletions INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT,
    text TEXT NOT NULL,
    category TEXT,
    confidence REAL,
    bot_probability REAL,
    action TEXT NOT NULL,
    reason TEXT,
    tokens INTEGER NOT NULL DEFAULT 0,
    -- set when an admin overrides the bot: 'not_spam' or 'confirmed'
    feedback TEXT
);
CREATE TABLE IF NOT EXISTS appeals (
    user_id INTEGER NOT NULL,
    log_id INTEGER NOT NULL,
    ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS examples (
    chat_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    kind TEXT NOT NULL,  -- 'allow' or 'remove'
    ts INTEGER NOT NULL,
    PRIMARY KEY (chat_id, key)
);
CREATE TABLE IF NOT EXISTS trusted (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (chat_id, user_id)
);
"""

# Columns added after the first release: (name, definition). Added to old databases on open.
CHAT_COLUMNS = [
    ("rules_json", "TEXT"),
    ("pending_json", "TEXT"),
    ("mode", "TEXT NOT NULL DEFAULT 'normal'"),
    ("lockdown", "INTEGER NOT NULL DEFAULT 0"),
    ("escalation", "INTEGER NOT NULL DEFAULT 1"),
    ("digest", "INTEGER NOT NULL DEFAULT 1"),
    ("useful_mode", "TEXT NOT NULL DEFAULT 'digest'"),
    ("digest_last", "TEXT"),
    ("conflicts", "INTEGER NOT NULL DEFAULT 1"),
]
SETTINGS = {name for name, _ in CHAT_COLUMNS} - {"rules_json", "pending_json", "digest_last"} | {"enabled"}

DAY = 24 * 3600
DELETING = ("delete_and_ban", "delete_and_mute", "delete_silent", "delete_and_explain")


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        async with self.db.execute("PRAGMA table_info(chats)") as cur:
            existing = {row["name"] for row in await cur.fetchall()}
        for name, definition in CHAT_COLUMNS:
            if name not in existing:
                await self.db.execute(f"ALTER TABLE chats ADD COLUMN {name} {definition}")
        async with self.db.execute("PRAGMA table_info(trusted)") as cur:
            if "name" not in {row["name"] for row in await cur.fetchall()}:
                await self.db.execute("ALTER TABLE trusted ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    # --- chats ---

    async def get_chat(self, chat_id: int) -> aiosqlite.Row | None:
        async with self.db.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)) as cur:
            return await cur.fetchone()

    async def ensure_chat(self, chat_id: int) -> None:
        await self.db.execute("INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", (chat_id,))
        await self.db.commit()

    async def set_pending_rules(self, chat_id: int, text: str, rules_json: str) -> None:
        await self.ensure_chat(chat_id)
        await self.db.execute(
            "UPDATE chats SET pending_rules = ?, pending_json = ? WHERE chat_id = ?", (text, rules_json, chat_id)
        )
        await self.db.commit()

    async def confirm_rules(self, chat_id: int, owner_id: int) -> bool:
        chat = await self.get_chat(chat_id)
        if not chat or chat["pending_rules"] is None:
            return False
        await self.db.execute(
            """UPDATE chats SET rules = pending_rules, rules_json = pending_json,
               pending_rules = NULL, pending_json = NULL, owner_id = ? WHERE chat_id = ?""",
            (owner_id, chat_id),
        )
        await self.db.commit()
        return True

    async def discard_pending_rules(self, chat_id: int) -> None:
        await self.db.execute(
            "UPDATE chats SET pending_rules = NULL, pending_json = NULL WHERE chat_id = ?", (chat_id,)
        )
        await self.db.commit()

    async def set_setting(self, chat_id: int, key: str, value) -> None:
        if key not in SETTINGS:
            raise ValueError(f"unknown setting {key!r}")
        await self.ensure_chat(chat_id)
        await self.db.execute(f"UPDATE chats SET {key} = ? WHERE chat_id = ?", (value, chat_id))
        await self.db.commit()

    async def all_chats(self) -> list[aiosqlite.Row]:
        async with self.db.execute("SELECT * FROM chats") as cur:
            return await cur.fetchall()

    async def save_rules(self, chat_id: int, raw_text: str, rules_json: str, owner_id: int) -> None:
        """Save rules straight from the web panel; the first admin to save becomes the owner."""
        await self.ensure_chat(chat_id)
        await self.db.execute(
            "UPDATE chats SET rules = ?, rules_json = ?, owner_id = COALESCE(owner_id, ?) WHERE chat_id = ?",
            (raw_text, rules_json, owner_id, chat_id),
        )
        await self.db.commit()

    async def set_enabled(self, chat_id: int, enabled: bool) -> None:
        await self.set_setting(chat_id, "enabled", int(enabled))

    # --- users ---

    async def user_counters(self, chat_id: int, user_id: int) -> tuple[int, int]:
        async with self.db.execute(
            "SELECT messages, deletions FROM users WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        ) as cur:
            row = await cur.fetchone()
        return (row["messages"], row["deletions"]) if row else (0, 0)

    async def count_message(self, chat_id: int, user_id: int, deleted: bool) -> None:
        await self.db.execute(
            """INSERT INTO users (chat_id, user_id, messages, deletions) VALUES (?, ?, 1, ?)
               ON CONFLICT (chat_id, user_id) DO UPDATE SET
               messages = messages + 1, deletions = deletions + excluded.deletions""",
            (chat_id, user_id, int(deleted)),
        )
        await self.db.commit()

    async def reset_deletions(self, chat_id: int, user_id: int) -> None:
        await self.db.execute(
            "UPDATE users SET deletions = MAX(deletions - 1, 0) WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )
        await self.db.commit()

    # --- trusted users ---

    async def set_trusted(self, chat_id: int, user_id: int, trusted: bool, name: str = "") -> None:
        if trusted:
            await self.db.execute(
                "INSERT OR REPLACE INTO trusted (chat_id, user_id, name) VALUES (?, ?, ?)", (chat_id, user_id, name)
            )
        else:
            await self.db.execute("DELETE FROM trusted WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        await self.db.commit()

    async def list_trusted(self, chat_id: int) -> list[aiosqlite.Row]:
        async with self.db.execute("SELECT user_id, name FROM trusted WHERE chat_id = ?", (chat_id,)) as cur:
            return await cur.fetchall()

    async def is_trusted(self, chat_id: int, user_id: int) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM trusted WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        ) as cur:
            return await cur.fetchone() is not None

    # --- channel posts (context for comments) ---

    async def add_post(self, chat_id: int, message_id: int, text: str) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO posts (chat_id, message_id, text) VALUES (?, ?, ?)",
            (chat_id, message_id, text[:1500]),
        )
        await self.db.commit()

    async def get_post(self, chat_id: int, message_id: int) -> str | None:
        async with self.db.execute(
            "SELECT text FROM posts WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)
        ) as cur:
            row = await cur.fetchone()
        return row["text"] if row else None

    # --- log ---

    async def add_log(self, **fields) -> int:
        fields.setdefault("ts", int(time.time()))
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = await self.db.execute(f"INSERT INTO log ({cols}) VALUES ({marks})", tuple(fields.values()))
        await self.db.commit()
        return cur.lastrowid

    async def get_log(self, log_id: int) -> aiosqlite.Row | None:
        async with self.db.execute("SELECT * FROM log WHERE id = ?", (log_id,)) as cur:
            return await cur.fetchone()

    async def set_feedback(self, log_id: int, feedback: str) -> None:
        """Record the admin's decision; it is also remembered for identical comments later."""
        await self.db.execute("UPDATE log SET feedback = ? WHERE id = ?", (feedback, log_id))
        await self.db.commit()
        entry = await self.get_log(log_id)
        key = example_key(entry["text"]) if entry else None
        if key and feedback in ("not_spam", "confirmed"):
            await self.add_example(entry["chat_id"], key, "allow" if feedback == "not_spam" else "remove")

    async def add_example(self, chat_id: int, key: str, kind: str) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO examples (chat_id, key, kind, ts) VALUES (?, ?, ?, ?)",
            (chat_id, key, kind, int(time.time())),
        )
        await self.db.commit()

    async def example_kind(self, chat_id: int, key: str) -> str | None:
        async with self.db.execute("SELECT kind FROM examples WHERE chat_id = ? AND key = ?", (chat_id, key)) as cur:
            row = await cur.fetchone()
        return row["kind"] if row else None

    async def stats(self, chat_id: int, since: int) -> dict[str, int]:
        async with self.db.execute(
            "SELECT action, COUNT(*) AS n, SUM(tokens) AS t FROM log WHERE chat_id = ? AND ts >= ? GROUP BY action",
            (chat_id, since),
        ) as cur:
            rows = await cur.fetchall()
        result = {row["action"]: row["n"] for row in rows}
        result["tokens"] = sum(row["t"] or 0 for row in rows)
        return result

    async def recent_log(self, chat_id: int, limit: int = 40) -> list[aiosqlite.Row]:
        """Deleted comments and comments waiting for the admin, newest first."""
        actions = (*DELETING, "send_to_review")
        marks = ", ".join("?" for _ in actions)
        async with self.db.execute(
            f"SELECT * FROM log WHERE chat_id = ? AND action IN ({marks}) ORDER BY id DESC LIMIT ?",
            (chat_id, *actions, limit),
        ) as cur:
            return await cur.fetchall()

    async def pending_review_count(self, chat_id: int) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM log WHERE chat_id = ? AND action = 'send_to_review' AND feedback IS NULL",
            (chat_id,),
        ) as cur:
            (n,) = await cur.fetchone()
        return n

    async def top_reasons(self, chat_id: int, since: int, limit: int = 3) -> list[tuple[str, int]]:
        marks = ", ".join("?" for _ in DELETING)
        async with self.db.execute(
            f"""SELECT reason, COUNT(*) AS n FROM log
                WHERE chat_id = ? AND ts >= ? AND action IN ({marks}) AND reason != ''
                GROUP BY reason ORDER BY n DESC LIMIT ?""",
            (chat_id, since, *DELETING, limit),
        ) as cur:
            return [(row["reason"], row["n"]) for row in await cur.fetchall()]

    async def useful_comments(self, chat_id: int, since: int, limit: int = 5) -> list[aiosqlite.Row]:
        async with self.db.execute(
            """SELECT * FROM log WHERE chat_id = ? AND ts >= ? AND action = 'forward_useful'
               ORDER BY confidence DESC, id DESC LIMIT ?""",
            (chat_id, since, limit),
        ) as cur:
            return await cur.fetchall()

    async def digest_chats(self) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM chats WHERE digest = 1 AND owner_id IS NOT NULL AND enabled = 1"
        ) as cur:
            return await cur.fetchall()

    async def mark_digest_sent(self, chat_id: int, day: str) -> None:
        await self.db.execute("UPDATE chats SET digest_last = ? WHERE chat_id = ?", (day, chat_id))
        await self.db.commit()

    # --- appeals ---

    async def can_appeal(self, user_id: int) -> bool:
        async with self.db.execute(
            "SELECT COUNT(*) FROM appeals WHERE user_id = ? AND ts >= ?", (user_id, int(time.time()) - DAY)
        ) as cur:
            (n,) = await cur.fetchone()
        return n == 0

    async def add_appeal(self, user_id: int, log_id: int) -> None:
        await self.db.execute(
            "INSERT INTO appeals (user_id, log_id, ts) VALUES (?, ?, ?)", (user_id, log_id, int(time.time()))
        )
        await self.db.commit()
