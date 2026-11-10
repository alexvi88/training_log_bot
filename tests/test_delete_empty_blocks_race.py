"""Чистка пустых блоков не падает, если подход лёг между проверкой и удалением
(живой краш 10.11.2026: удаление подхода и запись строкой в одну секунду)."""

import db


async def _workout_with_block(user_id: int):
    workout_id, _ = await db.get_or_create_active_workout(user_id)
    exercise_id = await db.create_exercise(user_id, "Жим ногами", None)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, exercise_id, 0)
    return workout_id, block_id, exercise_id


async def test_block_that_got_a_set_is_kept(fresh_db, monkeypatch):
    user = await db.create_app_only_user(language_code="ru")
    workout_id, block_id, exercise_id = await _workout_with_block(user["telegram_id"])

    # Чистка решила, что блок пуст, — и тут прилетает подход (как в гонке).
    real_list = db.list_blocks_for_workout

    async def list_then_log(wid):
        blocks = await real_list(wid)
        await db.add_set(block_id, exercise_id, 0, 0, 230, 10)
        return blocks

    async def stale_empty(_block_id):
        return []  # прочитано до того, как подход лёг

    monkeypatch.setattr(db, "list_blocks_for_workout", list_then_log)
    monkeypatch.setattr(db, "list_sets_for_block", stale_empty)
    await db.delete_empty_blocks(workout_id)
    monkeypatch.undo()

    assert [b["id"] for b in await db.list_blocks_for_workout(workout_id)] == [block_id]
    assert len(await db.list_sets_for_block(block_id)) == 1


async def test_really_empty_block_is_removed(fresh_db):
    user = await db.create_app_only_user(language_code="ru")
    workout_id, block_id, _ = await _workout_with_block(user["telegram_id"])
    await db.delete_empty_blocks(workout_id)
    assert await db.list_blocks_for_workout(workout_id) == []
