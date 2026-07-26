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


async def test_same_exercise_in_two_blocks_is_merged_into_one_view(fresh_db, user_id):
    """Logging an exercise as two separate blocks in one workout (e.g. 2 sets up
    front, 2 more at the end) is a legitimate entry pattern — but the summary,
    its e1RM and any "прошлая" comparison must treat it as a single exercise,
    not two disconnected rows with split stats."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    row = await db.create_exercise(user_id, "Barbell row", group_id)
    workout_id = await db.create_workout(user_id)

    first_block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(first_block, row, 0)
    await db.add_set(first_block, row, 1, 0, 60, 8)
    await db.add_set(first_block, row, 2, 0, 60, 8)

    second_block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(second_block, row, 0)
    await db.add_set(second_block, row, 1, 0, 65, 6)

    blocks = await view_builder.build_block_views(workout_id)

    assert len(blocks) == 1
    merged = blocks[0]
    assert merged.sets == [(60.0, 8), (60.0, 8), (65.0, 6)]
    assert merged.tonnage == 60 * 8 + 60 * 8 + 65 * 6
