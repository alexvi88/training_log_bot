"""«8» первым подходом в приложении: вес — с прошлой тренировки, как у бота.

Раньше сервер искал вес только в сегодняшних подходах упражнения, и первый
подход голыми повторами ложился как 0 кг × 8 — тоннаж и рекорды обнулялись.
"""

import httpx
import pytest

import api_v1


async def _client(fresh_db):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    code = await fresh_db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _parse(client, workout_id, exercise_id, text):
    resp = await client.post(
        f"/workouts/{workout_id}/sets/parse", json={"exercise_id": exercise_id, "text": text}
    )
    assert resp.status_code == 201, resp.text
    return [(s["weight"], s["reps"]) for s in resp.json()["sets"]]


@pytest.mark.asyncio
async def test_bare_reps_first_set_takes_last_session_first_weight(fresh_db):
    client = await _client(fresh_db)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]

    first = (await client.post("/workouts/active")).json()["id"]
    # Первый подход прошлого раза — рабочий вес; последний — уже откат.
    await _parse(client, first, exercise_id, "60 8, 55 8")
    assert (await client.post(f"/workouts/{first}/finish")).status_code == 200

    second = (await client.post("/workouts/active")).json()["id"]
    assert second != first
    assert await _parse(client, second, exercise_id, "8") == [(60, 8)]
    # Дальше — как раньше: вес прошлого подхода сегодня.
    await _parse(client, second, exercise_id, "62.5 6")
    assert await _parse(client, second, exercise_id, "6") == [(62.5, 6)]


@pytest.mark.asyncio
async def test_bare_reps_without_history_stays_zero(fresh_db):
    client = await _client(fresh_db)
    exercise_id = (await client.post("/exercises", json={"name": "Подтягивания"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    assert await _parse(client, workout_id, exercise_id, "8") == [(0, 8)]
