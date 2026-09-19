"""REST `/v1` для главной сводки — api_v1_dashboard.py.

По образцу tests/test_api_v1_account.py: httpx поверх ASGI-приложения без
сокета, `client_factory` и `_linked_client` заведены локально (в чужой
тестовый файл не лезем — его может в это же время редактировать другой агент).
"""

import datetime as dt
import re

import httpx
import pytest

import api_v1
import db

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111, lang=None):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    if lang is not None:
        await fresh_db.set_user_lang(telegram_id, lang)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def _train(user_id: int, name: str = "Bench Press", days_ago: int = 1) -> None:
    """Одна законченная тренировка с тремя подходами — минимум, при котором
    сводка перестаёт быть пустой: нужны и дата в окне объёма, и сами подходы."""
    group_id = (await db.list_muscle_groups(None, global_only=True))[0]["id"]
    ex_id = await db.create_exercise(user_id, name, group_id)
    day = dt.datetime.now() - dt.timedelta(days=days_ago)
    workout_id = await db.create_finished_workout(
        user_id, day.isoformat(), (day + dt.timedelta(hours=1)).isoformat()
    )
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for _ in range(3):
        await db.append_set(block_id, ex_id, 0, 100.0, 5, rpe=8.0)


@pytest.mark.asyncio
async def test_dashboard_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/dashboard")
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_dashboard_is_null_for_a_user_without_workouts(fresh_db, client_factory):
    """Пустая сводка — не ошибка и не структура из нулей: у новичка показывать
    нечего, и клиент по `null` рисует приглашение начать, а не таблицу."""
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    assert resp.json() is None


@pytest.mark.asyncio
async def test_dashboard_has_headline_rank_and_tiles_after_a_workout(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    await _train(111)

    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["headline"]
    assert body["rank"]["name"]
    assert isinstance(body["rank"]["level"], int)
    assert body["tiles"]
    for tile in body["tiles"]:
        assert set(tile) == {"label", "value", "sub"}
        assert tile["label"] and tile["value"]
    # Объём за неделю — тренировка была вчера, значит группа в панели есть.
    assert body["volume"]["title"]
    assert any(row["sets"] == 3 for row in body["volume"]["rows"])
    for row in body["volume"]["rows"]:
        assert set(row) == {"group", "sets", "status"}
    assert set(body["lifts"]) == {"title", "note", "tiles"}


@pytest.mark.asyncio
async def test_dashboard_speaks_the_users_language(fresh_db, client_factory):
    """Ответ собирается под users.lang, а не под язык того, кто первым дёрнул
    модуль в этом процессе: без i18n.use_lang англоязычный атлет получил бы
    русскую сводку (CLAUDE.md, «Ловушка, встретившаяся шесть раз»)."""
    client = await _linked_client(fresh_db, client_factory, lang="en")
    await _train(111)

    body = (await client.get("/dashboard")).json()

    assert not CYRILLIC.search(body["headline"]), body["headline"]
    assert not CYRILLIC.search(body["rank"]["name"]), body["rank"]["name"]
    assert not CYRILLIC.search(body["volume"]["title"]), body["volume"]["title"]
    for tile in body["tiles"]:
        assert not CYRILLIC.search(tile["label"]), tile["label"]
        assert not CYRILLIC.search(tile["sub"] or ""), tile["sub"]
