"""The public pages: landing, offer, privacy."""

from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from core.billing import PLANS, REFUND_DAYS, TRIAL_DAYS
from core.storage import Storage
from webapp import legal
from webapp.server import create_app

SELLER = {"LEGAL_NAME": "ИП Иванов Иван Иванович", "LEGAL_INN": "123456789012", "LEGAL_OGRNIP": "304500116000157",
          "LEGAL_EMAIL": "help@example.ru", "SUPPORT_CONTACT": "@support"}


@pytest.fixture
async def site(tmp_path):
    storage = Storage(str(tmp_path / "l.db"))
    await storage.open()
    adapter = MagicMock()
    adapter.storage = storage
    adapter.bot_username = "DefenceAiBot"
    async with TestClient(TestServer(create_app(adapter, "123:TEST"))) as c:
        yield c
    await storage.close()


def fill(monkeypatch):
    for key, value in SELLER.items():
        monkeypatch.setenv(key, value)


async def test_the_pages_are_public_and_the_panel_moved_to_app(site, monkeypatch):
    fill(monkeypatch)
    for path, needle in [("/", "ИИ-модератор"), ("/offer", "Публичная оферта"), ("/privacy", "Политика конфиденциальности")]:
        r = await site.get(path)
        assert r.status == 200 and "text/html" in r.headers["Content-Type"] and needle in await r.text(), path
        assert "Content-Security-Policy" in r.headers
    assert "<title>DefenceAi</title>" in await (await site.get("/app/")).text()
    assert (await site.get("/app")).status == 200


async def test_pages_carry_the_sellers_details_and_the_facts_of_the_product(site, monkeypatch):
    fill(monkeypatch)
    offer = await (await site.get("/offer")).text()
    for value in SELLER.values():
        assert value in offer
    assert f"{TRIAL_DAYS} дней" in offer and f"{REFUND_DAYS} дней" in offer
    for plan in PLANS.values():
        assert plan.title in offer and f"{plan.rub:,}".replace(",", " ") in offer
    landing = await (await site.get("/")).text()
    assert "https://t.me/DefenceAiBot" in landing and "/offer" in landing and "/privacy" in landing
    assert "<mark>" not in offer + landing + await (await site.get("/privacy")).text()


async def test_empty_details_stay_visibly_empty(site, monkeypatch):
    for key in SELLER:
        monkeypatch.delenv(key, raising=False)
    offer = await (await site.get("/offer")).text()
    assert "<mark>[заполните LEGAL_NAME в .env]</mark>" in offer
    assert legal.missing_details() == ["LEGAL_NAME", "LEGAL_INN", "LEGAL_OGRNIP", "LEGAL_EMAIL"]


async def test_seller_values_are_escaped(site, monkeypatch):
    fill(monkeypatch)
    monkeypatch.setenv("LEGAL_NAME", "<script>alert(1)</script>")
    assert "<script>alert(1)" not in await (await site.get("/offer")).text()


async def test_the_site_does_not_take_over_the_api(site):
    assert (await site.get("/api/chats")).status == 401
    assert (await site.get("/static/site.css")).status == 200 and (await site.get("/static/landing.js")).status == 200
