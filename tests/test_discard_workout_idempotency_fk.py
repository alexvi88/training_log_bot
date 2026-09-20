"""db.discard_workout must not trip the `set_write_attempts` → `sets` foreign
key: a set logged with an idempotency key (any set the iOS app writes over
HTTP) left a row in `set_write_attempts` that the old delete order never
cleaned up before deleting `sets`, so `sqlite3.IntegrityError: FOREIGN KEY
constraint failed` broke deleting a workout — in the bot too, since discard_
workout is shared code, not an app-only path.
"""
import pytest

pytestmark = pytest.mark.asyncio


async def test_discard_workout_with_idempotent_set_does_not_raise(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Тяга", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    # Ровно то, что оставляет HTTP-запись подхода из приложения: строка в
    # set_write_attempts, ссылающаяся на только что вставленный sets.id.
    await db.append_set(
        block_id, ex_id, 0, 60.0, 10, user_id=user_id, idempotency_key="test-key-1"
    )

    await db.discard_workout(workout_id)

    assert await db.get_workout(workout_id) is None


async def test_discard_workout_removes_the_set_write_attempt_row(fresh_db, user_id):
    """Not just "doesn't crash" — the orphaned idempotency row must actually
    be gone, or a later idempotency check on a stale set_id would misbehave."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Тяга", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.append_set(
        block_id, ex_id, 0, 60.0, 10, user_id=user_id, idempotency_key="test-key-2"
    )

    await db.discard_workout(workout_id)

    cur = await db.conn().execute(
        "SELECT COUNT(*) FROM set_write_attempts WHERE user_id = ? AND idempotency_key = ?",
        (user_id, "test-key-2"),
    )
    (count,) = await cur.fetchone()
    assert count == 0
