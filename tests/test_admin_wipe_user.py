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


async def test_wipe_does_not_touch_other_users(fresh_db, user_id):
    other = (await fresh_db.get_or_create_user(telegram_id=222, username="other"))["telegram_id"]
    await fresh_db.create_exercise(other, "Присед", 1)

    await fresh_db.wipe_user_account(user_id)

    assert await fresh_db.get_user(other) is not None
    assert len(await fresh_db.list_user_exercises(other)) == 1
