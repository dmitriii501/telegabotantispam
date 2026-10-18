import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from core.rules import Rule
from core.storage import Storage
from webapp.auth import validate_init_data
from webapp.server import create_app

TOKEN = "123:TEST"
ADMIN = {"id": 42, "first_name": "Дима"}
CHAT = -1001


def sign(user=ADMIN, token=TOKEN, auth_date=None, tamper=False) -> str:
    fields = {"auth_date": str(int(auth_date if auth_date is not None else time.time())), "user": json.dumps(user)}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        fields["user"] = json.dumps({**user, "id": 1})
    return urlencode({**fields, "hash": digest})


def test_valid_init_data():
    assert validate_init_data(sign(), TOKEN) == ADMIN


def test_init_data_rejected_when_tampered_expired_or_wrong_token():
    assert validate_init_data(sign(tamper=True), TOKEN) is None
    assert validate_init_data(sign(auth_date=time.time() - 3 * 24 * 3600), TOKEN) is None
    assert validate_init_data(sign(), "999:OTHER") is None
    assert validate_init_data("", TOKEN) is None
    assert validate_init_data("garbage", TOKEN) is None
    assert validate_init_data("auth_date=1&user=%7B%7D", TOKEN) is None


@pytest.fixture
async def client(tmp_path):
    storage = Storage(str(tmp_path / "w.db"))
    await storage.open()
    adapter = MagicMock()
    adapter.storage = storage
    adapter.is_admin = AsyncMock(side_effect=lambda chat_id, user_id, fresh=False: user_id == 42)
    adapter.chat_title = AsyncMock(return_value="Мой канал")
    adapter.moderator.parse_rules = AsyncMock(return_value=[Rule("Рекламу нельзя — бан", "prohibition", "ban")])
    adapter.restore_comment = AsyncMock()
    adapter.resolve_review = AsyncMock(return_value="🗑 Удалено.")
    await storage.ensure_chat(CHAT)
    async with TestClient(TestServer(create_app(adapter, TOKEN))) as c:
        c.adapter = adapter
        c.storage = storage
        yield c
    await storage.close()


def h(user=ADMIN):
    return {"X-Init-Data": sign(user)}


async def test_api_requires_valid_telegram_signature(client):
    assert (await client.get("/api/chats")).status == 401
    assert (await client.get("/api/chats", headers={"X-Init-Data": sign(tamper=True)})).status == 401


async def test_static_page_and_security_headers(client):
    resp = await client.get("/")
    assert resp.status == 200 and "frame-ancestors" in resp.headers["Content-Security-Policy"]
    assert (await client.get("/static/app.js")).status == 200


async def test_lists_only_chats_where_user_is_admin(client):
    await client.storage.remember_admins(CHAT, {42})
    data = await (await client.get("/api/chats", headers=h())).json()
    assert data["chats"] == [{"chat_id": CHAT, "title": "Мой канал", "enabled": True, "observe": False}]
    other = await (await client.get("/api/chats", headers=h({"id": 7, "first_name": "X"}))).json()
    assert other["chats"] == []


async def test_non_admin_gets_403(client):
    resp = await client.get(f"/api/chat/{CHAT}", headers=h({"id": 7, "first_name": "X"}))
    assert resp.status == 403
    assert (await client.put(f"/api/chat/{CHAT}/rules", json={"rules": []}, headers=h({"id": 7, "first_name": "X"}))).status == 403


async def test_unknown_chat_is_404(client):
    assert (await client.get("/api/chat/555", headers=h())).status == 404


async def test_save_rules_roundtrip_and_first_saver_becomes_owner(client):
    rules = [
        {"text": "Мат можно", "kind": "permission", "action": "ban"},  # action dropped for permissions
        {"text": "Рекламу нельзя", "kind": "prohibition", "action": "ban"},
        {"text": "Политику нельзя", "kind": "prohibition", "action": None},
    ]
    assert (await client.put(f"/api/chat/{CHAT}/rules", json={"rules": rules}, headers=h())).status == 200
    data = await (await client.get(f"/api/chat/{CHAT}", headers=h())).json()
    assert [(r["text"], r["kind"], r["action"]) for r in data["rules"]] == [
        ("Мат можно", "permission", None),
        ("Рекламу нельзя", "prohibition", "ban"),
        ("Политику нельзя", "prohibition", None),
    ]
    assert (await client.storage.get_chat(CHAT))["owner_id"] == 42


@pytest.mark.parametrize(
    "bad",
    [
        {"rules": "x"},
        {"rules": [{"text": "", "kind": "prohibition"}]},
        {"rules": [{"text": "x" * 301, "kind": "prohibition"}]},
        {"rules": [{"text": "x", "kind": "weird"}]},
        {"rules": [{"text": "x", "kind": "prohibition", "action": "nuke"}]},
        {"rules": [{"text": "x"}] * 31},
    ],
)
async def test_invalid_rules_rejected(client, bad):
    assert (await client.put(f"/api/chat/{CHAT}/rules", json=bad, headers=h())).status == 400


async def test_settings_partial_update_and_validation(client):
    ok = await client.put(
        f"/api/chat/{CHAT}/settings",
        json={"mode": "strict", "lockdown": True, "observe": True, "antiflood": False, "analytics": False},
        headers=h(),
    )
    assert ok.status == 200
    s = (await (await client.get(f"/api/chat/{CHAT}", headers=h())).json())["settings"]
    assert s["mode"] == "strict" and s["lockdown"] is True and s["escalation"] is True
    assert s["observe"] is True and s["antiflood"] is False and s["analytics"] is False
    for bad in ({"mode": "insane"}, {"lockdown": "yes"}, {"owner_id": 1}, {"useful_mode": "x"}):
        assert (await client.put(f"/api/chat/{CHAT}/settings", json=bad, headers=h())).status == 400


async def test_parse_text_uses_jev(client):
    data = await (await client.post(f"/api/chat/{CHAT}/parse", json={"text": "рекламу нельзя — бан"}, headers=h())).json()
    assert data["rules"][0]["action"] == "ban"
    assert (await client.post(f"/api/chat/{CHAT}/parse", json={"text": ""}, headers=h())).status == 400


async def add_log(client, action, chat_id=CHAT):
    return await client.storage.add_log(
        chat_id=chat_id, message_id=9, user_id=5, user_name="Вася", text="<b>привет</b>",
        category="spam", confidence=0.9, bot_probability=0.8, action=action, reason="спам", tokens=1,
    )


async def test_log_and_stats(client):
    await add_log(client, "delete_and_ban")
    await add_log(client, "send_to_review")
    await add_log(client, "none")
    data = await (await client.get(f"/api/chat/{CHAT}/log", headers=h())).json()
    assert data["stats"] == {"checked": 3, "deleted": 1, "pending": 1}
    assert [e["action"] for e in data["entries"]] == ["send_to_review", "delete_and_ban"]
    assert data["entries"][0]["text"] == "<b>привет</b>"  # raw text: the page escapes it


async def test_restore_and_review_actions(client):
    deleted = await add_log(client, "delete_silent")
    pending = await add_log(client, "send_to_review")
    assert (await client.post(f"/api/chat/{CHAT}/log/{deleted}/restore", headers=h())).status == 200
    client.adapter.restore_comment.assert_awaited_once()
    # a pending review cannot be "restored", and a deletion cannot be reviewed
    assert (await client.post(f"/api/chat/{CHAT}/log/{pending}/restore", headers=h())).status == 409
    assert (await client.post(f"/api/chat/{CHAT}/log/{deleted}/review", json={"delete": True}, headers=h())).status == 409
    resp = await client.post(f"/api/chat/{CHAT}/log/{pending}/review", json={"delete": True}, headers=h())
    assert (await resp.json())["result"] == "🗑 Удалено."
    client.adapter.resolve_review.assert_awaited_once()
    assert client.adapter.resolve_review.await_args.args[1] is True


async def test_log_entry_of_another_chat_is_404(client):
    await client.storage.ensure_chat(-2002)
    entry = await add_log(client, "delete_silent", chat_id=-2002)
    assert (await client.post(f"/api/chat/{CHAT}/log/{entry}/restore", headers=h())).status == 404


async def test_trust_and_untrust(client):
    entry = await add_log(client, "delete_silent")
    assert (await client.post(f"/api/chat/{CHAT}/log/{entry}/trust", headers=h())).status == 200
    data = await (await client.get(f"/api/chat/{CHAT}", headers=h())).json()
    assert data["trusted"] == [{"user_id": 5, "name": "Вася"}]
    assert (await client.delete(f"/api/chat/{CHAT}/trusted/5", headers=h())).status == 200
    assert (await (await client.get(f"/api/chat/{CHAT}", headers=h())).json())["trusted"] == []


async def test_insights_endpoint(client):
    await client.storage.add_log(
        chat_id=CHAT, message_id=7, user_id=5, user_name="Вася", text="Сколько стоит?", category="normal",
        confidence=0.9, bot_probability=0.1, action="none", reason="", tokens=1,
        kind="question", lead=0.9, needs_answer=0.9, sentiment=1.0,
    )
    data = await (await client.get(f"/api/chat/{CHAT}/insights?days=7", headers=h())).json()
    assert data["kinds"] == {"question": 1}
    assert [w["message_id"] for w in data["waiting"]] == [7]
    assert (await client.get(f"/api/chat/{CHAT}/insights?days=abc", headers=h())).status == 400
    assert (await client.get(f"/api/chat/{CHAT}/insights", headers=h({"id": 7, "first_name": "X"}))).status == 403


async def test_log_marks_dry_run_entries(client):
    entry = await client.storage.add_log(
        chat_id=CHAT, message_id=9, user_id=5, user_name="Вася", text="спам", category="spam", confidence=0.9,
        bot_probability=0.8, action="delete_and_ban", reason="спам", tokens=1, executed=0,
    )
    data = await (await client.get(f"/api/chat/{CHAT}/log", headers=h())).json()
    assert data["entries"][0]["id"] == entry and data["entries"][0]["executed"] is False


async def test_night_mode_settings(client):
    ok = await client.put(
        f"/api/chat/{CHAT}/settings", json={"night_mode": True, "night_from": 23, "night_to": 6}, headers=h()
    )
    assert ok.status == 200
    s = (await (await client.get(f"/api/chat/{CHAT}", headers=h())).json())["settings"]
    assert (s["night_mode"], s["night_from"], s["night_to"]) == (True, 23, 6)
    for bad in ({"night_from": 24}, {"night_to": -1}, {"night_from": "0"}):
        assert (await client.put(f"/api/chat/{CHAT}/settings", json=bad, headers=h())).status == 400


async def test_health_needs_no_login_and_reports_a_stalled_bot(client):
    client.adapter.heartbeat = time.time()
    r = await client.get("/health")
    assert r.status == 200 and (await r.json())["ok"] is True
    client.adapter.heartbeat = time.time() - 600
    assert (await client.get("/health")).status == 503


async def test_chat_payload_has_the_plan_and_a_payment_link(client):
    b = (await (await client.get(f"/api/chat/{CHAT}", headers=h())).json())["billing"]
    assert b["state"] == "trial" and b["pay_url"] is None and b["limit"] > 0 and b["days_left"] >= 1
    client.adapter.bot_username = "DefenceAiBot"
    await client.storage.record_payment("w1", CHAT, 42, "pro", 1, 990)
    b = (await (await client.get(f"/api/chat/{CHAT}", headers=h())).json())["billing"]
    assert b["state"] == "paid" and b["title"] == "Про" and b["pay_url"] == f"https://t.me/DefenceAiBot?start=pay_{CHAT}"
    assert [p["key"] for p in b["plans"]] == ["start", "pro"]


async def test_plan_columns_cannot_be_changed_through_settings(client):
    for key in ("plan", "paid_until", "trial_until"):
        r = await client.put(f"/api/chat/{CHAT}/settings", json={key: 1}, headers=h())
        assert r.status == 400


async def test_bank_notification_route_needs_no_login_but_the_signature_decides(client):
    client.adapter.handle_tbank_notification = AsyncMock(return_value=True)
    r = await client.post("/tbank/notify", json={"OrderId": "x"})
    assert r.status == 200 and await r.text() == "OK"
    client.adapter.handle_tbank_notification = AsyncMock(return_value=False)
    r = await client.post("/tbank/notify", json={"OrderId": "x"})
    assert r.status == 400
    assert (await client.post("/tbank/notify", data="not json")).status == 400
    assert (await client.post("/tbank/notify", json=[1, 2])).status == 400


async def test_pay_button_asks_the_bot_to_write_the_plans(client):
    client.adapter.show_plans = AsyncMock(return_value=True)
    r = await client.post(f"/api/chat/{CHAT}/pay/chat", headers=h())
    assert r.status == 200 and (await r.json()) == {"sent": True}
    client.adapter.show_plans.assert_awaited_once_with(42, CHAT)
    client.adapter.show_plans = AsyncMock(return_value=False)  # the admin never pressed Start
    assert (await (await client.post(f"/api/chat/{CHAT}/pay/chat", headers=h())).json()) == {"sent": False}
    assert (await client.post(f"/api/chat/{CHAT}/pay/chat")).status == 401
    assert (await client.post(f"/api/chat/{CHAT}/pay/chat", headers=h({"id": 7, "first_name": "X"}))).status == 403


# ----------------------------------------------------------- paying from the panel


async def test_billing_payload_lists_plans_prices_and_ways_to_pay(client):
    from core.billing import PLANS, price, price_rub

    client.adapter.tbank = None
    await client.storage.set_email(42, "me@mail.ru")
    b = (await (await client.get(f"/api/chat/{CHAT}", headers=h())).json())["billing"]
    assert b["rub_enabled"] is False and b["email"] == "me@mail.ru"
    assert [t["months"] for t in b["terms"]] == [1, 3, 12] and b["terms"][2]["discount"] == 25
    start = next(p for p in b["plans"] if p["key"] == "start")
    assert start["prices"]["3"] == {"stars": price(PLANS["start"], 3), "rub": price_rub(PLANS["start"], 3)}
    client.adapter.tbank = object()
    assert (await (await client.get(f"/api/chat/{CHAT}", headers=h())).json())["billing"]["rub_enabled"] is True


async def test_stars_payment_gives_a_link_for_the_payment_window(client):
    client.adapter.create_stars_link = AsyncMock(return_value="https://t.me/$abc")
    r = await client.post(f"/api/chat/{CHAT}/pay", json={"method": "stars", "plan": "pro", "months": 3}, headers=h())
    assert r.status == 200 and await r.json() == {"kind": "invoice", "url": "https://t.me/$abc"}
    client.adapter.create_stars_link.assert_awaited_once_with(CHAT, "pro", 3)


async def test_rouble_payment_saves_the_email_and_gives_the_bank_page(client):
    client.adapter.tbank = object()
    client.adapter.make_rub_payment = AsyncMock(return_value="https://securepay/x")
    body = {"method": "rub", "plan": "start", "months": 1, "email": " me@mail.ru "}
    r = await client.post(f"/api/chat/{CHAT}/pay", json=body, headers=h())
    assert r.status == 200 and await r.json() == {"kind": "link", "url": "https://securepay/x"}
    client.adapter.make_rub_payment.assert_awaited_once_with(42, CHAT, "start", 1, "me@mail.ru")
    assert await client.storage.get_email(42) == "me@mail.ru"


async def test_the_pay_endpoint_rejects_bad_requests_and_strangers(client):
    from core.tbank import TBankError

    client.adapter.tbank = object()
    client.adapter.make_rub_payment = AsyncMock()
    ok = {"method": "rub", "plan": "start", "months": 1, "email": "me@mail.ru"}
    for bad in ({**ok, "plan": "gold"}, {**ok, "months": 7}, {**ok, "months": True}, {**ok, "method": "cash"},
                {**ok, "email": "nope"}, {**ok, "email": ""}):
        assert (await client.post(f"/api/chat/{CHAT}/pay", json=bad, headers=h())).status == 400, bad
    assert not client.adapter.make_rub_payment.await_count
    assert (await client.post(f"/api/chat/{CHAT}/pay", json=ok)).status == 401
    assert (await client.post(f"/api/chat/{CHAT}/pay", json=ok, headers=h({"id": 7, "first_name": "X"}))).status == 403
    client.adapter.make_rub_payment = AsyncMock(side_effect=TBankError("терминал не найден"))
    r = await client.post(f"/api/chat/{CHAT}/pay", json=ok, headers=h())
    assert r.status == 502 and "терминал" in (await r.json())["error"]
    client.adapter.tbank = None
    assert (await client.post(f"/api/chat/{CHAT}/pay", json=ok, headers=h())).status == 400


async def test_every_script_of_the_panel_is_served(client):
    page = await (await client.get("/app/")).text()
    scripts = [part.split('"')[0] for part in page.split('src="/static/')[1:]]
    assert {"core.js", "shell.js", "home.js", "settings.js", "pay.js", "rules.js", "insights.js", "log.js", "app.js"} <= set(scripts)
    for name in scripts:
        r = await client.get(f"/static/{name}")
        assert r.status == 200 and "javascript" in r.headers["Content-Type"], name


async def test_the_chat_list_asks_telegram_only_about_the_users_own_chats(client):
    for chat_id in range(-2000, -1900):  # a hundred chats of other people
        await client.storage.ensure_chat(chat_id)
        await client.storage.remember_admins(chat_id, {900 + (-chat_id % 7)})
    await client.storage.remember_admins(CHAT, {42})
    client.adapter.is_admin.reset_mock()
    data = await (await client.get("/api/chats", headers=h())).json()
    assert [c["chat_id"] for c in data["chats"]] == [CHAT]
    assert client.adapter.is_admin.await_count == 1


async def test_owners_and_remembered_admins_are_candidates(client):
    await client.storage.ensure_chat(-3001)
    await client.storage.save_rules(-3001, "", "[]", 42)  # becomes the owner
    ids = {c["chat_id"] for c in await client.storage.chats_of_admin(42)}
    assert ids == {-3001}
    await client.storage.remember_admins(CHAT, {42, 43})
    await client.storage.remember_admins(CHAT, {43})  # replaced, 42 is no longer an admin
    assert {c["chat_id"] for c in await client.storage.chats_of_admin(42)} == {-3001}
    assert {c["chat_id"] for c in await client.storage.chats_of_admin(43)} == {CHAT}
