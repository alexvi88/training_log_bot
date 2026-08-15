"""db.merge_exercises — combining two exercise entries that turned out to be
the same movement logged under different names (e.g. "ягодичный мостик" and
"glute bridge")."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_merge_rolls_back_on_mid_transaction_failure(fresh_db, user_id, monkeypatch):
    """A DML failing partway through must not leave sets repointed to keep_id
    while exercises/drop_id is still around — half-merged is worse than
    not-merged, and the caller (a retried merge) would see a stale MERGE_OK
    picture if the first attempt's partial UPDATE survived."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    keep_id = await db.create_exercise(user_id, "glute bridge", group_id)
    drop_id = await db.create_exercise(user_id, "ягодичный мостик", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, drop_id, 0)
    await db.append_set(block_id, drop_id, 0, 60.0, 10)
    await db.finish_workout(workout_id)

    real_execute = db.conn().execute

    async def _boom(sql, *args, **kwargs):
        if sql.startswith("DELETE FROM exercises"):
            raise RuntimeError("boom")
        return await real_execute(sql, *args, **kwargs)

    monkeypatch.setattr(db.conn(), "execute", _boom)

    with pytest.raises(RuntimeError):
        await db.merge_exercises(user_id, keep_id=keep_id, drop_id=drop_id)

    # Rolled back: the set is still on drop_id, and drop_id is still there.
    sets = await db.list_sets_for_block(block_id)
    assert sets[0]["exercise_id"] == drop_id
    assert await db.get_exercise(drop_id) is not None


async def _log_set(db, user_id, ex_id, weight=50.0, reps=8):
    """Логируем и ЗАКРЫВАЕМ тренировку: объединяют исторические дубли, а
    упражнение, открытое в текущей сессии, merge_exercises теперь отклоняет —
    её FSM держит id, который объединение удалило бы из-под неё."""
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.append_set(block_id, ex_id, 0, weight, reps)
    await db.finish_workout(workout_id)
    return workout_id, block_id


async def test_merge_moves_sets_and_deletes_source(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    keep_id = await db.create_exercise(user_id, "glute bridge", group_id)
    drop_id = await db.create_exercise(user_id, "ягодичный мостик", group_id)
    _workout_id, block_id = await _log_set(db, user_id, drop_id, weight=60.0, reps=10)

    outcome = await db.merge_exercises(user_id, keep_id=keep_id, drop_id=drop_id)

    assert outcome == db.MERGE_OK
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

    outcome = await db.merge_exercises(user_id, keep_id=ex_id, drop_id=ex_id)

    assert outcome == db.MERGE_INVALID


async def test_merge_rejects_exercise_belonging_to_another_user(fresh_db, user_id):
    db = fresh_db
    await db.get_or_create_user(telegram_id=222, username="other")
    group_id = await db.create_muscle_group(user_id, "Ноги")
    other_group_id = await db.create_muscle_group(222, "Ноги")
    keep_id = await db.create_exercise(user_id, "glute bridge", group_id)
    other_id = await db.create_exercise(222, "ягодичный мостик", other_group_id)

    outcome = await db.merge_exercises(user_id, keep_id=keep_id, drop_id=other_id)

    assert outcome == db.MERGE_INVALID
    assert await db.get_exercise(other_id) is not None


# ---------- коллизии: обе стороны уже лежат в одном контейнере ----------


async def test_merge_does_not_leave_the_survivor_twice_in_one_routine_day(fresh_db, user_id):
    """Объединяют как раз потому, что заметили обе строки рядом — то есть
    типичный случай и есть «оба в одном дне»."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    keep_id = await db.create_exercise(user_id, "glute bridge", group_id)
    drop_id = await db.create_exercise(user_id, "ягодичный мостик", group_id)
    routine_id = await db.create_routine(user_id, "Ноги")
    await db.add_routine_exercise(routine_id, keep_id, 0, "4×8")
    await db.add_routine_exercise(routine_id, drop_id, 1, "3×10")

    assert await db.merge_exercises(user_id, keep_id=keep_id, drop_id=drop_id) == db.MERGE_OK

    rows = await db.list_routine_exercises(routine_id)
    assert [(r["display_name"], r["target"]) for r in rows] == [("glute bridge", "4×8")]


async def test_the_surviving_slot_adopts_a_scheme_it_did_not_have(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    keep_id = await db.create_exercise(user_id, "glute bridge", group_id)
    drop_id = await db.create_exercise(user_id, "ягодичный мостик", group_id)
    routine_id = await db.create_routine(user_id, "Ноги")
    await db.add_routine_exercise(routine_id, keep_id, 0, None)
    await db.add_routine_exercise(routine_id, drop_id, 1, "3×10")

    await db.merge_exercises(user_id, keep_id=keep_id, drop_id=drop_id)

    rows = await db.list_routine_exercises(routine_id)
    assert [(r["display_name"], r["target"]) for r in rows] == [("glute bridge", "3×10")]


async def test_merge_does_not_leave_the_survivor_twice_in_one_superset(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    keep_id = await db.create_exercise(user_id, "glute bridge", group_id)
    drop_id = await db.create_exercise(user_id, "ягодичный мостик", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "superset")
    await db.add_block_exercise(block_id, keep_id, 0)
    await db.add_block_exercise(block_id, drop_id, 1)
    await db.append_set(block_id, keep_id, 0, 60.0, 10)
    await db.append_set(block_id, drop_id, 1, 65.0, 8)
    await db.finish_workout(workout_id)

    assert await db.merge_exercises(user_id, keep_id=keep_id, drop_id=drop_id) == db.MERGE_OK

    assert [be["exercise_id"] for be in await db.get_block_exercises(block_id)] == [keep_id]
    # Подходы обеих сторон при этом на месте.
    assert len(await db.list_sets_for_block(block_id)) == 2


async def test_the_same_exercise_cannot_be_inserted_into_a_block_twice(fresh_db, user_id):
    """Второй рубеж под коллизией — UNIQUE, а не только аккуратность merge."""
    import aiosqlite

    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    ex_id = await db.create_exercise(user_id, "glute bridge", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "superset")
    await db.add_block_exercise(block_id, ex_id, 0)

    with pytest.raises(aiosqlite.IntegrityError):
        await db.add_block_exercise(block_id, ex_id, 1)


# ---------- отказы ----------


async def test_merge_into_an_archived_exercise_is_refused(fresh_db, user_id):
    """Иначе вся история уезжает в архив, и человек видит, что упражнение
    просто исчезло — искать его в «🗄 Архив» он не пойдёт, он не знает, что
    его туда унесло."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    keep_id = await db.create_exercise(user_id, "glute bridge", group_id)
    drop_id = await db.create_exercise(user_id, "ягодичный мостик", group_id)
    await db.archive_exercise(keep_id)

    outcome = await db.merge_exercises(user_id, keep_id=keep_id, drop_id=drop_id)

    assert outcome == db.MERGE_TARGET_ARCHIVED
    assert await db.get_exercise(drop_id) is not None
    assert [e["display_name"] for e in await db.list_user_exercises(user_id)] == ["ягодичный мостик"]


async def test_merging_something_open_in_the_active_workout_is_refused(fresh_db, user_id):
    """Живой экран держит id упражнения в FSM — удалить строку из-под него
    значит оставить запись подхода указывающей в никуда."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    keep_id = await db.create_exercise(user_id, "glute bridge", group_id)
    drop_id = await db.create_exercise(user_id, "ягодичный мостик", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, drop_id, 0)

    outcome = await db.merge_exercises(user_id, keep_id=keep_id, drop_id=drop_id)

    assert outcome == db.MERGE_IN_ACTIVE_WORKOUT
    assert await db.get_exercise(drop_id) is not None

    # После завершения тренировки то же объединение проходит.
    await db.finish_workout(workout_id)
    assert await db.merge_exercises(user_id, keep_id=keep_id, drop_id=drop_id) == db.MERGE_OK
