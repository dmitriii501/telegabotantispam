"""The backup really restores: made by the real backup script, checked by the real verifier."""

import asyncio
import gzip
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.storage import Storage
from scripts.verify_backup import main, newest_backup, verify

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
async def installation(tmp_path):
    """A folder laid out like the server's: .venv/bin/python, bot.db with data, a backup folder."""
    app = tmp_path / "app"
    (app / ".venv" / "bin").mkdir(parents=True)
    (app / ".venv" / "bin" / "python").symlink_to(sys.executable)
    storage = Storage(str(app / "bot.db"))
    await storage.open()
    await storage.ensure_chat(-1001)
    await storage.record_payment("tg-1", -1001, 5, "start", 1, 399)
    await storage.add_log(chat_id=-1001, message_id=1, user_id=5, text="x", action="none", tokens=900)
    await storage.add_usage(-1001)
    await storage.close()
    dest = tmp_path / "backups"
    return app, dest


def run_backup(app, dest):
    env = {**os.environ, "APP_DIR": str(app), "DEST": str(dest)}
    return subprocess.run(["bash", str(ROOT / "deploy" / "backup.sh")], env=env, capture_output=True, text=True, check=True)


async def test_a_backup_made_by_the_script_restores(installation):
    app, dest = installation
    run_backup(app, dest)
    problems, facts = await asyncio.to_thread(verify, dest, app / "bot.db")
    assert problems == [] and facts["integrity"] == "ok"
    assert facts["rows"] == {"chats": 1, "log": 1, "payments": 1}


async def test_the_command_line_says_so_and_sets_the_exit_code(installation, capsys):
    app, dest = installation
    run_backup(app, dest)
    assert await asyncio.to_thread(main, ["--dest", str(dest), "--live", str(app / "bot.db")]) == 0
    assert "backup restorable" in capsys.readouterr().out
    assert await asyncio.to_thread(main, ["--dest", str(dest / "nothing")]) == 1
    assert "BACKUP PROBLEM" in capsys.readouterr().out


async def test_no_backup_is_a_problem(tmp_path):
    problems, _ = await asyncio.to_thread(verify, tmp_path)
    assert problems and "no backups" in problems[0]


async def test_a_stale_backup_is_a_problem(installation):
    app, dest = installation
    run_backup(app, dest)
    backup = newest_backup(dest)
    old = time.time() - 3 * 24 * 3600
    os.utime(backup, (old, old))
    problems, _ = await asyncio.to_thread(lambda: verify(dest, max_age_hours=36))
    assert any("hours old" in p for p in problems)


async def test_a_damaged_archive_is_a_problem(installation):
    app, dest = installation
    run_backup(app, dest)
    backup = newest_backup(dest)
    data = backup.read_bytes()
    backup.write_bytes(data[: len(data) // 2])  # cut in half
    problems, _ = await asyncio.to_thread(verify, dest)
    assert problems and "does not unpack" in problems[0]


async def test_a_file_that_is_not_a_database_is_a_problem(tmp_path):
    dest = tmp_path / "b"
    dest.mkdir()
    with gzip.open(dest / "bot-2026-01-01.db.gz", "wb") as f:
        f.write(b"this is not a database at all" * 50)
    problems, _ = await asyncio.to_thread(lambda: verify(dest, max_age_hours=10**6))
    assert problems and "not a database" in problems[0]


async def test_a_copy_of_an_empty_database_is_a_problem(tmp_path):
    empty = Storage(str(tmp_path / "empty.db"))
    await empty.open()
    await empty.close()
    dest = tmp_path / "b"
    dest.mkdir()
    with open(tmp_path / "empty.db", "rb") as src, gzip.open(dest / "bot-2026-01-01.db.gz", "wb") as out:
        shutil.copyfileobj(src, out)
    problems, _ = await asyncio.to_thread(lambda: verify(dest, max_age_hours=10**6))
    assert any("no chats" in p for p in problems)


async def test_missing_tables_are_reported(tmp_path):
    db = sqlite3.connect(tmp_path / "odd.db")
    db.execute("CREATE TABLE chats (chat_id INTEGER)")
    db.execute("INSERT INTO chats VALUES (1)")
    db.commit()
    db.close()
    dest = tmp_path / "b"
    dest.mkdir()
    with open(tmp_path / "odd.db", "rb") as src, gzip.open(dest / "bot-2026-01-01.db.gz", "wb") as out:
        shutil.copyfileobj(src, out)
    problems, _ = await asyncio.to_thread(lambda: verify(dest, max_age_hours=10**6))
    assert any("table payments is missing" in p for p in problems)


async def test_a_backup_with_more_payments_than_the_live_database_is_a_trap(installation):
    app, dest = installation
    run_backup(app, dest)
    live = sqlite3.connect(app / "bot.db")
    live.execute("DELETE FROM payments")  # the live database lost its payments after the backup
    live.commit()
    live.close()
    problems, _ = await asyncio.to_thread(verify, dest, app / "bot.db")
    assert any("more payments" in p for p in problems)


async def test_an_old_backup_without_the_newer_tables_still_opens_with_the_app(tmp_path):
    """A backup from before this version has no usage table: the bot's own migration has to cope."""
    db = Storage(str(tmp_path / "now.db"))
    await db.open()
    await db.ensure_chat(-5)
    await db.close()
    raw = sqlite3.connect(tmp_path / "now.db")
    raw.execute("DROP TABLE usage")
    raw.commit()
    raw.close()
    reopened = Storage(str(tmp_path / "now.db"))
    await reopened.open()  # recreates what is missing
    assert await reopened.ai_checks(-5, 0) == 0
    await reopened.close()
