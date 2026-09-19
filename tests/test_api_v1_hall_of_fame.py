"""REST `/v1` для зала славы — api_v1_hall_of_fame.py.

Тот же приём, что у tests/test_api_v1_progress.py: httpx поверх ASGI-
приложения без сокета, `client_factory`/`_linked_client` заведены локально (в
чужой тестовый файл не лезем — его может в это же время править другой агент).
"""

import httpx
import pytest

import analytics
import api_v1
import db
import hall_of_fame_data

pytestmark = pytest.mark.asyncio


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def _log_session(user_id: int, ex_id: int, day: int, sets: list[tuple[float, int]]) -> int:
    workout_id = await db.create_finished_workout(
        user_id,
        started_at=f"2026-03-{day:02d}T10:00:00",
        finished_at=f"2026-03-{day:02d}T11:00:00",
    )
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for i, (weight, reps) in enumerate(sets):
        await db.add_set(block_id, ex_id, round_index=i + 1, order_in_round=0, weight=weight, reps=reps)
    return workout_id


async def test_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/hall-of-fame")
    assert resp.status_code == 401


async def test_empty_history_gives_null_not_error(fresh_db, client_factory):
    """Пустая история — не 200 из нулей и не падение, а осмысленное `null`,
    тем же приёмом, что и GET /dashboard: у новичка нет ни рекордов, ни
    тоннажа, ни звания выше стартового."""
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/hall-of-fame")
    assert resp.status_code == 200
    assert resp.json() is None


async def test_records_counted_over_whole_history_not_just_last_session(fresh_db, client_factory):
    """Рекорд по упражнению — из ЛЮБОЙ прошлой тренировки, а не только
    последней: тяжёлая сессия месяц назад не должна теряться, стоит человеку
    сходить полегче сегодня."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)

    await _log_session(user_id, ex_id, day=1, sets=[(100.0, 5)])
    await _log_session(user_id, ex_id, day=10, sets=[(60.0, 5)])

    resp = await client.get("/hall-of-fame")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_workouts"] == 2
    lifts = {lift["exercise"]: lift for lift in body["top_lifts"]}
    assert lifts["Жим лёжа"]["weight"] == 100.0
    assert lifts["Жим лёжа"]["is_bodyweight"] is False


async def test_bodyweight_exercise_records_by_reps_not_weight(fresh_db, client_factory):
    """Упражнение своим весом не имеет нагрузки для ранжирования — его рекорд
    это лучшие повторы, и он отмечен is_bodyweight, а не спрятан как нулевой
    вес."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Подтягивания", group_id)

    await _log_session(user_id, ex_id, day=1, sets=[(0.0, 8), (0.0, 12)])

    resp = await client.get("/hall-of-fame")
    body = resp.json()
    lift = next(lift for lift in body["top_lifts"] if lift["exercise"] == "Подтягивания")
    assert lift["is_bodyweight"] is True
    assert lift["weight"] is None
    assert lift["reps"] == 12


async def test_rank_and_tonnage_equivalent_are_present_and_localized(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    group_id = await db.create_muscle_group(user_id, "Ноги")
    ex_id = await db.create_exercise(user_id, "Присед", group_id)
    await _log_session(user_id, ex_id, day=1, sets=[(100.0, 5), (100.0, 5), (100.0, 5)])

    resp = await client.get("/hall-of-fame")
    body = resp.json()
    assert body["rank"]["name"]
    assert isinstance(body["rank"]["level"], int)
    assert body["tonnage"]["value"] > 0
    # Может быть None на очень маленьком тоннаже — эквивалент подбирается по
    # порогам, но само поле обязано присутствовать, а не отсутствовать.
    assert "equivalent" in body["tonnage"]


async def test_rank_ladder_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/hall-of-fame/rank-ladder")
    assert resp.status_code == 401


async def test_rank_ladder_shows_starting_rung_with_empty_history(fresh_db, client_factory):
    """В отличие от GET /hall-of-fame ладдер не отдаёт `null` на пустой
    истории — стартовая ступень (level 0) должна быть видна с первого дня."""
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/hall-of-fame/rank-ladder")
    assert resp.status_code == 200
    body = resp.json()
    assert body["current_level"] == 0
    assert body["per_week"] == 0.0
    assert len(body["ranks"]) == len(analytics.RANKS)


async def test_rank_ladder_thresholds_come_from_analytics_ranks(fresh_db, client_factory):
    """Пороги — те же числа, что и analytics.RANKS, без дублирования на
    сервере, не говоря уже о клиенте."""
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/hall-of-fame/rank-ladder")
    body = resp.json()
    for rung, rank in zip(body["ranks"], analytics.RANKS, strict=True):
        assert rung["level"] == rank.level
        assert rung["emoji"] == rank.emoji
        assert rung["name"] == rank.name
        assert rung["min_workouts"] == rank.min_workouts
        assert rung["min_tonnage_kg"] == rank.min_tonnage_kg
        assert rung["min_per_week"] == rank.min_per_week


async def test_rank_ladder_reflects_current_level_and_frequency(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    group_id = await db.create_muscle_group(user_id, "Ноги")
    ex_id = await db.create_exercise(user_id, "Присед", group_id)
    for day in (1, 3, 5, 8, 10):
        await _log_session(user_id, ex_id, day=day, sets=[(100.0, 5), (100.0, 5)])

    resp = await client.get("/hall-of-fame/rank-ladder")
    body = resp.json()
    hof = await hall_of_fame_data.collect(user_id)
    assert body["current_level"] == hof.rank.level
    assert body["per_week"] == round(hof.per_week, 2)
