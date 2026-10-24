"""Страница приглашения тренера `/coach/<код>` и AASA (coach_invite_page.py),
плюс https-ссылка в текстах приглашения бота и `/v1`.

Гоняется через то же приложение, что поднимает прод (`mcp_server.build_app`):
страница, которая есть в коде, но не доехала до маршрутов, — это 404 у
каждого, кому тренер отправил ссылку.
"""

from __future__ import annotations

import json

import httpx
import pytest

import coach_invite_page
import coach_service
import config
from handlers import sharing

BASE = "https://training-log.example.com"
COACH = 701
OTHER = 702


@pytest.fixture(autouse=True)
def public_url(monkeypatch):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", BASE)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(config, "APP_STORE_URL", "")
    monkeypatch.setattr(coach_invite_page, "_hits", {})


async def _get(path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    import mcp_server

    transport = httpx.ASGITransport(app=mcp_server.build_app())
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        return await client.get(path, headers=headers or {})


async def _coach(fresh_db, name: str = "ABishop") -> str:
    await fresh_db.get_or_create_user(telegram_id=COACH, username="coach")
    await fresh_db.upsert_coach_profile(COACH, name)
    await fresh_db.replace_coach_invite(COACH, "BR6YMNND")
    return "BR6YMNND"


def _has_cyrillic(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in text)


async def test_russian_page_shows_name_code_and_three_buttons(fresh_db, monkeypatch):
    code = await _coach(fresh_db)
    monkeypatch.setattr(config, "APP_STORE_URL", "https://apps.apple.com/app/id123")
    monkeypatch.setattr(sharing, "_bot_username", "TrainLogBot")

    resp = await _get(f"/coach/{code.lower()}", headers={"Accept-Language": "ru-RU,ru;q=0.9"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["content-language"] == "ru"
    assert "Accept-Language" in resp.headers["vary"]
    body = resp.text
    assert "<html lang=ru>" in body
    assert "ABishop зовёт тебя тренироваться" in body
    assert f"id=code>{code}</div>" in body
    assert f'href="trainingdiary://coach/{code}"' in body and "Открыть в приложении" in body
    assert 'href="https://apps.apple.com/app/id123"' in body and "Скачать в App Store" in body
    # Ссылка из App Store до приложения не доедет — кнопка кладёт код в буфер.
    assert "id=store" in body and "getElementById('store')" in body
    assert f'href="https://t.me/TrainLogBot?start=coach_{code}"' in body
    assert "Продолжить в Telegram" in body


async def test_english_page_has_no_russian(fresh_db, monkeypatch):
    code = await _coach(fresh_db)
    monkeypatch.setattr(sharing, "_bot_username", "TrainLogBot")

    resp = await _get(f"/coach/{code}", headers={"Accept-Language": "en-US,en;q=0.9"})

    assert resp.status_code == 200
    assert "<html lang=en>" in resp.text
    assert "ABishop wants to coach you" in resp.text
    assert "Continue in Telegram" in resp.text
    assert not _has_cyrillic(resp.text)


async def test_optional_buttons_hide_without_config(fresh_db):
    code = await _coach(fresh_db)

    resp = await _get(f"/coach/{code}", headers={"Accept-Language": "en"})

    assert "Open in the app" in resp.text
    # Подсказка про App Store в тексте остаётся (код вводят руками), кнопки нет.
    assert "Download on the App Store" not in resp.text and "id=store" not in resp.text
    assert "t.me" not in resp.text


async def test_page_leaks_only_the_coach_name(fresh_db):
    code = await _coach(fresh_db, name="<b>Ivan</b>")

    resp = await _get(f"/coach/{code}", headers={"Accept-Language": "en"})

    assert "&lt;b&gt;Ivan&lt;/b&gt;" in resp.text and "<b>Ivan</b>" not in resp.text
    assert str(COACH) not in resp.text and "coach\"" not in resp.text


@pytest.mark.parametrize("raw", ["ZZZZZZZZ", "short", "BR6YMNN0"])
async def test_unknown_code_is_friendly_404(fresh_db, raw):
    await _coach(fresh_db)

    ru = await _get(f"/coach/{raw}", headers={"Accept-Language": "ru"})
    en = await _get(f"/coach/{raw}", headers={"Accept-Language": "en"})

    assert ru.status_code == 404 and en.status_code == 404
    assert "Не нашёл такое приглашение" in ru.text and "Попроси у тренера" in ru.text
    assert "Can&#x27;t find this invite" in en.text and not _has_cyrillic(en.text)


async def test_renewed_code_is_404(fresh_db):
    code = await _coach(fresh_db)
    await fresh_db.replace_coach_invite(COACH, "K7MP2QXA")

    resp = await _get(f"/coach/{code}")

    assert resp.status_code == 404


async def test_lookups_are_rate_limited_per_ip(fresh_db, monkeypatch):
    code = await _coach(fresh_db)
    monkeypatch.setattr(coach_invite_page, "LOOKUPS_PER_WINDOW", 3)
    headers = {"Accept-Language": "en", "Fly-Client-IP": "203.0.113.5"}

    statuses = [(await _get(f"/coach/X{i}", headers=headers)).status_code for i in range(3)]
    blocked = await _get(f"/coach/{code}", headers=headers)
    other_ip = await _get(f"/coach/{code}", headers={"Accept-Language": "en", "Fly-Client-IP": "203.0.113.6"})

    assert statuses == [404, 404, 404]
    assert blocked.status_code == 429 and "Too many tries" in blocked.text
    assert "ABishop" not in blocked.text
    assert other_ip.status_code == 200


async def test_aasa_served_as_json_without_redirect(monkeypatch):
    monkeypatch.setattr(config, "APPLE_TEAM_ID", "H93ZYM2S8U")
    monkeypatch.setattr(config, "APPLE_BUNDLE_ID", "com.trainingdiary.ios")

    resp = await _get("/.well-known/apple-app-site-association")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    data = json.loads(resp.text)
    detail = data["applinks"]["details"][0]
    assert detail["appIDs"] == ["H93ZYM2S8U.com.trainingdiary.ios"]
    # Тренер, реферальная ссылка атлета и страница визитки — все три пути.
    assert detail["components"] == [{"/": "/coach/*"}, {"/": "/i/*"}, {"/": "/p/*"}]
    assert detail["paths"] == ["/coach/*", "/i/*", "/p/*"]


async def test_aasa_absent_without_team(monkeypatch):
    monkeypatch.setattr(config, "APPLE_TEAM_ID", "")

    resp = await _get("/.well-known/apple-app-site-association")

    assert resp.status_code == 404


def test_default_team_is_the_owners(monkeypatch):
    """Team ID из портала владельца — значение по умолчанию, не секрет."""
    monkeypatch.delenv("APPLE_TEAM_ID", raising=False)
    monkeypatch.delenv("APPLE_BUNDLE_ID", raising=False)
    import importlib

    fresh = importlib.reload(config)
    try:
        assert fresh.apple_app_id() == "H93ZYM2S8U.com.trainingdiary.ios"
    finally:
        importlib.reload(config)


# ---------- ссылка в приглашениях ----------


def test_invite_json_link_is_https():
    data = coach_service.invite_json("BR6YMNND")

    assert data["app_link"] == f"{BASE}/coach/BR6YMNND"
    assert "trainingdiary://" not in json.dumps(data)


def test_invite_json_falls_back_to_scheme_without_public_url(monkeypatch):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "")

    assert coach_service.invite_json("BR6YMNND")["app_link"] == "trainingdiary://coach/BR6YMNND"


@pytest.mark.parametrize(
    "raw",
    [
        "BR6YMNND",
        "br6y-mnnd",
        f"{BASE}/coach/BR6YMNND",
        f"{BASE}/coach/br6ymnnd/",
        f"{BASE}/coach/BR6YMNND?lang=en#x",
        "trainingdiary://coach/BR6YMNND",
        "br6y mnnd",
        "Код для приложения: BR6YMNND",
        "Code for the app: br6y-mnnd",
        # Продиктовали, а набрали в русской раскладке: В и М — кириллица.
        "ВR6YМNND",
        "Это Иван — буду вести твои тренировки.\nhttps://x.example/coach/BR6YMNND\nКод для приложения: BR6YMNND",
    ],
)
def test_normalize_accepts_old_and_new_links(raw):
    assert coach_service.normalize_invite_code(raw) == "BR6YMNND"


def test_bot_extracts_code_from_https_link():
    from handlers import coach

    assert coach._extract_code(f"{BASE}/coach/br6ymnnd") == "BR6YMNND"
    assert coach._extract_code("https://t.me/Bot?start=coach_BR6YMNND") == "BR6YMNND"
    assert coach._extract_code("trainingdiary://coach/BR6YMNND") == "BR6YMNND"
