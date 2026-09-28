"""SQLite storage: chat settings, moderation log, per-user counters, appeals."""

import time

import aiosqlite

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
"""

DAY = 24 * 3600


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
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

    async def set_pending_rules(self, chat_id: int, text: str) -> None:
        await self.ensure_chat(chat_id)
        await self.db.execute("UPDATE chats SET pending_rules = ? WHERE chat_id = ?", (text, chat_id))
        await self.db.commit()

    async def confirm_rules(self, chat_id: int, owner_id: int) -> bool:
        chat = await self.get_chat(chat_id)
        if not chat or chat["pending_rules"] is None:
            return False
        await self.db.execute(
            "UPDATE chats SET rules = pending_rules, pending_rules = NULL, owner_id = ? WHERE chat_id = ?",
            (owner_id, chat_id),
        )
        await self.db.commit()
        return True

    async def discard_pending_rules(self, chat_id: int) -> None:
        await self.db.execute("UPDATE chats SET pending_rules = NULL WHERE chat_id = ?", (chat_id,))
        await self.db.commit()

    async def set_enabled(self, chat_id: int, enabled: bool) -> None:
        await self.ensure_chat(chat_id)
        await self.db.execute("UPDATE chats SET enabled = ? WHERE chat_id = ?", (int(enabled), chat_id))
        await self.db.commit()

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
        await self.db.execute("UPDATE log SET feedback = ? WHERE id = ?", (feedback, log_id))
        await self.db.commit()

    async def stats(self, chat_id: int, since: int) -> dict[str, int]:
        async with self.db.execute(
            "SELECT action, COUNT(*) AS n, SUM(tokens) AS t FROM log WHERE chat_id = ? AND ts >= ? GROUP BY action",
            (chat_id, since),
        ) as cur:
            rows = await cur.fetchall()
        result = {row["action"]: row["n"] for row in rows}
        result["tokens"] = sum(row["t"] or 0 for row in rows)
        return result

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
