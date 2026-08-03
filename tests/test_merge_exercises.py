"""db.merge_exercises — combining two exercise entries that turned out to be
the same movement logged under different names (e.g. "ягодичный мостик" and
"glute bridge")."""
import pytest

pytestmark = pytest.mark.asyncio


async def _log_set(db, user_id, ex_id, weight=50.0, reps=8):
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.append_set(block_id, ex_id, 0, weight, reps)
    return workout_id, block_id


async def test_merge_moves_sets_and_deletes_source(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    keep_id = await db.create_exercise(user_id, "glute bridge", group_id)
    drop_id = await db.create_exercise(user_id, "ягодичный мостик", group_id)
    _workout_id, block_id = await _log_set(db, user_id, drop_id, weight=60.0, reps=10)

    ok = await db.merge_exercises(user_id, keep_id=keep_id, drop_id=drop_id)

    assert ok is True
    assert await db.get_exercise(drop_id) is None
    sets = await db.list_sets_for_block(block_id)
    assert len(sets) == 1
    assert sets[0]["weight"] == 60.0
    assert sets[0]["exercise_id"] == keep_id
    block_exs = await db.get_block_exercises(block_id)
    assert [be["exercise_id"] for be in block_exs] == [keep_id]


async def test_merge_moves_routine_slots(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    keep_id = await db.create_exercise(user_id, "glute bridge", group_id)
    drop_id = await db.create_exercise(user_id, "ягодичный мостик", group_id)
    routine_id = await db.create_routine(user_id, "Программа A")
    await db.add_routine_exercise(routine_id, drop_id, 0)

    await db.merge_exercises(user_id, keep_id=keep_id, drop_id=drop_id)

    routine_exs = await db.list_routine_exercises(routine_id)
    assert [re["exercise_id"] for re in routine_exs] == [keep_id]


async def test_merge_prefers_keeps_note_on_workout_note_collision(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    keep_id = await db.create_exercise(user_id, "glute bridge", group_id)
    drop_id = await db.create_exercise(user_id, "ягодичный мостик", group_id)
    workout_id = await db.create_workout(user_id)
    await db.set_workout_exercise_note(workout_id, keep_id, "keep's note")
    await db.set_workout_exercise_note(workout_id, drop_id, "drop's note")

    await db.merge_exercises(user_id, keep_id=keep_id, drop_id=drop_id)

    assert await db.get_workout_exercise_note(workout_id, keep_id) == "keep's note"


async def test_merge_carries_over_description_and_photo_when_keep_has_none(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    keep_id = await db.create_exercise(user_id, "glute bridge", group_id)
    drop_id = await db.create_exercise(user_id, "ягодичный мостик", group_id)
    await db.set_exercise_description(drop_id, "техника выполнения")
    await db.set_exercise_photo(drop_id, "FILE_ID")

    await db.merge_exercises(user_id, keep_id=keep_id, drop_id=drop_id)

    kept = await db.get_exercise(keep_id)
    assert kept["description"] == "техника выполнения"
    assert kept["custom_photo_file_id"] == "FILE_ID"


async def test_merge_rejects_same_exercise(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    ex_id = await db.create_exercise(user_id, "glute bridge", group_id)

    ok = await db.merge_exercises(user_id, keep_id=ex_id, drop_id=ex_id)

    assert ok is False


async def test_merge_rejects_exercise_belonging_to_another_user(fresh_db, user_id):
    db = fresh_db
    await db.get_or_create_user(telegram_id=222, username="other")
    group_id = await db.create_muscle_group(user_id, "Ноги")
    other_group_id = await db.create_muscle_group(222, "Ноги")
    keep_id = await db.create_exercise(user_id, "glute bridge", group_id)
    other_id = await db.create_exercise(222, "ягодичный мостик", other_group_id)

    ok = await db.merge_exercises(user_id, keep_id=keep_id, drop_id=other_id)

    assert ok is False
    assert await db.get_exercise(other_id) is not None
