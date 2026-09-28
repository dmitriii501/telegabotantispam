"""HTTP server for the Telegram Mini App: static page plus a small JSON API.

Every /api call carries the `initData` Telegram signed for the person who
opened the app (header `X-Init-Data`). The server checks the signature and
then asks Telegram whether that person is an admin of the requested chat, so
there are no passwords and no separate accounts.
"""

import json
import logging
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

from core.jev_client import JevError
from core.models import DELETING_ACTIONS, ActionType
from core.moderation import MODES, ChatConfig
from core.rules import ACTIONS, CONTEXT, EVERYTHING, PERMISSION, PROHIBITION, Rule, rules_to_json
from webapp.auth import validate_init_data

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
MAX_RULES = 30
MAX_RULE_LENGTH = 300
MAX_PARSE_LENGTH = 2000
KINDS = {PROHIBITION, PERMISSION, CONTEXT, EVERYTHING}
USEFUL_MODES = {"digest", "instant", "off"}
BOOL_SETTINGS = ("lockdown", "escalation", "digest", "enabled", "conflicts", "antiflood", "analytics", "observe")
DELETING = {a.value for a in DELETING_ACTIONS}

CSP = (
    "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; object-src 'none'; "
    "frame-ancestors https://web.telegram.org https://webk.telegram.org https://webz.telegram.org"
)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@web.middleware
async def security_headers(request: web.Request, handler):
    response = await handler(request)
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if not request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@web.middleware
async def errors(request: web.Request, handler):
    try:
        return await handler(request)
    except ApiError as e:
        return web.json_response({"error": e.message}, status=e.status)


@web.middleware
async def auth(request: web.Request, handler):
    if request.path.startswith("/api/"):
        user = validate_init_data(request.headers.get("X-Init-Data", ""), request.app["token"])
        if not user:
            raise ApiError(401, "Откройте панель из Telegram")
        request["user"] = user
    return await handler(request)


async def read_json(request: web.Request) -> dict:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(400, "Некорректный запрос")
    if not isinstance(data, dict):
        raise ApiError(400, "Некорректный запрос")
    return data


async def admin_chat(request: web.Request, fresh: bool = False):
    """Chat row for the URL, after checking with Telegram that the caller is its admin."""
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        raise ApiError(400, "Некорректный чат")
    app = request.app
    if not await app["adapter"].is_admin(chat_id, request["user"]["id"], fresh=fresh):
        raise ApiError(403, "Нет доступа к этому чату")
    chat = await app["adapter"].storage.get_chat(chat_id)
    if chat is None:
        raise ApiError(404, "Чат не найден")
    return chat


def validate_rules(data: dict) -> list[Rule]:
    items = data.get("rules")
    if not isinstance(items, list) or len(items) > MAX_RULES:
        raise ApiError(400, f"Правил должно быть не больше {MAX_RULES}")
    rules = []
    for item in items:
        if not isinstance(item, dict):
            raise ApiError(400, "Некорректное правило")
        text = str(item.get("text", "")).strip()
        kind = item.get("kind", PROHIBITION)
        action = item.get("action") or None
        if not text or len(text) > MAX_RULE_LENGTH:
            raise ApiError(400, f"Текст правила: от 1 до {MAX_RULE_LENGTH} символов")
        if kind not in KINDS:
            raise ApiError(400, "Неизвестный тип правила")
        if action is not None and action not in ACTIONS:
            raise ApiError(400, "Неизвестное действие")
        if kind not in (PROHIBITION, EVERYTHING):
            action = None
        rules.append(Rule(text, kind, action))
    return rules


def chat_payload(chat, trusted) -> dict:
    config = ChatConfig.from_row(chat)
    return {
        "chat_id": chat["chat_id"],
        "rules": [asdict(r) for r in config.rules],
        "settings": {
            "mode": config.mode,
            "lockdown": config.lockdown,
            "escalation": config.escalation,
            "conflicts": config.conflicts,
            "antiflood": config.antiflood,
            "analytics": config.analytics,
            "observe": config.observe,
            "digest": bool(chat["digest"]),
            "useful_mode": chat["useful_mode"],
            "enabled": bool(chat["enabled"]),
        },
        "trusted": [{"user_id": t["user_id"], "name": t["name"]} for t in trusted],
    }


# ---------------------------------------------------------------------- handlers


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC / "index.html")


async def list_chats(request: web.Request) -> web.Response:
    adapter = request.app["adapter"]
    user_id = request["user"]["id"]
    chats = []
    for row in await adapter.storage.all_chats():
        if await adapter.is_admin(row["chat_id"], user_id):
            chats.append(
                {
                    "chat_id": row["chat_id"],
                    "title": await adapter.chat_title(row["chat_id"]),
                    "enabled": bool(row["enabled"]),
                }
            )
    return web.json_response({"chats": chats})


async def get_chat(request: web.Request) -> web.Response:
    chat = await admin_chat(request)
    trusted = await request.app["adapter"].storage.list_trusted(chat["chat_id"])
    return web.json_response(chat_payload(chat, trusted))


async def put_rules(request: web.Request) -> web.Response:
    chat = await admin_chat(request, fresh=True)
    rules = validate_rules(await read_json(request))
    raw = ". ".join(r.text for r in rules)
    await request.app["adapter"].storage.save_rules(chat["chat_id"], raw, rules_to_json(rules), request["user"]["id"])
    return web.json_response({"ok": True})


async def put_settings(request: web.Request) -> web.Response:
    chat = await admin_chat(request, fresh=True)
    data = await read_json(request)
    updates = {}
    for key, value in data.items():
        if key == "mode" and value in MODES:
            updates[key] = value
        elif key == "useful_mode" and value in USEFUL_MODES:
            updates[key] = value
        elif key in BOOL_SETTINGS and isinstance(value, bool):
            updates[key] = int(value)
        else:
            raise ApiError(400, f"Некорректная настройка: {key}")
    storage = request.app["adapter"].storage
    for key, value in updates.items():
        await storage.set_setting(chat["chat_id"], key, value)
    return web.json_response({"ok": True})


async def parse_text(request: web.Request) -> web.Response:
    await admin_chat(request)
    text = str((await read_json(request)).get("text", "")).strip()
    if not text or len(text) > MAX_PARSE_LENGTH:
        raise ApiError(400, f"Текст: от 1 до {MAX_PARSE_LENGTH} символов")
    try:
        rules = await request.app["adapter"].moderator.parse_rules(text)
    except JevError:
        raise ApiError(503, "ИИ временно недоступен, попробуйте позже")
    return web.json_response({"rules": [asdict(r) for r in rules]})


async def get_log(request: web.Request) -> web.Response:
    import time

    chat = await admin_chat(request)
    storage = request.app["adapter"].storage
    chat_id = chat["chat_id"]
    stats = await storage.stats(chat_id, int(time.time()) - 24 * 3600)
    entries = await storage.recent_log(chat_id)
    return web.json_response(
        {
            "stats": {
                "checked": sum(v for k, v in stats.items() if k != "tokens"),
                "deleted": sum(stats.get(a, 0) for a in DELETING),
                "pending": await storage.pending_review_count(chat_id),
            },
            "entries": [
                {
                    "id": e["id"],
                    "ts": e["ts"],
                    "user_id": e["user_id"],
                    "user_name": e["user_name"],
                    "text": e["text"][:400],
                    "confidence": e["confidence"],
                    "bot_probability": e["bot_probability"],
                    "action": e["action"],
                    "reason": e["reason"],
                    "feedback": e["feedback"],
                    "executed": bool(e["executed"]),
                }
                for e in entries
            ],
        }
    )


async def get_insights(request: web.Request) -> web.Response:
    import time

    chat = await admin_chat(request)
    try:
        days = min(max(int(request.query.get("days", "7")), 1), 30)
    except ValueError:
        raise ApiError(400, "Некорректный период")
    storage = request.app["adapter"].storage
    chat_id = chat["chat_id"]
    since = int(time.time()) - days * 24 * 3600
    waiting = await storage.unanswered(chat_id, since)
    return web.json_response(
        {
            "days": days,
            "analytics": bool(chat["analytics"]),
            "kinds": await storage.kind_counts(chat_id, since),
            "mood": await storage.sentiment_summary(chat_id, since),
            "waiting": [
                {
                    "id": r["id"],
                    "message_id": r["message_id"],
                    "user_name": r["user_name"],
                    "text": r["text"][:300],
                    "lead": r["lead"] or 0,
                    "needs_answer": r["needs_answer"] or 0,
                    "kind": r["kind"],
                    "ts": r["ts"],
                }
                for r in waiting
            ],
        }
    )


async def log_entry(request: web.Request, chat):
    entry = await request.app["adapter"].storage.get_log(int(request.match_info["log_id"]))
    if entry is None or entry["chat_id"] != chat["chat_id"]:
        raise ApiError(404, "Запись не найдена")
    return entry


async def restore(request: web.Request) -> web.Response:
    chat = await admin_chat(request, fresh=True)
    entry = await log_entry(request, chat)
    if entry["action"] not in DELETING or entry["feedback"]:
        raise ApiError(409, "Эту запись уже нельзя вернуть")
    await request.app["adapter"].restore_comment(entry)
    return web.json_response({"ok": True})


async def review(request: web.Request) -> web.Response:
    chat = await admin_chat(request, fresh=True)
    entry = await log_entry(request, chat)
    if entry["action"] != ActionType.SEND_TO_REVIEW.value or entry["feedback"]:
        raise ApiError(409, "Решение уже принято")
    delete = bool((await read_json(request)).get("delete"))
    result = await request.app["adapter"].resolve_review(entry, delete)
    return web.json_response({"ok": True, "result": result})


async def trust_author(request: web.Request) -> web.Response:
    chat = await admin_chat(request, fresh=True)
    entry = await log_entry(request, chat)
    await request.app["adapter"].storage.set_trusted(chat["chat_id"], entry["user_id"], True, entry["user_name"] or "")
    return web.json_response({"ok": True})


async def untrust(request: web.Request) -> web.Response:
    chat = await admin_chat(request, fresh=True)
    try:
        user_id = int(request.match_info["user_id"])
    except ValueError:
        raise ApiError(400, "Некорректный пользователь")
    await request.app["adapter"].storage.set_trusted(chat["chat_id"], user_id, False)
    return web.json_response({"ok": True})


def create_app(adapter, bot_token: str) -> web.Application:
    app = web.Application(middlewares=[security_headers, errors, auth], client_max_size=64 * 1024)
    app["adapter"] = adapter
    app["token"] = bot_token
    app.router.add_get("/", index)
    app.router.add_static("/static", STATIC)
    app.router.add_get("/api/chats", list_chats)
    app.router.add_get("/api/chat/{chat_id}", get_chat)
    app.router.add_put("/api/chat/{chat_id}/rules", put_rules)
    app.router.add_put("/api/chat/{chat_id}/settings", put_settings)
    app.router.add_post("/api/chat/{chat_id}/parse", parse_text)
    app.router.add_get("/api/chat/{chat_id}/log", get_log)
    app.router.add_get("/api/chat/{chat_id}/insights", get_insights)
    app.router.add_post("/api/chat/{chat_id}/log/{log_id}/restore", restore)
    app.router.add_post("/api/chat/{chat_id}/log/{log_id}/review", review)
    app.router.add_post("/api/chat/{chat_id}/log/{log_id}/trust", trust_author)
    app.router.add_delete("/api/chat/{chat_id}/trusted/{user_id}", untrust)
    return app


async def start_web(adapter, bot_token: str, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(create_app(adapter, bot_token))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("Web panel listening on %s:%s", host, port)
    return runner
