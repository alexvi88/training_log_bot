"""wipe_user_account убирает пользователя целиком — данные и саму строку users,
так что следующий /start видит его новичком."""

import db


async def test_wipe_removes_account_and_all_scoped_data(fresh_db, user_id):
    ex_id = await fresh_db.create_exercise(user_id, "Жим лёжа", 1)
    workout_id = await fresh_db.create_finished_workout(
        user_id, "2026-08-01T10:00:00", "2026-08-01T10:30:00"
    )
    block_id = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_set(block_id, ex_id, 1, 0, 100.0, 5)
    await fresh_db.add_bodyweight_log(user_id, 82.5)
    await fresh_db.add_food_entry(user_id, "2026-08-01", "омлет", calories=300)

    assert await fresh_db.get_user(user_id) is not None

    await fresh_db.wipe_user_account(user_id)

    assert await fresh_db.get_user(user_id) is None
    assert await fresh_db.list_workouts(user_id, status="finished") == []
    assert await fresh_db.list_bodyweight_logs(user_id) == []
    assert await fresh_db.list_food_entries(user_id, "2026-08-01") == []

    conn = db.conn()
    for table, column in (
        ("sets", None),
        ("workout_blocks", "workout_id"),
        ("workouts", "user_id"),
        ("exercises", "user_id"),
        ("bodyweight_logs", "telegram_id"),
        ("food_entries", "telegram_id"),
    ):
        if column is None:
            continue
        cur = await conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (user_id,))
        row = await cur.fetchone()
        assert row[0] == 0, f"{table} still has rows for the wiped user"


async def test_wipe_leaves_no_row_anywhere_in_the_schema(fresh_db, user_id):
    """Ни одной строки атлета ни в одной таблице базы — включая те, которых на
    момент этого теста ещё нет: список берётся из схемы, а не из памяти автора.

    Программа с днями стоит тут отдельным пунктом: «снёс историю, а программа
    осталась» — ровно то, за чем этот тест поставлен.
    """
    program_id = await fresh_db.create_program(user_id, "Масса 4× верх/низ")
    for day in ("Верх А", "Низ А", "Верх Б", "Низ Б"):
        await fresh_db.create_routine(user_id, day, program_id=program_id)
    ex_id = await fresh_db.create_exercise(user_id, "Жим лёжа", 1)
    workout_id = await fresh_db.create_finished_workout(
        user_id, "2026-08-01T10:00:00", "2026-08-01T10:30:00"
    )
    block_id = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_set(block_id, ex_id, 1, 0, 100.0, 5)
    await fresh_db.add_bodyweight_log(user_id, 82.5)
    await fresh_db.add_food_entry(user_id, "2026-08-01", "омлет", calories=300)
    await fresh_db.record_donation(user_id, "charge-1", 100)
    await fresh_db.log_cost_event(user_id, "question", model="grok", prompt_tokens=10)
    await fresh_db.issue_mcp_token(user_id)
    await fresh_db.award_achievements(user_id, {"first_workout"})

    await fresh_db.wipe_user_account(user_id)

    assert await fresh_db.list_programs(user_id) == []
    assert await fresh_db.list_routines(user_id) == []
    assert await fresh_db.get_user(user_id) is None

    conn = db.conn()
    for table, column in await db._user_scoped_tables():
        cur = await conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (user_id,))
        (left,) = await cur.fetchone()
        assert left == 0, f"{table} still has rows for the wiped user"
    # Дети без своей колонки владельца: остаться им не от кого — родителей нет.
    for table in ("sets", "block_exercises", "workout_blocks", "exercise_notes",
                  "routine_exercises"):
        cur = await conn.execute(f"SELECT COUNT(*) FROM {table}")
        (left,) = await cur.fetchone()
        assert left == 0, f"{table} still has orphan rows after the wipe"


async def test_every_user_scoped_table_is_covered(fresh_db, user_id):
    """Каждая таблица с хозяином попадает под снос — проверка самого списка.

    Без неё «сносим по схеме» проверялось бы только теми таблицами, которые
    тест успел заполнить, а пустая таблица молча сходит за вычищенную.
    """
    scoped = dict(await db._user_scoped_tables())
    assert scoped["programs"] == "user_id"
    assert scoped["routines"] == "user_id"
    assert scoped["donations"] == "user_id"
    assert scoped["ai_chat_messages"] == "telegram_id"
    assert scoped["shared_items"] == "owner_id"
    # users уходит последней: на её отсутствие смотрит «первый ли это /start».
    assert list(await db._user_scoped_tables())[-1] == ("users", "telegram_id")


async def test_wipe_does_not_touch_other_users(fresh_db, user_id):
    other = (await fresh_db.get_or_create_user(telegram_id=222, username="other"))["telegram_id"]
    await fresh_db.create_exercise(other, "Присед", 1)

    await fresh_db.wipe_user_account(user_id)

    assert await fresh_db.get_user(other) is not None
    assert len(await fresh_db.list_user_exercises(other)) == 1
