"""Реферальная ссылка без Telegram: страница `/i/ref_<id>` (referral_page.py),
`invite_url` в `/v1/me` и `POST /v1/me/referral` — атрибуция атлета из
приложения тем же db.set_user_source, что у /start в боте.

Страница гоняется через то же приложение, что поднимает прод
(`mcp_server.build_app`): маршрут, который есть в коде, но не доехал до
сервера, — 404 у каждого, кому атлет отправил ссылку.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

import acquisition
import api_v1
import config
import db
from handlers import sharing

BASE = "https://training-log.example.com"
REFERRER = 801


@pytest.fixture(autouse=True)
def public_url(monkeypatch):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", BASE)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(config, "APP_STORE_URL", "")
    monkeypatch.setattr(sharing, "_bot_username", None)


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _get(path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    import mcp_server

    transport = httpx.ASGITransport(app=mcp_server.build_app())
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        return await client.get(path, headers=headers or {})


def _has_cyrillic(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in text)


# ---------- acquisition: разбор и сборка ----------


@pytest.mark.parametrize(
    ("code", "expected"),
    [("ref_123", 123), (" ref_-1000000001 ", -1000000001), ("ref_", None), ("ref_1²", None),
     ("123", None), ("src_tg", None), ("", None), (None, None)],
)
def test_referrer_from_code(code, expected):
    assert acquisition.referrer_from_code(code) == expected


def test_web_link_and_campaign():
    assert acquisition.referral_web_link(BASE, 42) == f"{BASE}/i/ref_42"
    assert acquisition.referral_web_link(None, 42) is None
    assert acquisition.with_campaign("https://apps.apple.com/app/id1", 42) == (
        "https://apps.apple.com/app/id1?ct=ref_42"
    )
    assert acquisition.with_campaign("https://apps.apple.com/app/id1?pt=99", 42) == (
        "https://apps.apple.com/app/id1?pt=99&ct=ref_42"
    )


# ---------- страница ----------


async def test_russian_page_app_first_store_with_campaign_telegram_last(monkeypatch):
    monkeypatch.setattr(config, "APP_STORE_URL", "https://apps.apple.com/app/id123?pt=99")
    monkeypatch.setattr(sharing, "_bot_username", "TrainLogBot")

    resp = await _get("/i/ref_801", headers={"Accept-Language": "ru-RU,ru;q=0.9"})

    assert resp.status_code == 200
    body = resp.text
    app = body.index("trainingdiary://i/ref_801")
    store = body.index("https://apps.apple.com/app/id123?pt=99&amp;ct=ref_801")
    telegram = body.index("https://t.me/TrainLogBot?start=ref_801")
    assert app < store < telegram, "порядок: приложение → App Store → Telegram"
    assert "Открыть в приложении" in body
    assert 'class="btn small"' in body[telegram - 200:telegram + 200]
    assert resp.headers["cache-control"] == "no-store"


async def test_english_page_has_no_russian(monkeypatch):
    monkeypatch.setattr(config, "APP_STORE_URL", "https://apps.apple.com/app/id123")
    monkeypatch.setattr(sharing, "_bot_username", "TrainLogBot")

    resp = await _get("/i/ref_801", headers={"Accept-Language": "en-US"})

    assert resp.status_code == 200
    assert not _has_cyrillic(resp.text)
    assert "Open in the app" in resp.text


async def test_page_works_without_store_and_bot():
    resp = await _get("/i/ref_801")

    assert resp.status_code == 200
    assert "trainingdiary://i/ref_801" in resp.text
    assert "apps.apple.com" not in resp.text and "t.me" not in resp.text


async def test_page_does_not_touch_the_referrer(fresh_db):
    """Ни имени, ни «такой есть/нет»: у существующего и выдуманного id ответ
    один и тот же — страница не оракул по чужим аккаунтам."""
    await fresh_db.get_or_create_user(telegram_id=REFERRER, username="secret_name")

    real = await _get("/i/ref_801")
    fake = await _get("/i/ref_802")

    assert real.status_code == fake.status_code == 200
    assert "secret_name" not in real.text
    assert real.text.replace("802", "801") == fake.text.replace("802", "801")


@pytest.mark.parametrize("raw", ["nope", "ref_", "ref_abc", "src_tg"])
async def test_malformed_code_is_friendly_404(raw):
    resp = await _get(f"/i/{raw}", headers={"Accept-Language": "en"})

    assert resp.status_code == 404
    assert "read this link" in resp.text


# ---------- /v1 ----------


async def _client_for(fresh_db, client_factory, user_id: int):
    code = await fresh_db.issue_oauth_link_code(user_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _app_only_user(fresh_db) -> int:
    user = await fresh_db.create_app_only_user(language_code="ru")
    return user["telegram_id"]


async def test_me_gives_web_invite_url(fresh_db, client_factory):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    client = await _client_for(fresh_db, client_factory, 111)

    me = (await client.get("/me")).json()

    assert me["invite_url"] == f"{BASE}/i/ref_111"
    assert "t.me" not in me["invite_url"]


async def test_me_invite_url_is_null_without_public_address(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "")
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    client = await _client_for(fresh_db, client_factory, 111)

    me = (await client.get("/me")).json()

    assert me["invite_url"] is None


async def test_app_only_user_gets_referral_source(fresh_db, client_factory):
    await fresh_db.get_or_create_user(telegram_id=REFERRER, username="ref")
    user_id = await _app_only_user(fresh_db)
    client = await _client_for(fresh_db, client_factory, user_id)

    resp = await client.post("/me/referral", json={"code": f"ref_{REFERRER}"})

    assert resp.status_code == 200 and resp.json() == {"applied": True}
    user = await db.get_user(user_id)
    assert user["source"] == acquisition.SOURCE_REFERRAL
    assert user["referrer_id"] == REFERRER
    top = await db.top_referrers()
    assert [r["referrer_id"] for r in top] == [REFERRER]


async def test_first_touch_wins(fresh_db, client_factory):
    await fresh_db.get_or_create_user(telegram_id=REFERRER, username="ref")
    await fresh_db.get_or_create_user(telegram_id=REFERRER + 1, username="ref2")
    user_id = await _app_only_user(fresh_db)
    client = await _client_for(fresh_db, client_factory, user_id)

    first = await client.post("/me/referral", json={"code": f"ref_{REFERRER}"})
    second = await client.post("/me/referral", json={"code": f"ref_{REFERRER + 1}"})

    assert first.json() == {"applied": True}
    assert second.json() == {"applied": False}
    assert (await db.get_user(user_id))["referrer_id"] == REFERRER


async def test_bot_user_keeps_its_start_source(fresh_db, client_factory):
    await fresh_db.get_or_create_user(telegram_id=REFERRER, username="ref")
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    await fresh_db.set_user_source(111, "src_channel")
    client = await _client_for(fresh_db, client_factory, 111)

    resp = await client.post("/me/referral", json={"code": f"ref_{REFERRER}"})

    assert resp.json() == {"applied": False}
    assert (await db.get_user(111))["source"] == "src_channel"


async def test_self_unknown_and_stale_are_not_applied(fresh_db, client_factory, monkeypatch):
    user_id = await _app_only_user(fresh_db)
    client = await _client_for(fresh_db, client_factory, user_id)

    own = await client.post("/me/referral", json={"code": f"ref_{user_id}"})
    ghost = await client.post("/me/referral", json={"code": "ref_424242"})
    assert own.json() == {"applied": False}
    assert ghost.json() == {"applied": False}

    await fresh_db.get_or_create_user(telegram_id=REFERRER, username="ref")
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=config.REFERRAL_CLAIM_DAYS + 1)).isoformat()
    await fresh_db.conn().execute("UPDATE users SET created_at = ? WHERE telegram_id = ?", (old, user_id))
    await fresh_db.conn().commit()
    stale = await client.post("/me/referral", json={"code": f"ref_{REFERRER}"})
    assert stale.json() == {"applied": False}
    assert (await db.get_user(user_id))["source"] is None


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_malformed_code_is_400_in_athletes_language(fresh_db, client_factory, lang):
    user = await fresh_db.create_app_only_user(language_code=lang)
    client = await _client_for(fresh_db, client_factory, user["telegram_id"])

    resp = await client.post("/me/referral", json={"code": "https://t.me/x?start=ref_1"})

    assert resp.status_code == 400
    data = resp.json()
    assert data["error"] == "invalid_referral"
    assert _has_cyrillic(data["message"]) == (lang == "ru")


def test_route_is_registered():
    assert any(getattr(r, "path", "") == "/me/referral" for r in api_v1.routes)
