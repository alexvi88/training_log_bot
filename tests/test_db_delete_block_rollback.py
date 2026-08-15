"""db.delete_block_and_sets — same rollback rule as discard_workout: a
mid-transaction failure must not leave the block half-deleted (some sets
gone, the block row still there or vice versa)."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_delete_block_and_sets_rolls_back_on_failure(fresh_db, user_id, monkeypatch):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    ex_id = await db.create_exercise(user_id, "Присед", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.append_set(block_id, ex_id, 0, 60.0, 10)
    await db.finish_workout(workout_id)

    real_execute = db.conn().execute

    async def _boom(sql, *args, **kwargs):
        if sql.startswith("DELETE FROM workout_blocks"):
            raise RuntimeError("boom")
        return await real_execute(sql, *args, **kwargs)

    monkeypatch.setattr(db.conn(), "execute", _boom)

    with pytest.raises(RuntimeError):
        await db.delete_block_and_sets(block_id)

    monkeypatch.undo()
    # Rolled back: sets are still there, not half-deleted.
    assert len(await db.list_sets_for_block(block_id)) == 1
