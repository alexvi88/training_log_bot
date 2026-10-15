"""`body_volume` в GET /v1/dashboard — фигура «ТЫ» в сцене зала приложения.

Среднее рабочих подходов в неделю за 6 недель по семи группам, тем же счётом
подходов, что панель «ОБЪЁМ ЗА 7 ДНЕЙ» (db.weekly_volume_by_group).
"""

import datetime as dt

import httpx
import pytest

import api_v1
import dashboard_data
import db

KEYS = ["chest", "back", "legs", "shoulders", "biceps", "triceps", "abs"]


async def _client(fresh_db, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _group_id(name):
    return next(g["id"] for g in await db.list_muscle_groups(None, global_only=True) if g["name"] == name)


async def _train(user_id, group_name, sets, days_ago, ex_name):
    ex_id = await db.create_exercise(user_id, ex_name, await _group_id(group_name))
    day = dt.datetime.now() - dt.timedelta(days=days_ago)
    workout_id = await db.create_finished_workout(
        user_id, day.isoformat(), (day + dt.timedelta(hours=1)).isoformat()
    )
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for _ in range(sets):
        await db.append_set(block_id, ex_id, 0, 50.0, 8)
    return workout_id


@pytest.mark.asyncio
async def test_body_volume_averages_six_weeks_per_group(fresh_db):
    client = await _client(fresh_db)
    await _train(111, "Грудь", 10, 1, "Bench A")
    await _train(111, "Грудь", 10, 20, "Bench B")
    await _train(111, "Ноги", 4, 3, "Squat")
    await _train(111, "Пресс", 3, 2, "Crunch")
    await _train(111, "Другое", 6, 2, "Shrug")
    # Старше шести недель — за окном.
    await _train(111, "Спина", 12, 50, "Row")

    body = (await client.get("/dashboard")).json()
    assert list(body["body_volume"]) == KEYS
    assert body["body_volume"] == {
        "chest": 3.3, "back": 0.0, "legs": 0.7, "shoulders": 0.0,
        "biceps": 0.0, "triceps": 0.0, "abs": 0.5,
    }
    # Индекс звания в лестнице — rank.level (RANKS[i].level == i).
    assert isinstance(body["rank"]["level"], int)


@pytest.mark.asyncio
async def test_body_volume_respects_excluded_workouts(fresh_db):
    client = await _client(fresh_db)
    await _train(111, "Ноги", 6, 1, "Squat")
    wid = await _train(111, "Грудь", 6, 2, "Bench")
    body = (await client.get(f"/dashboard?exclude_workout={wid}")).json()
    assert body["body_volume"]["chest"] == 0.0
    assert body["body_volume"]["legs"] == 1.0


def test_rank_level_is_the_ladder_index():
    import analytics

    assert [r.level for r in analytics.RANKS] == list(range(len(analytics.RANKS)))
    assert tuple(KEYS) == dashboard_data.BODY_VOLUME_GROUPS
