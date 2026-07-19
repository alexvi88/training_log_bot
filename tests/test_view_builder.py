"""Regression test: an exercise added to a workout but never given a set must
still show up when the workout is viewed (finished-workout summary, history,
etc.) instead of silently disappearing."""
import datetime as dt

import pytest

import formatting
import view_builder

pytestmark = pytest.mark.asyncio


async def test_exercise_without_sets_is_not_dropped(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    squat = await db.create_exercise(user_id, "Squat", group_id)
    workout_id = await db.create_workout(user_id)

    block_with_set = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_with_set, bench, 0)
    await db.add_set(block_with_set, bench, 1, 0, 100, 5)

    empty_block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(empty_block, squat, 0)

    blocks = await view_builder.build_block_views(workout_id)

    names = [b.exercise_name for b in blocks]
    assert "Squat" in names
    assert "Bench press" in names

    squat_block = next(b for b in blocks if b.exercise_name == "Squat")
    assert squat_block.sets == []

    summary = formatting.build_workout_summary(dt.datetime.now(), blocks)
    assert "Squat" in summary
