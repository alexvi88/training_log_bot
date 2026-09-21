"""Same FOREIGN KEY constraint as tests/test_discard_workout_idempotency_fk.py,
but for the four places that delete rows from `sets` directly instead of going
through discard_workout: db.delete_set, db.delete_last_set_in_block,
db.delete_last_set_for_exercise_in_block and db.delete_block_and_sets.

`set_write_attempts.set_id` is a FK on `sets(id)` without `ON DELETE CASCADE`,
and `PRAGMA foreign_keys=ON` is on. A set logged with an idempotency key (any
set the iOS app writes over HTTP) leaves a row in `set_write_attempts` that
none of these four functions used to clean up before deleting the `sets` row
they point at, so deleting that set raised `sqlite3.IntegrityError: FOREIGN
KEY constraint failed` — a live 500 from `DELETE /workouts/{id}/sets/{set_id}`
and from `remove_workout_exercise`, plus the same crash from the bot's "delete
last set" and "remove exercise" flows whenever the set being removed happened
to come from the app.
"""
import pytest

pytestmark = pytest.mark.asyncio


async def _setup_block_with_idempotent_set(db, user_id, key: str):
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Тяга", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    set_id = await db.append_set(
        block_id, ex_id, 0, 60.0, 10, user_id=user_id, idempotency_key=key
    )
    return block_id, ex_id, set_id


async def test_delete_set_with_idempotent_set_does_not_raise(fresh_db, user_id):
    db = fresh_db
    _block_id, _ex_id, set_id = await _setup_block_with_idempotent_set(db, user_id, "dk-1")

    await db.delete_set(set_id)

    assert await db.get_set(set_id) is None
    cur = await db.conn().execute(
        "SELECT COUNT(*) FROM set_write_attempts WHERE set_id = ?", (set_id,)
    )
    (count,) = await cur.fetchone()
    assert count == 0


async def test_delete_last_set_in_block_with_idempotent_set_does_not_raise(fresh_db, user_id):
    db = fresh_db
    block_id, _ex_id, set_id = await _setup_block_with_idempotent_set(db, user_id, "dk-2")

    row = await db.delete_last_set_in_block(block_id)

    assert row is not None and row["id"] == set_id
    assert await db.get_set(set_id) is None
    cur = await db.conn().execute(
        "SELECT COUNT(*) FROM set_write_attempts WHERE set_id = ?", (set_id,)
    )
    (count,) = await cur.fetchone()
    assert count == 0


async def test_delete_last_set_for_exercise_in_block_with_idempotent_set_does_not_raise(
    fresh_db, user_id
):
    db = fresh_db
    block_id, ex_id, set_id = await _setup_block_with_idempotent_set(db, user_id, "dk-3")

    row = await db.delete_last_set_for_exercise_in_block(block_id, ex_id)

    assert row is not None and row["id"] == set_id
    assert await db.get_set(set_id) is None
    cur = await db.conn().execute(
        "SELECT COUNT(*) FROM set_write_attempts WHERE set_id = ?", (set_id,)
    )
    (count,) = await cur.fetchone()
    assert count == 0


async def test_delete_block_and_sets_with_idempotent_set_does_not_raise(fresh_db, user_id):
    db = fresh_db
    block_id, _ex_id, set_id = await _setup_block_with_idempotent_set(db, user_id, "dk-4")

    await db.delete_block_and_sets(block_id)

    assert await db.get_set(set_id) is None
    cur = await db.conn().execute(
        "SELECT COUNT(*) FROM set_write_attempts WHERE set_id = ?", (set_id,)
    )
    (count,) = await cur.fetchone()
    assert count == 0
