"""Программа как самостоятельная сущность, а не строка, которую делят её дни.

До `programs` идентичностью программы было ровно значение
`routines.program_name`, и всё, что могло совпасть по имени, совпадало: две
программы с одним именем были одной программой, переименование одной в имя
другой их сливало, повторное добавление того же сплита набивало в него дубли
дней, а «ручкой» экрана служил MAX(routine.id). Тесты ниже — про то, что
каждое из этих состояний теперь недостижимо.
"""
import asyncio

import pytest

import config

pytestmark = pytest.mark.asyncio


async def _program_with_days(db, user_id, name, day_names, source="manual"):
    program_id = await db.create_program(user_id, name, source=source)
    for day in day_names:
        await db.create_routine(user_id, day, program_id=program_id)
    return program_id


# ---------- идентичность и коллизии имён ----------


async def test_two_programs_cannot_share_a_name(fresh_db, user_id):
    db = fresh_db
    assert await db.create_program(user_id, "PPL") is not None
    assert await db.create_program(user_id, "PPL") is None


async def test_name_collision_folds_case_and_whitespace(fresh_db, user_id):
    """Индекс сворачивает регистр, а старый резолвер AI-тренера сравнивал имена
    в нижнем регистре при том, что группировка была регистрозависимой — из-за
    чего правка «Сплит» могла удалить «сплит»."""
    db = fresh_db
    await db.create_program(user_id, "Сплит")
    assert await db.create_program(user_id, "сплит") is None
    assert await db.create_program(user_id, "  СПЛИТ  ") is None
    found = await db.find_program_by_name(user_id, "сПлИт")
    assert found is not None and found["name"] == "Сплит"


async def test_another_users_program_of_the_same_name_is_untouched(fresh_db, user_id):
    db = fresh_db
    await db.get_or_create_user(telegram_id=222, username="other")
    assert await db.create_program(user_id, "PPL") is not None
    assert await db.create_program(222, "PPL") is not None
    assert len(await db.list_programs(user_id)) == 1
    assert len(await db.list_programs(222)) == 1


async def test_rename_onto_an_existing_name_is_refused_not_merged(fresh_db, user_id):
    db = fresh_db
    alpha = await _program_with_days(fresh_db, user_id, "Альфа", ["A1", "A2"])
    beta = await _program_with_days(fresh_db, user_id, "Бета", ["B1"])

    assert await db.rename_program_by_id(beta, "Альфа") is False

    programs = {p["name"]: p["day_count"] for p in await db.list_programs(user_id)}
    assert programs == {"Альфа": 2, "Бета": 1}
    assert alpha != beta


async def test_merging_is_still_possible_but_only_on_purpose(fresh_db, user_id):
    db = fresh_db
    alpha = await _program_with_days(fresh_db, user_id, "Альфа", ["A1", "A2"])
    beta = await _program_with_days(fresh_db, user_id, "Бета", ["B1"])

    await db.merge_programs(user_id, source_id=beta, target_id=alpha)

    programs = {p["name"]: p["day_count"] for p in await db.list_programs(user_id)}
    assert programs == {"Альфа": 3}
    assert [d["name"] for d in await db.list_program_days_by_id(alpha)] == ["A1", "A2", "B1"]


async def test_merge_refuses_to_touch_another_users_program(fresh_db, user_id):
    db = fresh_db
    await db.get_or_create_user(telegram_id=222, username="other")
    mine = await _program_with_days(fresh_db, user_id, "Моя", ["D1"])
    theirs = await _program_with_days(fresh_db, 222, "Чужая", ["D1"])

    await db.merge_programs(user_id, source_id=theirs, target_id=mine)

    assert len(await db.list_program_days_by_id(mine)) == 1
    assert len(await db.list_program_days_by_id(theirs)) == 1


async def test_unique_program_name_prefers_the_suffix_then_counts(fresh_db, user_id):
    db = fresh_db
    assert await db.unique_program_name(user_id, "PPL") == "PPL"
    await db.create_program(user_id, "PPL")
    assert await db.unique_program_name(user_id, "PPL", suffix="от @vasya") == "PPL (от @vasya)"
    assert await db.unique_program_name(user_id, "PPL") == "PPL (2)"
    await db.create_program(user_id, "PPL (2)")
    assert await db.unique_program_name(user_id, "PPL") == "PPL (3)"


async def test_unique_program_name_never_exceeds_the_rename_limit(fresh_db, user_id):
    """Имя ровно в лимит получало « (2)» сверху — 52 символа, которые ручное
    переименование уже не принимает: копию нельзя было даже переназвать."""
    db = fresh_db
    base = "П" * config.MAX_PROGRAM_NAME_LENGTH
    await db.create_program(user_id, base)

    copy_name = await db.unique_program_name(user_id, base)
    assert len(copy_name) <= config.MAX_PROGRAM_NAME_LENGTH
    assert copy_name.endswith("(2)")

    # Суффиксный вариант («от @vasya») обязан влезать так же.
    suffixed = await db.unique_program_name(user_id, base, suffix="от @vasya")
    assert len(suffixed) <= config.MAX_PROGRAM_NAME_LENGTH
    assert suffixed.endswith("(от @vasya)")

    # И следующая копия не совпадает с уже занятой усечённой.
    await db.create_program(user_id, copy_name)
    third = await db.unique_program_name(user_id, base)
    assert len(third) <= config.MAX_PROGRAM_NAME_LENGTH
    assert third != copy_name


# ---------- порядок дней ----------


async def test_days_come_back_in_the_order_they_were_added(fresh_db, user_id):
    program_id = await _program_with_days(fresh_db, user_id, "PPL", ["Толкай", "Тяни", "Ноги"])
    days = await fresh_db.list_program_days_by_id(program_id)
    assert [d["name"] for d in days] == ["Толкай", "Тяни", "Ноги"]
    assert [d["day_order"] for d in days] == [0, 1, 2]


async def test_days_can_be_reordered(fresh_db, user_id):
    """Порядком дней был порядок id, поэтому переставить их было нельзя вовсе."""
    db = fresh_db
    program_id = await _program_with_days(fresh_db, user_id, "PPL", ["Толкай", "Тяни", "Ноги"])
    days = await db.list_program_days_by_id(program_id)

    await db.reorder_program_day(days[2]["id"], "up")

    assert [d["name"] for d in await db.list_program_days_by_id(program_id)] == [
        "Толкай", "Ноги", "Тяни",
    ]


async def test_reordering_past_either_end_is_a_no_op(fresh_db, user_id):
    db = fresh_db
    program_id = await _program_with_days(fresh_db, user_id, "PPL", ["A", "B"])
    days = await db.list_program_days_by_id(program_id)

    await db.reorder_program_day(days[0]["id"], "up")
    await db.reorder_program_day(days[1]["id"], "down")

    assert [d["name"] for d in await db.list_program_days_by_id(program_id)] == ["A", "B"]


async def test_a_day_can_move_between_programs_and_out_of_one(fresh_db, user_id):
    """«Добавить день в программу» и «вынести день наружу» — обе операции были
    невозможны, пока программой была общая строка."""
    db = fresh_db
    ppl = await _program_with_days(fresh_db, user_id, "PPL", ["Толкай"])
    upper = await _program_with_days(fresh_db, user_id, "Верх/низ", ["Верх"])
    loose = await db.create_routine(user_id, "Руки")

    await db.move_routine_to_program(loose, ppl)
    assert [d["name"] for d in await db.list_program_days_by_id(ppl)] == ["Толкай", "Руки"]

    await db.move_routine_to_program(loose, upper)
    assert [d["name"] for d in await db.list_program_days_by_id(upper)] == ["Верх", "Руки"]

    await db.move_routine_to_program(loose, None)
    assert [r["name"] for r in await db.list_standalone_routines(user_id)] == ["Руки"]


# ---------- удаление ----------


async def test_deleting_the_last_day_takes_the_program_with_it(fresh_db, user_id):
    db = fresh_db
    program_id = await _program_with_days(fresh_db, user_id, "PPL", ["Толкай", "Тяни"])
    days = await db.list_program_days_by_id(program_id)

    await db.delete_routine(days[0]["id"])
    assert len(await db.list_programs(user_id)) == 1

    await db.delete_routine(days[1]["id"])
    assert await db.list_programs(user_id) == []
    assert await db.get_program(program_id) is None


async def test_deleting_a_program_takes_its_days_and_their_exercises(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    program_id = await _program_with_days(fresh_db, user_id, "PPL", ["Толкай"])
    day = (await db.list_program_days_by_id(program_id))[0]
    await db.add_routine_exercise(day["id"], ex_id, 0, "4×8")

    await db.delete_program_by_id(program_id)

    assert await db.get_program(program_id) is None
    assert await db.get_routine(day["id"]) is None
    assert await db.list_routine_exercises(day["id"]) == []


# ---------- «какой сегодня день» ----------


async def _train_day(db, user_id, routine_id, started_at):
    """Полноценная сессия дня: начата и завершена — именно завершение делает
    тап «▶️» тренировкой в истории программы."""
    workout_id = await db.create_workout(user_id, started_at=started_at, routine_id=routine_id)
    await db.finish_workout(workout_id, finished_at=started_at)
    return workout_id


async def test_next_day_is_the_first_one_for_a_program_never_trained(fresh_db, user_id):
    program_id = await _program_with_days(fresh_db, user_id, "PPL", ["Толкай", "Тяни", "Ноги"])
    nxt = await fresh_db.next_program_day(program_id)
    assert nxt["name"] == "Толкай"


async def test_next_day_follows_the_last_one_actually_trained(fresh_db, user_id):
    db = fresh_db
    program_id = await _program_with_days(fresh_db, user_id, "PPL", ["Толкай", "Тяни", "Ноги"])
    days = await db.list_program_days_by_id(program_id)

    await _train_day(db, user_id, days[0]["id"], "2026-08-01T10:00:00")
    assert (await db.next_program_day(program_id))["name"] == "Тяни"

    await _train_day(db, user_id, days[1]["id"], "2026-08-03T10:00:00")
    assert (await db.next_program_day(program_id))["name"] == "Ноги"


async def test_next_day_wraps_round_at_the_end_of_the_program(fresh_db, user_id):
    db = fresh_db
    program_id = await _program_with_days(fresh_db, user_id, "PPL", ["Толкай", "Тяни"])
    days = await db.list_program_days_by_id(program_id)
    await _train_day(db, user_id, days[1]["id"], "2026-08-03T10:00:00")
    assert (await db.next_program_day(program_id))["name"] == "Толкай"


async def test_a_session_logged_out_of_order_just_moves_the_suggestion(fresh_db, user_id):
    """Указатель считается из истории, а не хранится — поэтому «залипнуть» ему
    негде, и день, сделанный не по очереди, чинится следующей же тренировкой."""
    db = fresh_db
    program_id = await _program_with_days(fresh_db, user_id, "PPL", ["Толкай", "Тяни", "Ноги"])
    days = await db.list_program_days_by_id(program_id)
    await _train_day(db, user_id, days[0]["id"], "2026-08-01T10:00:00")
    await _train_day(db, user_id, days[2]["id"], "2026-08-02T10:00:00")
    assert (await db.next_program_day(program_id))["name"] == "Толкай"


async def test_day_history_counts_every_session_per_day(fresh_db, user_id):
    db = fresh_db
    program_id = await _program_with_days(fresh_db, user_id, "PPL", ["Толкай", "Ноги"])
    days = await db.list_program_days_by_id(program_id)
    await _train_day(db, user_id, days[0]["id"], "2026-07-01T10:00:00")
    await _train_day(db, user_id, days[0]["id"], "2026-07-08T10:00:00")
    await _train_day(db, user_id, days[1]["id"], "2026-06-20T10:00:00")

    history = await db.program_day_history(program_id)

    assert history[days[0]["id"]] == ("2026-07-08T10:00:00", 2)
    assert history[days[1]["id"]] == ("2026-06-20T10:00:00", 1)


async def test_a_started_but_unfinished_workout_is_not_a_session_of_the_day(fresh_db, user_id):
    """Тап «▶️ День 1», брошенный в раздевалке, — не тренировка: он не должен
    ни попадать в историю дня, ни сдвигать «дальше по кругу» на День 2 —
    иначе несделанный день пропускается насовсем."""
    db = fresh_db
    program_id = await _program_with_days(fresh_db, user_id, "PPL", ["Толкай", "Тяни"])
    days = await db.list_program_days_by_id(program_id)

    # Активная (начатая и не завершённая) тренировка по Дню 1.
    await db.create_workout(user_id, started_at="2026-08-01T10:00:00", routine_id=days[0]["id"])

    assert await db.program_day_history(program_id) == {}
    assert (await db.next_program_day(program_id))["name"] == "Толкай"

    # Завершённая сессия Дня 1 против недоделанного захода на День 2 позже:
    # указатель слушает только завершённые.
    await _train_day(db, user_id, days[0]["id"], "2026-08-02T10:00:00")
    await db.create_workout(user_id, started_at="2026-08-03T10:00:00", routine_id=days[1]["id"])

    history = await db.program_day_history(program_id)
    assert set(history) == {days[0]["id"]}
    assert (await db.next_program_day(program_id))["name"] == "Тяни"


async def test_programs_are_listed_by_when_they_were_last_trained(fresh_db, user_id):
    db = fresh_db
    old = await _program_with_days(fresh_db, user_id, "Прошлогодний", ["A"])
    current = await _program_with_days(fresh_db, user_id, "Текущий", ["B"])
    old_day = (await db.list_program_days_by_id(old))[0]
    current_day = (await db.list_program_days_by_id(current))[0]
    await db.create_workout(user_id, started_at="2026-01-05T10:00:00", routine_id=old_day["id"])
    await db.create_workout(user_id, started_at="2026-08-01T10:00:00", routine_id=current_day["id"])

    assert [p["name"] for p in await db.list_programs(user_id)] == ["Текущий", "Прошлогодний"]


# ---------- бюджет дней ----------


async def test_routine_budget_is_one_rule_for_every_creation_path(fresh_db, user_id):
    db = fresh_db
    assert await db.routine_budget(user_id, 5) is None
    for i in range(config.MAX_ROUTINES_PER_USER):
        await db.create_routine(user_id, f"День {i}")
    assert await db.routine_budget(user_id, 1) is not None


async def test_replacing_days_does_not_count_them_twice(fresh_db, user_id):
    """Правка программы того же размера упиралась бы в потолок только потому,
    что старая версия ещё цела."""
    db = fresh_db
    for i in range(config.MAX_ROUTINES_PER_USER):
        await db.create_routine(user_id, f"День {i}")
    assert await db.routine_budget(user_id, adding=3, freeing=3) is None
    assert await db.routine_budget(user_id, adding=4, freeing=3) is not None


# ---------- схема из тренировки ----------


async def test_a_program_saved_from_a_workout_carries_what_was_actually_done(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", group_id)
    curl = await db.create_exercise(user_id, "Подъём на бицепс", group_id)
    workout_id = await db.create_workout(user_id)
    block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block, bench, 0)
    for i, reps in enumerate((8, 8, 7)):
        await db.add_set(block, bench, i + 1, 0, 80, reps)
    block2 = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block2, curl, 0)
    await db.add_set(block2, curl, 1, 0, 20, 12)
    await db.add_set(block2, curl, 2, 0, 20, 12)

    routine_id = await db.create_routine_from_workout(user_id, workout_id, "Толкай")

    targets = {ex["display_name"]: ex["target"] for ex in await db.list_routine_exercises(routine_id)}
    assert targets == {"Жим лёжа": "3×7–8", "Подъём на бицепс": "2×12"}


async def test_a_workout_snapshot_can_become_a_day_of_a_program(fresh_db, user_id):
    """Человек, который полгода ходит по своему А/Б, не мог собрать из этого
    программу — «из тренировки» умел делать только одиночные строки."""
    db = fresh_db
    program_id = await db.create_program(user_id, "Мой А/Б", source="workout")
    workout_id = await db.create_workout(user_id)

    await db.create_routine_from_workout(user_id, workout_id, "День A", program_id=program_id)
    await db.create_routine_from_workout(user_id, workout_id, "День B", program_id=program_id)

    assert [d["name"] for d in await db.list_program_days_by_id(program_id)] == ["День A", "День B"]
    assert await db.list_standalone_routines(user_id) == []


# ---------- миграция со старой модели ----------


async def test_migration_gives_every_name_group_a_program_row(fresh_db, user_id):
    """`_migrate_programs_from_names` на базе, где программы ещё были строками."""
    db = fresh_db
    for name, program in [("Толкай", "PPL"), ("Тяни", "PPL"), ("Ноги", "PPL"), ("Всё тело", None)]:
        await db.conn().execute(
            "INSERT INTO routines (user_id, name, created_at, program_name) VALUES (?, ?, ?, ?)",
            (user_id, name, db.now_iso(), program),
        )
    await db.conn().commit()

    await db._migrate_programs_from_names()

    programs = await db.list_programs(user_id)
    assert [(p["name"], p["day_count"]) for p in programs] == [("PPL", 3)]
    assert [d["name"] for d in await db.list_program_days_by_id(programs[0]["id"])] == [
        "Толкай", "Тяни", "Ноги",
    ]
    assert [r["name"] for r in await db.list_standalone_routines(user_id)] == ["Всё тело"]


async def test_migration_keeps_different_users_apart(fresh_db, user_id):
    db = fresh_db
    await db.get_or_create_user(telegram_id=222, username="other")
    for owner in (user_id, 222):
        await db.conn().execute(
            "INSERT INTO routines (user_id, name, created_at, program_name) VALUES (?, ?, ?, 'PPL')",
            (owner, "Толкай", db.now_iso()),
        )
    await db.conn().commit()

    await db._migrate_programs_from_names()

    assert len(await db.list_programs(user_id)) == 1
    assert len(await db.list_programs(222)) == 1


async def test_migration_folds_a_case_only_duplicate_rather_than_losing_it(fresh_db, user_id):
    """Старая группировка регистр учитывала, новый индекс — нет. Слить дни в
    одну программу можно, потерять их — нет."""
    db = fresh_db
    for program in ("Сплит", "сплит"):
        await db.conn().execute(
            "INSERT INTO routines (user_id, name, created_at, program_name) VALUES (?, ?, ?, ?)",
            (user_id, f"День {program}", db.now_iso(), program),
        )
    await db.conn().commit()

    await db._migrate_programs_from_names()

    programs = await db.list_programs(user_id)
    assert len(programs) == 1
    assert len(await db.list_program_days_by_id(programs[0]["id"])) == 2


async def test_migration_is_a_no_op_the_second_time(fresh_db, user_id):
    db = fresh_db
    await db.conn().execute(
        "INSERT INTO routines (user_id, name, created_at, program_name) VALUES (?, ?, ?, 'PPL')",
        (user_id, "Толкай", db.now_iso()),
    )
    await db.conn().commit()

    await db._migrate_programs_from_names()
    await db._migrate_programs_from_names()

    assert len(await db.list_programs(user_id)) == 1


# ---------- шаринг: счётчик и отзыв ----------


async def test_shared_item_counts_how_many_times_it_was_taken(fresh_db, user_id):
    db = fresh_db
    token = await db.create_shared_item(user_id, "routine", "{}")
    assert (await db.get_shared_item(token))["taken_count"] == 0
    await db.mark_shared_item_taken(token)
    await db.mark_shared_item_taken(token)
    assert (await db.get_shared_item(token))["taken_count"] == 2


async def test_only_the_owner_can_revoke_a_share(fresh_db, user_id):
    db = fresh_db
    token = await db.create_shared_item(user_id, "routine", "{}")
    assert await db.delete_shared_item(token, owner_id=999) is False
    assert await db.get_shared_item(token) is not None
    assert await db.delete_shared_item(token, owner_id=user_id) is True
    assert await db.get_shared_item(token) is None


async def test_two_days_added_at_once_get_different_positions(fresh_db, user_id):
    """Два быстрых тапа «➕ Добавить день» — и оба дня получали day_order = 0.

    Порядок читался отдельным SELECT до вставки, а aiogram обрабатывает апдейты
    конкурентно: второй хендлер успевал прочитать тот же MAX, пока первый ещё не
    вставил строку. Уникального индекса на (program_id, day_order) нет, так что
    коллизия проходила молча, а «поднять день» после неё переставлял не тот день —
    сортировка становилась неоднозначной.
    """
    db = fresh_db
    program_id = await db.create_program(user_id, "PPL")

    await asyncio.gather(
        db.create_routine(user_id, "Толкай", program_id=program_id),
        db.create_routine(user_id, "Тяни", program_id=program_id),
        db.create_routine(user_id, "Ноги", program_id=program_id),
    )

    days = await db.list_program_days_by_id(program_id)
    orders = sorted(day["day_order"] for day in days)
    assert orders == [0, 1, 2]


async def test_moving_days_in_concurrently_does_not_collide(fresh_db, user_id):
    """Тот же race на «вынести день в программу» — там порядок тоже читался до
    записи."""
    db = fresh_db
    program_id = await db.create_program(user_id, "PPL")
    await db.create_routine(user_id, "Толкай", program_id=program_id)
    loose = [await db.create_routine(user_id, name) for name in ("Тяни", "Ноги")]

    await asyncio.gather(*(db.move_routine_to_program(rid, program_id) for rid in loose))

    days = await db.list_program_days_by_id(program_id)
    assert sorted(day["day_order"] for day in days) == [0, 1, 2]


async def test_merging_appends_days_without_reusing_positions(fresh_db, user_id):
    db = fresh_db
    keep = await db.create_program(user_id, "Основная")
    drop = await db.create_program(user_id, "Вторая")
    for name in ("Толкай", "Тяни"):
        await db.create_routine(user_id, name, program_id=keep)
    for name in ("Верх", "Низ"):
        await db.create_routine(user_id, name, program_id=drop)

    await db.merge_programs(user_id, drop, keep)

    days = await db.list_program_days_by_id(keep)
    assert [day["name"] for day in days] == ["Толкай", "Тяни", "Верх", "Низ"]
    assert [day["day_order"] for day in days] == [0, 1, 2, 3]
