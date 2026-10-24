"""REST `/v1/dashboard/timeline` — «ТЫ» по дням истории для видео «Как ты
рос» (dashboard_data.you_timeline). Только настоящие данные: точка — день с
тренировкой, последняя — сегодня; звание, тело, тоннаж и рекорды к каждому
дню — тем же счётом, что сводка сегодня."""

import datetime as dt

import httpx
import pytest

import api_v1
import dashboard_data
import db
import timeutil

pytestmark = pytest.mark.asyncio


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")


async def _linked_client(fresh_db, telegram_id=111, lang="ru"):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    await fresh_db.set_user_lang(telegram_id, lang)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = _client()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _session(user_id: int, ex_id: int, day: dt.date, sets: list[tuple[float, int]]) -> None:
    workout_id = await db.create_finished_workout(
        user_id, started_at=f"{day.isoformat()}T10:00:00", finished_at=f"{day.isoformat()}T11:00:00"
    )
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for i, (weight, reps) in enumerate(sets):
        await db.add_set(block_id, ex_id, round_index=i + 1, order_in_round=0, weight=weight, reps=reps)


async def test_requires_auth(fresh_db):
    resp = await _client().get("/dashboard/timeline")
    assert resp.status_code == 401


async def test_no_workouts_gives_null(fresh_db):
    client = await _linked_client(fresh_db)
    resp = await client.get("/dashboard/timeline")
    assert resp.status_code == 200
    assert resp.json() is None


async def test_points_are_real_days_and_end_today(fresh_db):
    user_id = 111
    client = await _linked_client(fresh_db, user_id)
    today = timeutil.user_today(await db.get_user(user_id))
    chest = await db.create_muscle_group(user_id, "Грудь")
    legs = await db.create_muscle_group(user_id, "Ноги")
    bench = await db.create_exercise(user_id, "Жим лёжа", chest)
    squat = await db.create_exercise(user_id, "Присед", legs)
    first = today - dt.timedelta(days=100)
    await _session(user_id, bench, first, [(50, 5)])
    await _session(user_id, bench, first + dt.timedelta(days=6), [(55, 5), (55, 5)])
    await _session(user_id, squat, first + dt.timedelta(days=6), [(80, 5)])
    await _session(user_id, bench, first + dt.timedelta(days=40), [(60, 5)])

    resp = await client.get("/dashboard/timeline")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["first_date"] == first.isoformat()
    assert body["unit"] == "kg"
    points = body["points"]
    # Два дня в один — одна точка; сегодня — последняя, без тренировки.
    assert [p["day"] for p in points] == [1, 7, 41, 101]
    assert [p["workouts"] for p in points] == [1, 3, 4, 4]
    assert [p["tonnage"] for p in points] == [250.0, 1200.0, 1500.0, 1500.0]
    # Первая попытка движения — не рекорд; 55 и 60 в жиме — рекорды.
    assert [p["records"] for p in points] == [0, 1, 2, 2]
    assert points[0]["rank"]["level"] == 0
    assert points[0]["rank"]["name"]
    # Тело — окно восьми недель по этот день: в день 41 жим дня 1 ещё в окне,
    # а сегодня (день 101) всё старше восьми недель — фигура по нулям.
    assert points[1]["body_volume"]["chest"] == round(3 / 8, 1)
    assert points[1]["body_volume"]["legs"] == round(1 / 8, 1)
    assert points[2]["body_volume"]["chest"] == round(4 / 8, 1)
    assert set(points[-1]["body_volume"]) == set(dashboard_data.BODY_VOLUME_GROUPS)
    assert all(v == 0 for v in points[-1]["body_volume"].values())


async def test_last_point_matches_dashboard_rank_and_body(fresh_db):
    """Конец видео — тот же «ТЫ», что в подвале сегодня: звание и тело
    последней точки совпадают со сводкой."""
    user_id = 111
    client = await _linked_client(fresh_db, user_id, lang="en")
    today = timeutil.user_today(await db.get_user(user_id))
    chest = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", chest)
    for back in (20, 15, 10, 5, 0):
        await _session(user_id, bench, today - dt.timedelta(days=back), [(100, 5)] * 5)

    timeline = (await client.get("/dashboard/timeline")).json()
    dashboard = (await client.get("/dashboard")).json()
    last = timeline["points"][-1]
    assert last["day"] == 21
    assert last["rank"] == {"level": dashboard["rank"]["level"], "name": dashboard["rank"]["name"]}
    assert last["body_volume"] == dashboard["body_volume"]
    assert last["workouts"] == 5
