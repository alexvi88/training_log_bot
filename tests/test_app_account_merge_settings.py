"""Слияние app-only аккаунта в уже существующую, но пустую Telegram-строку.

Обычный путь: человек жил в приложении, потом написал боту /start (строка
users появилась, истории нет) и только после этого — /link_app. Раньше
побеждала бот-строка: язык, согласие на AI, пояс, единицы и профиль из
приложения пропадали, англоязычный атлет становился русским и заново давал
согласие. Теперь пустая бот-строка ведёт себя как отсутствующая — побеждают
настройки приложения, а своё у бота (источник из deep link'а, username)
остаётся.
"""

import httpx
import pytest

import api_v1
import db

pytestmark = pytest.mark.asyncio

TG = 777


def _client(token):
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    c.headers["Authorization"] = f"Bearer {token}"
    return c


async def _app_user_with_history():
    app = await db.create_app_only_user(language_code="en", tz_offset=-5)
    aid = app["telegram_id"]
    c = _client(await db.issue_api_token(aid))
    # Единица — до подхода: 185 записывается уже в фунтах, как их и хранит
    # аккаунт в lb. Бот-строка с kg поверх такой истории превратила бы 185 lb
    # в 185 кг.
    r = await c.patch("/settings", json={"ai_consent": True, "unit": "lb"})
    assert r.status_code == 200, r.text
    wid = (await c.post("/workouts/active")).json()["id"]
    eid = (await c.post("/exercises", json={"name": "Bench"})).json()["id"]
    r = await c.post(
        f"/workouts/{wid}/sets",
        json={"exercise_id": eid, "weight": 185, "reps": 5, "idempotency_key": "k"},
    )
    assert r.status_code in (200, 201), r.text
    assert (await c.post(f"/workouts/{wid}/finish", json={})).status_code == 200
    assert (await c.post("/push/register", json={"device_token": "iphone"})).status_code in (200, 201)
    await db.update_user(aid, goal="strength", kcal_goal=2500, rank_level_seen=2)
    return aid, c


async def test_empty_telegram_row_takes_app_settings(fresh_db):
    aid, c = await _app_user_with_history()
    app_before = await db.get_user(aid)

    # /start в боте: строка есть, истории нет, язык телефона — русский,
    # человек пришёл по метке канала.
    await db.get_or_create_user(TG, "tg_name", "ru")
    await db.set_user_source(TG, "channel_x", referrer_id=42)
    await db.update_user(TG, voice_hint_shown=1, reply_keyboard_version=3)
    await db.register_push_token(TG, "ios", "ipad")

    assert await db.link_telegram_to_app_account(aid, TG, "tg_name") == "ok"

    assert await db.get_user(aid) is None
    u = await db.get_user(TG)
    # Настройки — из приложения.
    assert u["lang"] == "en"
    assert u["ai_consent_at"] == app_before["ai_consent_at"] is not None
    assert u["tz_offset"] == -5
    assert u["unit"] == "lb"
    assert u["goal"] == "strength" and u["kcal_goal"] == 2500
    assert u["rank_level_seen"] == 2
    # Своё у бота — остаётся.
    assert u["telegram_linked"] == 1
    assert u["username"] == "tg_name"
    assert u["source"] == "channel_x" and u["referrer_id"] == 42
    assert u["voice_hint_shown"] == 1
    assert u["reply_keyboard_version"] == 3
    # История и устройства обеих сторон — под telegram_id.
    assert await db.count_workouts(TG) == 1
    assert set(await db.get_push_tokens(TG)) == {"iphone", "ipad"}
    me = await c.get("/me")
    assert me.status_code == 200, me.text
    cur = await db.conn().execute("SELECT weight FROM sets")
    assert [row["weight"] for row in await cur.fetchall()] == [185]


async def test_telegram_row_with_history_keeps_its_settings(fresh_db):
    """Обратный случай не меняется: у бот-строки есть история, у app-строки
    нет — каноническая строка бота со своими настройками."""
    app = await db.create_app_only_user(language_code="en")
    aid = app["telegram_id"]
    await db.update_user(aid, unit="lb")

    await db.get_or_create_user(TG, "tg", "ru")
    await db.create_finished_workout(TG, "2026-01-01T10:00:00", "2026-01-01T11:00:00")

    assert await db.link_telegram_to_app_account(aid, TG, "tg") == "ok"
    u = await db.get_user(TG)
    assert u["lang"] == "ru" and u["unit"] == "kg" and u["telegram_linked"] == 1
    assert await db.get_user(aid) is None


async def test_both_with_history_still_refused(fresh_db):
    aid, c = await _app_user_with_history()
    await db.get_or_create_user(TG, "tg", "ru")
    await db.create_finished_workout(TG, "2026-01-01T10:00:00", "2026-01-01T11:00:00")
    assert await db.link_telegram_to_app_account(aid, TG, "tg") == "both_accounts_have_data"
    assert (await db.get_user(aid))["lang"] == "en"
    assert (await db.get_user(TG))["lang"] == "ru"
    assert (await c.get("/me")).status_code == 200


async def test_merge_into_empty_row_is_atomic(fresh_db, monkeypatch):
    """Сбой на переписывании строки users откатывает всё слияние целиком."""
    aid, _ = await _app_user_with_history()
    await db.get_or_create_user(TG, "tg", "ru")

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(db, "_adopt_app_user_settings", boom)
    with pytest.raises(RuntimeError):
        await db.link_telegram_to_app_account(aid, TG, "tg")
    assert (await db.get_user(aid))["lang"] == "en"
    assert (await db.get_user(TG))["lang"] == "ru"
    assert await db.count_workouts(aid) == 1
    assert await db.count_workouts(TG) == 0
    assert await db.get_push_tokens(aid) == ["iphone"]
