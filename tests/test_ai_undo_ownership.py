"""Кнопка отката под ответом тренера (`ai_undo.apply`) — владелец и чужие данные.

id в описании отката приезжает из прошлого хода (у приложения — из таблицы
`ai_undo_actions`, у бота — из FSM), а между записью и тапом проходит сколько
угодно времени. Поэтому каждая ветка обязана заново проверить, что строка всё
ещё принадлежит этому человеку, и не трогать её, если нет: иначе откат одного
атлета переименовал бы, перенёс или снёс бы чужую программу. Эти ветки, а
также «упражнение уже с подходами — не сносить» и частичный пакет, раньше не
исполнялись ни одним тестом.
"""

import pytest

import ai_undo

pytestmark = pytest.mark.asyncio

ME, OTHER = 111, 222


async def _users(db):
    await db.get_or_create_user(telegram_id=ME, username="me")
    await db.get_or_create_user(telegram_id=OTHER, username="other")


async def _exercise(db, user_id, name="Жим лёжа"):
    gid = await db.create_muscle_group(user_id, f"Грудь {user_id}")
    return await db.create_exercise(user_id, name, gid), gid


async def test_exercise_name_undo_restores_own_and_ignores_foreign(fresh_db):
    db = fresh_db
    await _users(db)
    mine, _ = await _exercise(db, ME, "Жим новый")
    foreign, _ = await _exercise(db, OTHER, "Чужой жим")

    assert await ai_undo.apply(ME, {"kind": "exercise_name", "id": foreign, "name": "Взломан"}) is None
    assert (await db.get_exercise(foreign))["name"] == "Чужой жим"

    assert await ai_undo.apply(ME, {"kind": "exercise_name", "id": mine, "name": "Жим старый"})
    assert (await db.get_exercise(mine))["name"] == "Жим старый"


async def test_exercise_name_undo_on_deleted_exercise_is_none(fresh_db):
    db = fresh_db
    await _users(db)
    assert await ai_undo.apply(ME, {"kind": "exercise_name", "id": 999999, "name": "x"}) is None


async def test_exercise_group_undo_ignores_foreign(fresh_db):
    db = fresh_db
    await _users(db)
    foreign, foreign_gid = await _exercise(db, OTHER)
    my_gid = await db.create_muscle_group(ME, "Спина")

    assert await ai_undo.apply(ME, {"kind": "exercise_group", "id": foreign, "group_id": my_gid}) is None
    assert (await db.get_exercise(foreign))["primary_group_id"] == foreign_gid


async def test_exercise_group_undo_moves_own_back(fresh_db):
    db = fresh_db
    await _users(db)
    mine, _ = await _exercise(db, ME)
    back_gid = await db.create_muscle_group(ME, "Спина")

    assert await ai_undo.apply(ME, {"kind": "exercise_group", "id": mine, "group_id": back_gid, "name": "Спина"})
    assert (await db.get_exercise(mine))["primary_group_id"] == back_gid


async def test_program_and_day_rename_undo_ignore_foreign(fresh_db):
    db = fresh_db
    await _users(db)
    program = await db.create_program(OTHER, "Чужая программа")
    day = await db.create_routine(OTHER, "День A", program_id=program)

    assert await ai_undo.apply(ME, {"kind": "program_name", "id": program, "name": "Моя"}) is None
    assert await ai_undo.apply(ME, {"kind": "routine_name", "id": day, "name": "Мой день"}) is None
    assert (await db.get_program(program))["name"] == "Чужая программа"
    assert (await db.get_routine(day))["name"] == "День A"


async def test_program_and_day_rename_undo_restore_own(fresh_db):
    db = fresh_db
    await _users(db)
    program = await db.create_program(ME, "Новое имя")
    day = await db.create_routine(ME, "Новый день", program_id=program)

    assert await ai_undo.apply(ME, {"kind": "program_name", "id": program, "name": "Старое имя"})
    assert await ai_undo.apply(ME, {"kind": "routine_name", "id": day, "name": "Старый день"})
    assert (await db.get_program(program))["name"] == "Старое имя"
    assert (await db.get_routine(day))["name"] == "Старый день"


async def test_program_copy_undo_never_deletes_a_foreign_program(fresh_db):
    db = fresh_db
    await _users(db)
    foreign = await db.create_program(OTHER, "Чужая программа")
    await db.create_routine(OTHER, "День A", program_id=foreign)

    assert await ai_undo.apply(ME, {"kind": "program_new", "id": foreign}) is None
    assert await db.get_program(foreign) is not None


async def test_program_copy_undo_removes_own_copy_with_days(fresh_db):
    db = fresh_db
    await _users(db)
    ex, _ = await _exercise(db, ME)
    program = await db.create_program(ME, "Копия")
    day = await db.create_routine(ME, "День A", program_id=program)
    await db.add_routine_exercise(day, ex, 0, "4x8")

    assert await ai_undo.apply(ME, {"kind": "program_new", "id": program, "name": "Копия"})
    assert await db.get_program(program) is None
    assert await db.get_routine(day) is None
    # Само упражнение — не часть копии программы, оно остаётся.
    assert await db.get_exercise(ex) is not None


async def test_new_exercise_undo_keeps_exercise_that_already_has_sets(fresh_db):
    db = fresh_db
    await _users(db)
    ex, _ = await _exercise(db, ME, "Созданное тренером")
    # Законченная тренировка; открытая — отдельный найденный баг, см.
    # test_exercise_delete_guard_open_workout.py.
    workout = await db.create_finished_workout(
        ME, started_at="2026-10-01T10:00:00", finished_at="2026-10-01T11:00:00"
    )
    block = await db.create_block(workout, "single")
    await db.add_block_exercise(block, ex, 0)
    await db.add_set(block, ex, 0, 0, 60.0, 10, None)

    # По нему уже записан подход — снос унёс бы его вместе с историей.
    assert await ai_undo.apply(ME, {"kind": "exercise_new", "id": ex, "name": "Созданное тренером"}) is None
    assert await db.get_exercise(ex) is not None
    assert len(await db.list_sets_for_block(block)) == 1


async def test_new_exercise_undo_removes_unused_own_but_not_foreign(fresh_db):
    db = fresh_db
    await _users(db)
    mine, _ = await _exercise(db, ME, "Лишнее")
    foreign, _ = await _exercise(db, OTHER, "Чужое")

    assert await ai_undo.apply(ME, {"kind": "exercise_new", "id": foreign}) is None
    assert await db.get_exercise(foreign) is not None

    assert await ai_undo.apply(ME, {"kind": "exercise_new", "id": mine})
    assert await db.get_exercise(mine) is None


async def test_archive_suggestion_skips_foreign_template_and_already_archived(fresh_db):
    db = fresh_db
    await _users(db)
    mine, _ = await _exercise(db, ME, "В архив")
    done, _ = await _exercise(db, ME, "Уже в архиве")
    await db.archive_exercise(done)
    foreign, _ = await _exercise(db, OTHER, "Чужое")
    cur = await db.conn().execute("SELECT id FROM exercises WHERE is_template = 1 LIMIT 1")
    template = (await cur.fetchone())[0]

    text = await ai_undo.apply(ME, {"kind": "archive_exercises", "ids": [mine, done, foreign, template]})

    assert text
    assert (await db.get_exercise(mine))["is_archived"]
    assert not (await db.get_exercise(foreign))["is_archived"]
    assert not (await db.get_exercise(template))["is_archived"]


async def test_archive_suggestion_with_nothing_to_archive_still_answers(fresh_db):
    db = fresh_db
    await _users(db)
    foreign, _ = await _exercise(db, OTHER, "Чужое")
    # Ответ есть («уже в архиве»), но чужое не тронуто.
    assert await ai_undo.apply(ME, {"kind": "archive_exercises", "ids": [foreign]})
    assert not (await db.get_exercise(foreign))["is_archived"]


async def test_batch_reports_partial_undo_and_none_when_nothing_undone(fresh_db):
    db = fresh_db
    await _users(db)
    mine, _ = await _exercise(db, ME, "Новое имя")
    foreign, _ = await _exercise(db, OTHER, "Чужое")

    partial = await ai_undo.apply(ME, {"kind": "batch", "items": [
        {"kind": "exercise_name", "id": mine, "name": "Старое имя"},
        {"kind": "exercise_name", "id": foreign, "name": "Взломан"},
    ]})
    # Частичный откат не выдаёт себя за полный: в тексте есть «1 из 2».
    assert partial and "1" in partial and "2" in partial
    assert (await db.get_exercise(mine))["name"] == "Старое имя"
    assert (await db.get_exercise(foreign))["name"] == "Чужое"

    assert await ai_undo.apply(ME, {"kind": "batch", "items": [
        {"kind": "exercise_name", "id": foreign, "name": "Взломан"},
    ]}) is None


async def test_batch_undoes_in_reverse_order(fresh_db):
    """Ход создал упражнение и записал вес тела — снимать с конца."""
    db = fresh_db
    await _users(db)
    ex, _ = await _exercise(db, ME, "Новое")
    bw_id = await db.add_bodyweight_log(ME, 80.0)
    seen = []
    real_apply = ai_undo.apply

    async def _spy(user_id, undo):
        seen.append(undo.get("kind"))
        return await real_apply(user_id, undo)

    import unittest.mock as mock

    with mock.patch.object(ai_undo, "apply", _spy):
        await real_apply(ME, {"kind": "batch", "items": [
            {"kind": "exercise_new", "id": ex},
            {"kind": "bodyweight", "id": bw_id},
        ]})
    assert seen == ["bodyweight", "exercise_new"]


async def test_unknown_kind_does_nothing(fresh_db):
    await _users(fresh_db)
    assert await ai_undo.apply(ME, {"kind": "drop_database"}) is None
