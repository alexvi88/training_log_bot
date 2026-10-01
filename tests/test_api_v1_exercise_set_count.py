"""`set_count` у строк GET /exercises — сколько подходов атлет реально записал
на упражнение. Экран «Прогресс» в приложении показывает только упражнения с
подходами: упражнение, добавленное в тренировку без единого подхода, и ни разу
не открытое упражнение — оба 0. Поле есть и в общем списке, и в `?group_id=`,
но не у выборок, которые его не считают (поиск)."""

from __future__ import annotations

import httpx
import pytest

import api_v1

pytestmark = pytest.mark.asyncio

USER = 111


async def _linked(fresh_db) -> httpx.AsyncClient:
    await fresh_db.get_or_create_user(telegram_id=USER, username="tester")
    code = await fresh_db.issue_oauth_link_code(USER, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def test_set_count_counts_logged_sets_only(fresh_db):
    client = await _linked(fresh_db)
    group_id = (await fresh_db.list_muscle_groups(USER))[0]["id"]
    done = await fresh_db.create_exercise(USER, "Жим штанги лёжа", group_id)
    added_empty = await fresh_db.create_exercise(USER, "Разводка гантелей", group_id)
    never = await fresh_db.create_exercise(USER, "Моё странное движение", group_id)

    # Подходы в двух тренировках — счёт идёт по всем.
    for sets in (2, 1):
        workout_id = await fresh_db.create_workout(USER)
        block_id = await fresh_db.create_block(workout_id, "single")
        await fresh_db.add_block_exercise(block_id, done, 0)
        for i in range(sets):
            await fresh_db.add_set(block_id, done, i, 0, 80.0, 8)
    # Добавлено в тренировку, но ни одного подхода.
    workout_id = await fresh_db.create_workout(USER)
    block_id = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_block_exercise(block_id, added_empty, 0)

    for path in ("/exercises", f"/exercises?group_id={group_id}"):
        resp = await client.get(path)
        assert resp.status_code == 200, resp.text
        counts = {e["id"]: e["set_count"] for e in resp.json()}
        assert counts[done] == 3, path
        assert counts[added_empty] == 0, path
        assert counts[never] == 0, path
        assert all(isinstance(v, int) for v in counts.values())

    # Выборка без колонки поле не выдумывает.
    searched = (await client.get("/exercises", params={"query": "Жим"})).json()
    assert searched and all("set_count" not in e for e in searched)
