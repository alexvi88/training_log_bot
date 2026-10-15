"""`sessions` и `last_workout_at` у строк GET /exercises — подпись строки своего
«Прогресса» в приложении («Ноги · 18 тренировок · последняя 7 окт.»). Те же
числа и те же имена полей, что у тренера в «Прогрессе клиента»
(`coach_service.client_exercises`): одна выборка, `db.list_trained_exercises`,
через `api_v1_common.trained_exercise_stats`. Поля необязательные: упражнение
без законченных тренировок их не получает, старые клиенты их не читают."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

import api_v1
import coach_service
import timeutil

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


async def _finished_workout(api: httpx.AsyncClient, exercise_id: int, *, date: str | None = None) -> None:
    wid = (await api.post("/workouts/active", json={})).json()["id"]
    resp = await api.post(f"/workouts/{wid}/sets", json={"exercise_id": exercise_id, "weight": 50, "reps": 5})
    assert resp.status_code == 201, resp.text
    assert (await api.post(f"/workouts/{wid}/finish", json={})).status_code == 200
    if date is not None:
        assert (await api.patch(f"/workouts/{wid}/date", json={"date": date})).status_code == 200


async def _exercise(api: httpx.AsyncClient, name: str, group_id: int) -> int:
    resp = await api.post("/exercises", json={"name": name, "group_id": group_id})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_own_list_carries_the_same_stats_as_the_coach_list(fresh_db):
    api = await _linked(fresh_db)
    group_id = (await fresh_db.list_muscle_groups(USER))[0]["id"]
    squat = await _exercise(api, "Присед", group_id)
    curl = await _exercise(api, "Бицепс", group_id)
    in_progress = await _exercise(api, "Только начал", group_id)
    never = await _exercise(api, "Не делал", group_id)
    old = (dt.date.today() - dt.timedelta(days=10)).isoformat()
    await _finished_workout(api, squat, date=old)
    await _finished_workout(api, squat)
    await _finished_workout(api, curl, date=old)
    # Идущая тренировка — не история: подход есть, законченной тренировки нет.
    wid = (await api.post("/workouts/active", json={})).json()["id"]
    await api.post(f"/workouts/{wid}/sets", json={"exercise_id": in_progress, "weight": 10, "reps": 5})

    user = await fresh_db.get_user(USER)
    coach_view = {
        item["exercise_id"]: {"sessions": item["sessions"], "last_workout_at": item["last_workout_at"]}
        for item in await coach_service.client_exercises(user)
    }
    assert coach_view[squat] == {"sessions": 2, "last_workout_at": timeutil.user_today(user).isoformat()}
    assert coach_view[curl]["sessions"] == 1

    for path in ("/exercises", f"/exercises?group_id={group_id}", "/exercises?query=Присед"):
        resp = await api.get(path)
        assert resp.status_code == 200, resp.text
        rows = {e["id"]: e for e in resp.json()}
        for exercise_id, row in rows.items():
            shown = {k: row[k] for k in ("sessions", "last_workout_at") if k in row}
            assert shown == coach_view.get(exercise_id, {}), (path, row)
    rows = {e["id"]: e for e in (await api.get("/exercises")).json()}
    # Без законченных тренировок — без полей, а не «0 тренировок».
    assert "sessions" not in rows[in_progress] and "last_workout_at" not in rows[in_progress]
    assert "sessions" not in rows[never]
