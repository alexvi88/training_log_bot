import pytest

import view_builder

pytestmark = pytest.mark.asyncio


async def _log_set_at(db, block_id, exercise_id, round_index, created_at):
    await db.add_set(block_id, exercise_id, round_index, 0, 100.0, 5)
    await db.conn().execute(
        "UPDATE sets SET created_at = ? WHERE id = (SELECT MAX(id) FROM sets)", (created_at,)
    )
    await db.conn().commit()


async def test_get_workout_set_span_none_without_sets(fresh_db, user_id):
    db = fresh_db
    workout_id = await db.create_workout(user_id)
    assert await db.get_workout_set_span(workout_id) is None


async def test_get_workout_set_span_spans_first_and_last_set(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)

    await _log_set_at(db, block_id, bench, 1, "2026-06-26T18:00:00")
    await _log_set_at(db, block_id, bench, 2, "2026-06-26T18:05:00")
    await _log_set_at(db, block_id, bench, 3, "2026-06-26T18:47:00")

    span = await db.get_workout_set_span(workout_id)
    assert span == ("2026-06-26T18:00:00", "2026-06-26T18:47:00")


async def test_workout_duration_seconds_computed_for_live_workout(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    workout_id = await db.create_workout(user_id, started_at="2026-06-26T18:00:00")
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)

    await _log_set_at(db, block_id, bench, 1, "2026-06-26T18:00:30")
    await _log_set_at(db, block_id, bench, 2, "2026-06-26T18:45:30")
    await db.finish_workout(workout_id, finished_at="2026-06-26T18:50:00")

    workout = await db.get_workout(workout_id)
    duration = await view_builder.workout_duration_seconds(workout)
    assert duration == 45 * 60


async def test_workout_duration_seconds_none_when_span_implausibly_long(fresh_db, user_id):
    """A finished workout edited days later (adding a set) leaves a fresh-timestamped
    set far from the original session — the span shouldn't be shown as the duration."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    workout_id = await db.create_workout(user_id, started_at="2026-06-26T18:00:00")
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)

    await _log_set_at(db, block_id, bench, 1, "2026-06-26T18:00:00")
    await db.finish_workout(workout_id, finished_at="2026-06-26T18:45:00")
    await _log_set_at(db, block_id, bench, 2, "2026-07-03T09:00:00")  # added via edit, days later

    workout = await db.get_workout(workout_id)
    assert await view_builder.workout_duration_seconds(workout) is None


async def test_workout_duration_seconds_none_for_backfilled_workout(fresh_db, user_id):
    """Backfilled workouts have started_at == finished_at (no live FSM ran), so the
    gap between logged sets only reflects data-entry time, not the real session."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    workout_id = await db.create_workout(user_id, started_at="2026-06-20T12:00:00")
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)

    await _log_set_at(db, block_id, bench, 1, "2026-06-26T09:00:00")
    await _log_set_at(db, block_id, bench, 2, "2026-06-26T09:02:00")
    await db.finish_workout(workout_id, finished_at="2026-06-20T12:00:00")

    workout = await db.get_workout(workout_id)
    assert await view_builder.workout_duration_seconds(workout) is None
