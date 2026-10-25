"""«Объединить» → «Разъединить»: возвращается ровно то, что переехало.

Существующие тесты разъединения (`test_import_overhaul.py`) гоняют только
подходы и описание. А журнал объединения хранит ещё места в суперсетах, дни
программ (в том числе общий день, где схема «5×5» досталась оставшемуся
упражнению), заметки к упражнению в тренировке и имена для импорта — и ни
одна из этих веток отката не исполнялась. Ошибка в любой из них тихо
оставляет программу или тренировку не такой, какой она была до объединения.
Здесь же — отказы разъединения: упражнение открыто в идущей тренировке, имя
уже занято новым упражнением, чужой журнал.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def _finished_block(db, user_id, exercise_ids, started_at):
    workout = await db.create_workout(user_id)
    block = await db.create_block(workout, "superset" if len(exercise_ids) > 1 else "single")
    for order, ex in enumerate(exercise_ids):
        await db.add_block_exercise(block, ex, order)
        await db.add_set(block, ex, 0, order, 50.0 + order, 10, None)
    await db.conn().execute("UPDATE workouts SET started_at = ? WHERE id = ?", (started_at, workout))
    await db.conn().commit()
    await db.finish_workout(workout)
    return workout, block


async def _routine_slots(db, routine_id):
    return [
        (r["exercise_id"], r["target"]) for r in await db.list_routine_exercises(routine_id)
    ]


async def test_unmerge_restores_supersets_program_days_notes_and_aliases(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Ноги")
    keep = await db.create_exercise(user_id, "Ягодичный мост", gid)
    drop = await db.create_exercise(user_id, "Хип-траст", gid)

    # Суперсет, где стоят оба (блок drop'а удаляется), и отдельная тренировка
    # только с drop (блок переезжает).
    shared_workout, shared_block = await _finished_block(db, user_id, [keep, drop], "2026-07-01T10:00:00")
    solo_workout, solo_block = await _finished_block(db, user_id, [drop], "2026-07-03T10:00:00")
    # Заметки: в общей тренировке есть у обоих (заметка drop'а уйдёт), в
    # отдельной — только у drop (переедет).
    await db.set_workout_exercise_note(shared_workout, keep, "заметка keep")
    await db.set_workout_exercise_note(shared_workout, drop, "заметка drop")
    await db.set_workout_exercise_note(solo_workout, drop, "только drop")
    # Дни программы: общий (у keep нет схемы, у drop «5x5») и только с drop.
    shared_day = await db.create_routine(user_id, "День A")
    await db.add_routine_exercise(shared_day, keep, 0, None)
    await db.add_routine_exercise(shared_day, drop, 1, "5x5")
    solo_day = await db.create_routine(user_id, "День B")
    await db.add_routine_exercise(solo_day, drop, 0, "3x12")
    # Имя из старого импорта, заведённое на drop.
    await db.set_exercise_alias(user_id, "Hip Thrust (Barbell)", drop, "import")

    before = {
        "shared_block": [be["exercise_id"] for be in await db.get_block_exercises(shared_block)],
        "solo_block": [be["exercise_id"] for be in await db.get_block_exercises(solo_block)],
        "shared_day": await _routine_slots(db, shared_day),
        "solo_day": await _routine_slots(db, solo_day),
    }

    journal: dict = {}
    assert await db.merge_exercises(user_id, keep, drop, journal_out=journal) == db.MERGE_OK
    # Объединение само по себе: схема досталась оставшемуся, алиас — ему же.
    assert await _routine_slots(db, shared_day) == [(keep, "5x5")]
    assert (await db.find_exercise_by_alias(user_id, "Hip Thrust (Barbell)"))["id"] == keep

    outcome, restored, moved = await db.undo_exercise_merge(user_id, journal["merge_id"])

    assert (outcome, restored) == (db.UNMERGE_OK, drop)
    assert moved == 2  # подход в суперсете и подход в отдельной тренировке
    assert [be["exercise_id"] for be in await db.get_block_exercises(shared_block)] == before["shared_block"]
    assert [be["exercise_id"] for be in await db.get_block_exercises(solo_block)] == before["solo_block"]
    assert sorted(await _routine_slots(db, shared_day), key=str) == sorted(before["shared_day"], key=str)
    assert await _routine_slots(db, solo_day) == before["solo_day"]
    assert await db.get_workout_exercise_note(shared_workout, keep) == "заметка keep"
    assert await db.get_workout_exercise_note(shared_workout, drop) == "заметка drop"
    assert await db.get_workout_exercise_note(solo_workout, drop) == "только drop"
    assert await db.get_workout_exercise_note(solo_workout, keep) is None
    assert (await db.find_exercise_by_alias(user_id, "Hip Thrust (Barbell)"))["id"] == drop
    # Подходы — у своих хозяев.
    for row in await db.list_sets_for_block(shared_block):
        assert row["exercise_id"] == (keep if row["weight"] == 50.0 else drop)
    assert {r["exercise_id"] for r in await db.list_sets_for_block(solo_block)} == {drop}


async def test_unmerge_is_refused_while_the_survivor_is_open_in_a_workout(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Грудь")
    keep = await db.create_exercise(user_id, "Жим", gid)
    drop = await db.create_exercise(user_id, "Жим лёжа", gid)
    await _finished_block(db, user_id, [drop], "2026-07-01T10:00:00")
    journal: dict = {}
    await db.merge_exercises(user_id, keep, drop, journal_out=journal)
    # Начал тренировку и открыл оставшееся упражнение — FSM держит его id.
    workout = await db.create_workout(user_id)
    block = await db.create_block(workout, "single")
    await db.add_block_exercise(block, keep, 0)

    outcome, restored, moved = await db.undo_exercise_merge(user_id, journal["merge_id"])

    assert (outcome, restored, moved) == (db.UNMERGE_IN_ACTIVE_WORKOUT, None, 0)
    assert await db.get_exercise(drop) is None
    # Журнал не погашен — после тренировки разъединить можно.
    await db.finish_workout(workout)
    assert (await db.undo_exercise_merge(user_id, journal["merge_id"]))[0] == db.UNMERGE_OK


async def test_unmerge_is_refused_when_the_name_was_taken_again(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Грудь")
    keep = await db.create_exercise(user_id, "Жим", gid)
    drop = await db.create_exercise(user_id, "Жим лёжа", gid)
    journal: dict = {}
    await db.merge_exercises(user_id, keep, drop, journal_out=journal)
    newcomer = await db.create_exercise(user_id, "Жим лёжа", gid)

    outcome, restored, _ = await db.undo_exercise_merge(user_id, journal["merge_id"])

    assert (outcome, restored) == (db.UNMERGE_NAME_TAKEN, None)
    assert (await db.get_exercise(newcomer))["display_name"] == "Жим лёжа"


async def test_foreign_merge_journal_is_not_found(fresh_db, user_id):
    db = fresh_db
    await db.get_or_create_user(telegram_id=222, username="other")
    gid = await db.create_muscle_group(222, "Грудь")
    keep = await db.create_exercise(222, "Жим", gid)
    drop = await db.create_exercise(222, "Жим лёжа", gid)
    journal: dict = {}
    await db.merge_exercises(222, keep, drop, journal_out=journal)

    assert (await db.undo_exercise_merge(user_id, journal["merge_id"]))[0] == db.UNMERGE_NOT_FOUND
    assert await db.get_exercise(drop) is None
