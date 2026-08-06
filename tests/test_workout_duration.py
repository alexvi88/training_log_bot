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


async def test_get_workout_set_span_before_excludes_later_sets(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)

    await _log_set_at(db, block_id, bench, 1, "2026-06-26T18:00:00")
    await _log_set_at(db, block_id, bench, 2, "2026-06-26T18:05:00")
    await _log_set_at(db, block_id, bench, 3, "2026-06-26T20:19:00")

    span = await db.get_workout_set_span(workout_id, before="2026-06-26T18:45:00")
    assert span == ("2026-06-26T18:00:00", "2026-06-26T18:05:00")


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


async def test_workout_duration_seconds_ignores_sets_added_after_finish(fresh_db, user_id):
    """находка 25: editing a finished workout can add a set with a fresh
    timestamp long after the session (e.g. "Новое упражнение" in the editor,
    hours later). That set must not stretch the shown duration — the original
    live span (first set to last, both before finished_at) is what's real."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    workout_id = await db.create_workout(user_id, started_at="2026-06-26T18:00:00")
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)

    await _log_set_at(db, block_id, bench, 1, "2026-06-26T18:00:00")
    await _log_set_at(db, block_id, bench, 2, "2026-06-26T18:05:00")
    await db.finish_workout(workout_id, finished_at="2026-06-26T18:45:00")
    # Edited a couple of hours later — well under the old 6h implausibility cap,
    # which used to let this inflate the duration to "2 ч+" instead of catching it.
    await _log_set_at(db, block_id, bench, 3, "2026-06-26T20:19:00")

    workout = await db.get_workout(workout_id)
    assert await view_builder.workout_duration_seconds(workout) == 5 * 60


async def test_workout_duration_seconds_none_when_all_sets_predate_finish_but_span_implausible(
    fresh_db, user_id
):
    """The old 6h sanity cap still catches a genuinely implausible *live* span —
    this isn't about post-finish edits at all."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    workout_id = await db.create_workout(user_id, started_at="2026-06-26T09:00:00")
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)

    await _log_set_at(db, block_id, bench, 1, "2026-06-26T09:00:00")
    await _log_set_at(db, block_id, bench, 2, "2026-06-26T18:00:00")
    await db.finish_workout(workout_id, finished_at="2026-06-26T18:05:00")

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
