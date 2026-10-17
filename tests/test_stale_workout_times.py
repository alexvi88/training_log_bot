"""Время начала и конца зависшей тренировки — и в боте, и в /v1.

1. «Завершить задним числом» в приложении (POST /v1/workouts/{id}/finish с
   `backdated`) закрывает тренировку по тому же правилу, что кнопка бота
   (db.backdated_finished_at): конец = начало, длительности нет. Без флага —
   как раньше, текущим моментом (так шлют уже вышедшие сборки).
2. Брошенная пустая активная тренировка, подхваченная новым стартом
   (db.get_or_create_active_workout — его зовут и бот, и POST /workouts/active),
   начинается заново: её started_at — этот старт, а не давнее открытие.

httpx поверх ASGI без сокета — по образцу tests/test_api_v1_finish_rewards.py.
"""

import datetime as dt

import httpx
import pytest

import api_v1
import config

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
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


def _hours_ago(hours: float) -> str:
    return (dt.datetime.now() - dt.timedelta(hours=hours)).isoformat()


async def _set_started_at(db, workout_id: int, started_at: str) -> None:
    await db.conn().execute(
        "UPDATE workouts SET started_at = ? WHERE id = ?", (started_at, workout_id)
    )
    await db.conn().commit()


async def _stale_workout_with_a_set(db, client, hours: float) -> tuple[int, str]:
    bench = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    resp = await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": bench, "weight": 80, "reps": 5}
    )
    assert resp.status_code in (200, 201), resp.text
    started = _hours_ago(hours)
    await _set_started_at(db, workout_id, started)
    return workout_id, started


# ---------- «Завершить задним числом» ----------


async def test_backdated_finish_ends_at_start_like_the_bot(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    workout_id, started = await _stale_workout_with_a_set(
        fresh_db, client, config.STALE_WORKOUT_HOURS + 20
    )

    resp = await client.post(f"/workouts/{workout_id}/finish", json={"backdated": True})

    assert resp.status_code == 200, resp.text
    saved = await fresh_db.get_workout(workout_id)
    assert saved["status"] == "finished"
    assert saved["finished_at"] == started
    assert saved["finished_at"] == fresh_db.backdated_finished_at(saved)
    # Сутки «тренировки» не превращаются в «Марафонца»: длительность не
    # известна и в значки не идёт.
    assert "marathon" not in await fresh_db.list_achievement_codes(111)


async def test_finish_without_backdated_still_ends_now(fresh_db, client_factory):
    """Уже вышедшие сборки поля не шлют — для них ничего не меняется."""
    client = await _linked_client(fresh_db, client_factory)
    workout_id, started = await _stale_workout_with_a_set(fresh_db, client, 3)

    resp = await client.post(f"/workouts/{workout_id}/finish", json={})

    assert resp.status_code == 200, resp.text
    finished = dt.datetime.fromisoformat((await fresh_db.get_workout(workout_id))["finished_at"])
    assert finished > dt.datetime.fromisoformat(started) + dt.timedelta(hours=2)
    # Единственный подход записан только что: от первого подхода до
    # последнего — ноль, и «Марафонца» за три часа открытого экрана нет
    # (правило view_builder.workout_duration_seconds, как у бота).
    assert "marathon" not in await fresh_db.list_achievement_codes(111)


# ---------- «Марафонец» — та же длительность, что в итогах ----------


def _utc_ago(minutes: float) -> str:
    """Наивный UTC, как сервер хранит started_at и created_at подходов."""
    moment = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=minutes)
    return moment.isoformat(timespec="seconds")


async def _live_workout(db, client, *, opened_min: float, set_minutes_ago: list[float]) -> int:
    """Живая тренировка: «Начать» нажали `opened_min` минут назад, подходы
    легли в моменты `set_minutes_ago` (их created_at — как у офлайн-досылки)."""
    bench = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await _set_started_at(db, workout_id, _utc_ago(opened_min))
    for ago in set_minutes_ago:
        resp = await client.post(
            f"/workouts/{workout_id}/sets",
            json={"exercise_id": bench, "weight": 80, "reps": 5, "created_at": _utc_ago(ago) + "Z"},
        )
        assert resp.status_code in (200, 201), resp.text
    return workout_id


async def test_marathon_ignores_time_before_the_first_set(fresh_db, client_factory):
    """Живой баг: итоги в приложении показали «1h 12m», а тем же ответом finish
    приехал «Марафонец · длиннее 2 часов». Экран тренировки открыли за 2.5 часа
    до конца, первый подход — за 72 минуты: итоги считают от первого подхода,
    значок считал от started_at."""
    client = await _linked_client(fresh_db, client_factory)
    workout_id = await _live_workout(
        fresh_db, client, opened_min=150, set_minutes_ago=[72, 40, 1]
    )

    resp = await client.post(f"/workouts/{workout_id}/finish", json={})

    assert resp.status_code == 200, resp.text
    codes = {b["code"] for b in resp.json()["rewards"]["new_achievements"]}
    assert "marathon" not in codes
    assert "marathon" not in await fresh_db.list_achievement_codes(111)


async def test_marathon_still_earned_by_a_real_two_hour_session(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    workout_id = await _live_workout(
        fresh_db, client, opened_min=150, set_minutes_ago=[130, 60, 1]
    )

    resp = await client.post(f"/workouts/{workout_id}/finish", json={})

    assert resp.status_code == 200, resp.text
    codes = {b["code"] for b in resp.json()["rewards"]["new_achievements"]}
    assert "marathon" in codes
    assert "marathon" in await fresh_db.list_achievement_codes(111)


async def test_backdated_false_is_a_normal_finish(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    workout_id, started = await _stale_workout_with_a_set(fresh_db, client, 3)

    resp = await client.post(f"/workouts/{workout_id}/finish", json={"backdated": False})

    assert resp.status_code == 200, resp.text
    assert (await fresh_db.get_workout(workout_id))["finished_at"] != started


async def test_backdated_must_be_a_boolean(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    workout_id, _ = await _stale_workout_with_a_set(fresh_db, client, 3)

    resp = await client.post(f"/workouts/{workout_id}/finish", json={"backdated": "yes"})

    assert resp.status_code == 400, resp.text
    assert (await fresh_db.get_workout(workout_id))["status"] == "active"


# ---------- брошенная пустая тренировка при новом старте ----------


async def test_reused_empty_workout_starts_now(fresh_db, user_id):
    db = fresh_db
    workout_id = await db.create_workout(user_id, started_at=_hours_ago(30))

    before = dt.datetime.now()
    reused_id, created = await db.get_or_create_active_workout(user_id)

    assert (reused_id, created) == (workout_id, False)
    started = dt.datetime.fromisoformat((await db.get_workout(workout_id))["started_at"])
    assert started >= before - dt.timedelta(seconds=1)


async def test_reused_empty_workout_with_an_open_exercise_starts_now(fresh_db, user_id):
    """Выбранное, но не начатое упражнение — ещё не тренировка: подходов нет."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    workout_id = await db.create_workout(user_id, started_at=_hours_ago(30))
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)

    await db.get_or_create_active_workout(user_id)

    started = dt.datetime.fromisoformat((await db.get_workout(workout_id))["started_at"])
    assert dt.datetime.now() - started < dt.timedelta(minutes=1)
    assert await db.list_blocks_for_workout(workout_id)  # план не сносится


async def test_workout_with_sets_keeps_its_start(fresh_db, user_id):
    """С подходами это «Продолжить» идущей тренировки — её время не трогаем."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    started = _hours_ago(1)
    workout_id = await db.create_workout(user_id, started_at=started)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)
    await db.add_set(block_id, bench, 1, 0, 100, 8)

    await db.get_or_create_active_workout(user_id)

    assert (await db.get_workout(workout_id))["started_at"] == started


async def test_reused_empty_workout_does_not_touch_backfill(fresh_db, user_id):
    """Занесение задним числом — отдельный статус и своя дата, не «активная»."""
    db = fresh_db
    backfill_id, _ = await db.get_or_create_backfill_workout(user_id, "2026-01-05T12:00:00")

    await db.get_or_create_active_workout(user_id)

    assert (await db.get_workout(backfill_id))["started_at"] == "2026-01-05T12:00:00"


async def test_api_start_over_abandoned_empty_workout_returns_fresh_start(
    fresh_db, client_factory
):
    client = await _linked_client(fresh_db, client_factory)
    first = (await client.post("/workouts/active")).json()["id"]
    await _set_started_at(fresh_db, first, _hours_ago(config.STALE_WORKOUT_HOURS + 20))

    resp = await client.post("/workouts/active")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == first
    started = dt.datetime.fromisoformat((await fresh_db.get_workout(first))["started_at"])
    assert dt.datetime.now() - started < dt.timedelta(minutes=1)
    assert body["started_at"] == (await fresh_db.get_workout(first))["started_at"]
