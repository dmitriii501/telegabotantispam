"""Check that the newest database backup can really be restored.

A backup nobody ever opened is a hope, not a backup. This restores the newest file into a temporary folder, runs
SQLite's integrity check, lets the bot's own code open it (so the schema upgrade works on it too) and compares it
with the live database. Exit code 0 when everything is fine, 1 with the list of problems otherwise.

    python -m scripts.verify_backup --dest /var/backups/defenceai --live /opt/defenceai/bot.db
"""

import argparse
import asyncio
import gzip
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from core.storage import Storage

REQUIRED_TABLES = ("chats", "log", "users", "payments", "usage", "trusted", "examples")
MONEY_TABLES = ("payments",)  # a backup that lost payments is a bad backup, whatever its age


def newest_backup(dest: Path) -> Path | None:
    files = sorted(dest.glob("bot-*.db.gz"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _count(db: sqlite3.Connection, table: str) -> int:
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 (table names are constants)


async def _opens_with_the_app(path: Path) -> str | None:
    """The bot's own storage opens the copy (schema upgrades run on it). None when fine, else the problem."""
    storage = Storage(str(path))
    try:
        await storage.open()
        await storage.all_chats()
    except Exception as e:  # any failure here means the restored file would not start the bot
        return f"the bot cannot open the restored copy: {e}"
    finally:
        await storage.close()
    return None


def verify(dest: Path, live: Path | None = None, max_age_hours: float = 36.0) -> tuple[list[str], dict]:
    """Returns (problems, facts). No problems means the backup is restorable."""
    problems: list[str] = []
    facts: dict = {}
    backup = newest_backup(dest)
    if backup is None:
        return [f"no backups in {dest}"], facts
    facts["file"] = backup.name
    age = (time.time() - backup.stat().st_mtime) / 3600
    facts["age_hours"] = round(age, 1)
    facts["size_mb"] = round(backup.stat().st_size / 1e6, 2)
    if age > max_age_hours:
        problems.append(f"the newest backup is {age:.0f} hours old (limit {max_age_hours:.0f})")
    with tempfile.TemporaryDirectory(prefix="verify-backup-") as tmp:
        restored = Path(tmp) / "restored.db"
        try:
            with gzip.open(backup, "rb") as src, open(restored, "wb") as out:
                shutil.copyfileobj(src, out)
        except (OSError, EOFError) as e:
            return [*problems, f"the archive does not unpack: {e}"], facts
        try:
            db = sqlite3.connect(f"file:{restored}?mode=ro", uri=True)
            verdict = db.execute("PRAGMA integrity_check").fetchone()[0]
            facts["integrity"] = verdict
            if verdict != "ok":
                problems.append(f"SQLite integrity check: {verdict}")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            facts["tables"] = len(tables)
            for table in REQUIRED_TABLES:
                if table not in tables:
                    problems.append(f"table {table} is missing")
            counts = {t: _count(db, t) for t in ("chats", "log", "payments") if t in tables}
            facts["rows"] = counts
            if counts.get("chats", 0) == 0:
                problems.append("the backup has no chats: it was made from an empty database")
            db.close()
        except sqlite3.DatabaseError as e:
            return [*problems, f"the restored file is not a database: {e}"], facts
        if not problems or all("hours old" in p for p in problems):
            opened = asyncio.run(_opens_with_the_app(restored))
            if opened:
                problems.append(opened)
        if live is not None and live.exists():
            try:
                lv = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
                for table in MONEY_TABLES:
                    live_n, backup_n = _count(lv, table), counts.get(table, 0)
                    facts[f"{table}_live"] = live_n
                    # newer payments than the backup are normal (a day passed); a backup with MORE than live is a restore trap
                    if backup_n > live_n:
                        problems.append(f"the backup holds more {table} rows ({backup_n}) than the live database ({live_n})")
                lv.close()
            except sqlite3.DatabaseError as e:
                problems.append(f"the live database cannot be read for comparison: {e}")
    return problems, facts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dest", default="/var/backups/defenceai", type=Path)
    parser.add_argument("--live", default=None, type=Path)
    parser.add_argument("--max-age-hours", default=36.0, type=float)
    args = parser.parse_args(argv)
    problems, facts = verify(args.dest, args.live, args.max_age_hours)
    summary = ", ".join(f"{k}={v}" for k, v in facts.items())
    if problems:
        print("BACKUP PROBLEM: " + "; ".join(problems))
        print(summary)
        return 1
    print("backup restorable: " + summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
