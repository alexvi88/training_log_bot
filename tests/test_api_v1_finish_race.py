"""`POST /v1/workouts/{id}/finish`, когда тренировку закончили посреди запроса.

Повтор finish по уже законченной тренировке покрыт
(`test_api_v1_finish_replay_pending.py`). Не исполнялась гонка ВНУТРИ
запроса: проверка статуса прошла («active»), а `db.finish_workout` вернул
False — тренировку только что закончил второй клиент (бот, второй тап,
повтор из очереди). Обещание кода: человеку тот же итог, что и первому
вызову (`replayed`), без второго награждения; а если её между делом удалили —
честный 404, а не итоги несуществующей тренировки.
"""

import httpx
import pytest

import api_v1
import db as dbmod

pytestmark = pytest.mark.asyncio

USER = 111


async def _client(db):
    await db.get_or_create_user(telegram_id=USER, username="tester")
    code = await db.issue_oauth_link_code(USER, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    token = (await client.post("/auth/link", json={"code": code})).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def _active_workout_with_set(db):
    ex = await db.create_exercise(USER, "Жим", await db.create_muscle_group(USER, "Грудь"))
    workout = await db.create_workout(USER)
    block = await db.create_block(workout, "single")
    await db.add_block_exercise(block, ex, 0)
    await db.add_set(block, ex, 0, 0, 80.0, 8, None)
    return workout


async def test_finished_by_another_client_mid_request_returns_the_same_summary(fresh_db, monkeypatch):
    db = fresh_db
    client = await _client(db)
    workout = await _active_workout_with_set(db)
    real_finish = dbmod.finish_workout

    async def other_client_wins(workout_id, **kwargs):
        # Второй клиент закончил её ровно между проверкой статуса и записью.
        assert await real_finish(workout_id) is True
        return await real_finish(workout_id, **kwargs)

    monkeypatch.setattr(dbmod, "finish_workout", other_client_wins)

    resp = await client.post(f"/workouts/{workout}/finish", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["replayed"] is True
    assert body["id"] == workout
    assert body["status"] == "finished"
    # События момента завершения не сочиняются второй раз.
    assert body["rewards"]["new_achievements"] == []


async def test_deleted_by_another_client_mid_request_is_404(fresh_db, monkeypatch):
    db = fresh_db
    client = await _client(db)
    workout = await _active_workout_with_set(db)

    async def deleted_meanwhile(workout_id, **kwargs):
        await dbmod.discard_workout(workout_id)
        return False

    monkeypatch.setattr(dbmod, "finish_workout", deleted_meanwhile)

    resp = await client.post(f"/workouts/{workout}/finish", json={})

    assert resp.status_code == 404, resp.text
    assert await db.get_workout(workout) is None
