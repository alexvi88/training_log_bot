"""Перенос тренировки на другой день (workout_edit_data.move_workout_to_date)
по часовому поясу пользователя.

started_at хранится по часам сервера (UTC), а день, на который переносят,
человек выбирает по своим местным часам — в календаре бота и в приложении
(PATCH /v1/workouts/{id}/date). Склейка выбранной даты с UTC-временем
напрямую уводила тренировку на соседние сутки всем, у кого местная полночь
не совпадает с UTC.
"""
import datetime as dt

import pytest

import workout_edit_data

pytestmark = pytest.mark.asyncio


async def _workout(db, user_id: int, started: str, finished: str) -> int:
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим", group_id)
    workout_id = await db.create_finished_workout(user_id, started, finished)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, round_index=1, order_in_round=0, weight=100.0, reps=8)
    await db.conn().execute("UPDATE sets SET created_at = ? WHERE block_id = ?", (started, block_id))
    await db.conn().commit()
    return workout_id


async def _set_times(db, workout_id: int) -> list[dt.datetime]:
    cur = await db.conn().execute(
        "SELECT s.created_at FROM sets s JOIN workout_blocks b ON b.id = s.block_id WHERE b.workout_id = ?",
        (workout_id,),
    )
    # Метки подходов сдвигает SQLite-овский datetime(), он пишет их через
    # пробел, а не через «T» — сравниваем моменты, а не строки.
    return [dt.datetime.fromisoformat(row["created_at"]) for row in await cur.fetchall()]


async def test_move_keeps_the_local_day_east_of_utc(fresh_db, user_id):
    """UTC+10: тренировка в 08:00 местного 7 августа — это 22:00 UTC 6-го.
    Перенос на 1 августа должен дать 08:00 местного 1-го, то есть 22:00 UTC
    31 июля, а не 22:00 UTC 1-го (08:00 местного уже 2-го)."""
    db = fresh_db
    await db.update_user(user_id, tz_offset=10)
    workout_id = await _workout(db, user_id, "2026-08-06T22:00:00", "2026-08-06T23:00:00")

    await workout_edit_data.move_workout_to_date(workout_id, dt.date(2026, 8, 1))

    workout = await db.get_workout(workout_id)
    assert workout["started_at"] == "2026-07-31T22:00:00"
    assert workout["finished_at"] == "2026-07-31T23:00:00"
    assert await _set_times(db, workout_id) == [dt.datetime(2026, 7, 31, 22, 0)]


async def test_move_keeps_the_local_day_west_of_utc(fresh_db, user_id):
    """UTC-5: тренировка в 21:00 местного 7 августа — это 02:00 UTC 8-го.
    Перенос на 1 августа должен дать 21:00 местного 1-го, то есть 02:00 UTC
    2 августа, а не 02:00 UTC 1-го (21:00 местного 31 июля)."""
    db = fresh_db
    await db.update_user(user_id, tz_offset=-5)
    workout_id = await _workout(db, user_id, "2026-08-08T02:00:00", "2026-08-08T03:00:00")

    await workout_edit_data.move_workout_to_date(workout_id, dt.date(2026, 8, 1))

    workout = await db.get_workout(workout_id)
    assert workout["started_at"] == "2026-08-02T02:00:00"
    assert workout["finished_at"] == "2026-08-02T03:00:00"
    assert await _set_times(db, workout_id) == [dt.datetime(2026, 8, 2, 2, 0)]


async def test_move_in_utc_is_unchanged(fresh_db, user_id):
    """Смещение 0 — прежнее поведение: переносится день, время суток то же."""
    db = fresh_db
    await db.update_user(user_id, tz_offset=0)
    workout_id = await _workout(db, user_id, "2026-08-07T10:00:00", "2026-08-07T11:00:00")

    await workout_edit_data.move_workout_to_date(workout_id, dt.date(2026, 8, 1))

    workout = await db.get_workout(workout_id)
    assert workout["started_at"] == "2026-08-01T10:00:00"
    assert workout["finished_at"] == "2026-08-01T11:00:00"
