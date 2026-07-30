"""An exercise note ("!болит плечо", set via "📝 Заметка") belongs to the
specific (workout, exercise) pair it was written for — not to the exercise in
general. Writing a note in one workout must not resurface it in an unrelated
workout that happens to log the same exercise.
"""
import pytest

import view_builder

pytestmark = pytest.mark.asyncio


async def test_note_is_scoped_to_its_own_workout(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Pull down", group_id)
    w1 = await db.create_workout(user_id)
    w2 = await db.create_workout(user_id)

    await db.set_workout_exercise_note(w1, ex_id, "right shoulder discomfort")

    assert await db.get_workout_exercise_note(w1, ex_id) == "right shoulder discomfort"
    assert await db.get_workout_exercise_note(w2, ex_id) is None


async def test_clearing_a_note_only_affects_its_own_workout(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Pull down", group_id)
    w1 = await db.create_workout(user_id)
    w2 = await db.create_workout(user_id)
    await db.set_workout_exercise_note(w1, ex_id, "note one")
    await db.set_workout_exercise_note(w2, ex_id, "note two")

    await db.set_workout_exercise_note(w1, ex_id, None)

    assert await db.get_workout_exercise_note(w1, ex_id) is None
    assert await db.get_workout_exercise_note(w2, ex_id) == "note two"


async def test_list_workout_notes_for_exercise_returns_all_workouts(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Pull down", group_id)
    w1 = await db.create_workout(user_id)
    w2 = await db.create_workout(user_id)
    await db.set_workout_exercise_note(w1, ex_id, "note one")
    await db.set_workout_exercise_note(w2, ex_id, "note two")

    notes = await db.list_workout_notes_for_exercise(ex_id)
    assert notes == {w1: "note one", w2: "note two"}


async def test_build_block_views_only_shows_that_workouts_own_note(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Pull down", group_id)

    w1 = await db.create_workout(user_id)
    block1 = await db.create_block(w1, "single")
    await db.add_block_exercise(block1, ex_id, 0)
    await db.add_set(block1, ex_id, 0, 0, 100.0, 8, None)
    await db.set_workout_exercise_note(w1, ex_id, "right shoulder discomfort")

    w2 = await db.create_workout(user_id)
    block2 = await db.create_block(w2, "single")
    await db.add_block_exercise(block2, ex_id, 0)
    await db.add_set(block2, ex_id, 0, 0, 97.5, 8, None)

    views1 = await view_builder.build_block_views(w1)
    views2 = await view_builder.build_block_views(w2)

    assert views1[0].note == "right shoulder discomfort"
    assert views2[0].note is None
