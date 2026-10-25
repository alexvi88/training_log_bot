"""Страница визитки `/p/<токен>`, её картинка превью и `web_url` в `/v1`
(share_page.py, handlers/sharing.web_link).

Гоняется через то же приложение, что поднимает прод (`mcp_server.build_app`):
страница, которая есть в коде, но не доехала до маршрутов, — это 404 у
каждого, кому прислали ссылку.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

import config
import share_page
from handlers import sharing

BASE = "https://training-log.example.com"
OWNER = 801
OTHER = 802

PROGRAM = {
    "v": sharing.PAYLOAD_VERSION,
    "name": "Upper Lower",
    "description": "Four weeks, add 2.5kg each week",
    "days": [
        {"name": "Upper", "exercises": [
            {"name": "Жим штанги лёжа", "target": "4×6–8"},
            {"name": "Планка", "target": "3×30–60 сек"},
        ]},
        {"name": "Lower", "exercises": [{"name": "Присед со штангой", "target": "5×5"}]},
    ],
    "total_days": 2,
    "empty_days": 0,
}


@pytest.fixture(autouse=True)
def public_url(monkeypatch):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", BASE)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(config, "APP_STORE_URL", "")
    monkeypatch.setattr(sharing, "_bot_username", "TrainLogBot")


async def _get(path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    import mcp_server

    transport = httpx.ASGITransport(app=mcp_server.build_app())
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        return await client.get(path, headers=headers or {})


async def _share(fresh_db, kind: str = "program", payload: dict | None = None, *, owner_lang: str = "ru") -> str:
    await fresh_db.get_or_create_user(telegram_id=OWNER, username="secret_owner", language_code=owner_lang)
    return await fresh_db.create_shared_item(OWNER, kind, json.dumps(payload or PROGRAM, ensure_ascii=False))


def _has_cyrillic(text: str) -> bool:
    return bool(re.search("[а-яё]", text, re.IGNORECASE))


def test_template_names_exist_in_catalog():
    import seed_data

    names = {n for _g, n in seed_data.EXERCISE_TEMPLATES}
    assert {"Жим штанги лёжа", "Планка", "Присед со штангой"} <= names


async def test_russian_page_lists_days_exercises_and_three_buttons(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "APP_STORE_URL", "https://apps.apple.com/app/id123?pt=42")
    token = await _share(fresh_db)

    resp = await _get(f"/p/{token}", headers={"Accept-Language": "ru-RU,ru;q=0.9"})

    assert resp.status_code == 200
    assert resp.headers["content-language"] == "ru"
    assert resp.headers["cache-control"] == "no-store"
    body = resp.text
    assert "<html lang=ru>" in body
    assert '<meta name=robots content="noindex">' in body
    assert "Тебе прислали программу · 2 дня, 3 упражнения" in body
    assert "<h1>Upper Lower</h1>" in body and "Four weeks, add 2.5kg each week" in body
    assert "<h2>Upper</h2>" in body and "<h2>Lower</h2>" in body
    assert "Жим штанги лёжа" in body and "3×30–60 сек" in body
    assert f'href="trainingdiary://share/{token}"' in body and "Открыть в приложении" in body
    assert 'href="https://apps.apple.com/app/id123?pt=42&amp;ct=share_program"' in body
    assert "Скачать в App Store" in body
    assert f'href="https://t.me/TrainLogBot?start=sh_{token}"' in body and "Открыть в Telegram" in body
    assert f'<meta property="og:image" content="{BASE}/og/share/ru/{token}.png">' in body
    assert f'<meta property="og:url" content="{BASE}/p/{token}">' in body
    # Наружу — только снапшот: ни username, ни id владельца.
    assert "secret_owner" not in body and str(OWNER) not in body


async def test_english_page_has_no_russian(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "APP_STORE_URL", "https://apps.apple.com/app/id123")
    token = await _share(fresh_db)

    resp = await _get(f"/p/{token}?lang=en", headers={"Accept-Language": "ru"})

    assert resp.status_code == 200
    body = resp.text
    assert "<html lang=en>" in body
    assert "Someone sent you a program · 2 days, 3 exercises" in body
    assert "Open in the app" in body and "Download on the App Store" in body and "Open in Telegram" in body
    assert "ct=share_program" in body
    assert "3×30–60 sec" in body
    assert not _has_cyrillic(body)


async def test_language_falls_back_to_owner_without_header(fresh_db):
    """Боты превью мессенджеров ходят без Accept-Language — превью на языке
    владельца, а не на языке по умолчанию."""
    token = await _share(fresh_db, owner_lang="en")

    resp = await _get(f"/p/{token}")

    assert resp.headers["content-language"] == "en"
    assert not _has_cyrillic(resp.text)


async def test_exercise_and_routine_pages(fresh_db):
    ex = await _share(fresh_db, "exercise", {"v": 4, "name": "Планка", "group": "Пресс",
                                             "description": "Keep the line", "photo_file_id": "AgAC-secret"})
    rt = await _share(fresh_db, "routine", {"v": 4, "name": "Day A",
                                            "exercises": [{"name": "Планка", "target": None}]})

    ex_page = (await _get(f"/p/{ex}", headers={"Accept-Language": "en"})).text
    rt_page = (await _get(f"/p/{rt}", headers={"Accept-Language": "en"})).text

    assert "Someone sent you an exercise" in ex_page and "Keep the line" in ex_page
    assert "AgAC-secret" not in ex_page and not _has_cyrillic(ex_page)
    assert "Someone sent you a program · 1 exercise" in rt_page and "<h1>Day A</h1>" in rt_page
    assert not _has_cyrillic(rt_page)


async def test_owner_text_is_escaped(fresh_db):
    token = await _share(fresh_db, payload={**PROGRAM, "name": "<script>x</script>"})

    body = (await _get(f"/p/{token}", headers={"Accept-Language": "en"})).text

    assert "<script>x</script>" not in body and "&lt;script&gt;x&lt;/script&gt;" in body


@pytest.mark.parametrize("token", ["missing_tok", "a" * 80, "bad%20token"])
async def test_unknown_token_is_friendly_404(fresh_db, token):
    ru = await _get(f"/p/{token}", headers={"Accept-Language": "ru"})
    en = await _get(f"/p/{token}", headers={"Accept-Language": "en"})

    assert ru.status_code == 404 and en.status_code == 404
    assert "Не нашёл эту ссылку" in ru.text and "Попроси прислать свежую ссылку" in ru.text
    assert "Can&#x27;t find this link" in en.text and not _has_cyrillic(en.text)
    assert 'content="noindex"' in en.text


async def test_revoked_token_is_404_for_page_and_image(fresh_db):
    token = await _share(fresh_db)
    assert (await _get(share_page.og_path("en", token))).status_code == 200

    await fresh_db.delete_shared_item(token, OWNER)

    assert (await _get(f"/p/{token}")).status_code == 404
    assert (await _get(share_page.og_path("en", token))).status_code == 404


async def test_og_image_is_png_1200x630(fresh_db, monkeypatch, tmp_path):
    import io

    from PIL import Image

    import seo_static

    monkeypatch.setattr(seo_static, "OG_CACHE_DIR", str(tmp_path))
    token = await _share(fresh_db)

    resp = await _get(share_page.og_path("ru", token))

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(resp.content)).size == (1200, 630)
    assert (await _get(f"/og/share/it/{token}.png")).status_code == 404


def test_og_cache_key_keeps_token_case():
    """og_png пишет кэш на диск в нижнем регистре — токены, различающиеся
    только регистром, не должны делить картинку."""
    seen = []
    import seo_static

    original = seo_static.og_png
    try:
        seo_static.og_png = lambda key, *a: seen.append(key) or b""
        share_page.og_image("en", "AbC", "program", PROGRAM)
        share_page.og_image("en", "abc", "program", PROGRAM)
    finally:
        seo_static.og_png = original
    assert seen[0].lower() != seen[1].lower()


async def test_aasa_lists_share_path(monkeypatch):
    monkeypatch.setattr(config, "APPLE_TEAM_ID", "H93ZYM2S8U")

    data = json.loads((await _get("/.well-known/apple-app-site-association")).text)
    detail = data["applinks"]["details"][0]

    assert {"/": "/p/*"} in detail["components"] and {"/": "/coach/*"} in detail["components"]
    assert "/p/*" in detail["paths"] and "/coach/*" in detail["paths"]


def test_web_link_needs_public_url(monkeypatch):
    assert sharing.web_link("tok") == f"{BASE}/p/tok"
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "")
    assert sharing.web_link("tok") is None


# ---------- /v1: web_url ----------


async def _api_client(fresh_db, telegram_id: int) -> httpx.AsyncClient:
    import api_v1

    await fresh_db.get_or_create_user(telegram_id=telegram_id, username=f"user{telegram_id}")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def test_v1_share_responses_carry_web_url(fresh_db):
    client = await _api_client(fresh_db, OTHER)
    group_id = await fresh_db.create_muscle_group(OTHER, "Спина")
    ex_id = await fresh_db.create_exercise(OTHER, "Row", group_id)
    try:
        created = await client.post(f"/share/exercises/{ex_id}")
        mine = await client.get("/share/mine")
    finally:
        await client.aclose()

    assert created.status_code == 201
    token = created.json()["token"]
    assert created.json()["web_url"] == f"{BASE}/p/{token}"
    assert mine.json()["items"][0]["web_url"] == f"{BASE}/p/{token}"


# ---------- бот: строка со страницей в визитке ----------


async def test_bot_card_links_the_web_page(fresh_db, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr(config, "APP_STORE_URL", "https://apps.apple.com/app/id123")
    await fresh_db.get_or_create_user(telegram_id=OTHER, username="u")
    group_id = await fresh_db.create_muscle_group(OTHER, "Спина")
    ex_id = await fresh_db.create_exercise(OTHER, "Row", group_id)
    callback = MagicMock()
    callback.data = f"share:ex:{ex_id}"
    callback.from_user.id = OTHER
    callback.message.answer = AsyncMock()
    callback.answer = AsyncMock()

    await sharing.share_exercise(callback, MagicMock())

    text = callback.message.answer.await_args.args[0]
    token = callback.message.answer.await_args.kwargs["reply_markup"].inline_keyboard[1][0].callback_data.split(":")[2]
    assert f"{BASE}/p/{token}" in text
    # Страница несёт и App Store, поэтому голая ссылка на него из визитки ушла.
    assert "apps.apple.com" not in text
    # Кнопка визитки — по-прежнему в бота: пересланная в Telegram, она короче.
    assert callback.message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].url.startswith("https://t.me/")
