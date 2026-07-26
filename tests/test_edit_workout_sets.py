"""Editing sets on a finished workout must clean up after itself: a block
emptied by deleting its last set shouldn't linger as a "подходов нет" ghost
row forever, and any cached AI-trainer comment must be dropped so it gets
regenerated against the new numbers instead of describing stale ones."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

from fsm import EditWorkoutFlow
from handlers import edit_workout

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str = ""):
    message = MagicMock()
    message.delete = AsyncMock()
    message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _make_message(user_id: int, text: str):
    msg = MagicMock()
    msg.from_user = SimpleNamespace(id=user_id, username="tester")
    msg.text = text
    msg.reply = AsyncMock()
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    msg.delete = AsyncMock()
    return msg


async def _make_state(user_id: int, workout_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=MemoryStorage(), key=key)
    await state.set_state(EditWorkoutFlow.viewing)
    await state.update_data(edit_workout_id=workout_id)
    return state


async def _make_finished_workout(db, user_id: int) -> int:
    return await db.create_finished_workout(
        user_id, started_at="2026-01-05T10:00:00", finished_at="2026-01-05T10:30:00"
    )


async def _add_exercise_block(db, user_id: int, workout_id: int, group_id: int, name: str, sets=()):
    ex_id = await db.create_exercise(user_id, name, group_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    set_ids = []
    for weight, reps in sets:
        round_idx = await db.next_round_index(block_id, ex_id)
        set_ids.append(await db.add_set(block_id, ex_id, round_idx, 0, weight, reps, None))
    return ex_id, block_id, set_ids


async def test_deleting_last_set_drops_the_now_empty_block(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Бицепс")
    workout_id = await _make_finished_workout(db, user_id)
    _, block_id, [set_id] = await _add_exercise_block(
        db, user_id, workout_id, group_id, "Подъём на бицепс", sets=[(20.0, 10)]
    )
    state = await _make_state(user_id, workout_id)

    await edit_workout.editw_delset(_make_callback(user_id, f"editw:delset:{set_id}"), state)

    assert await db.list_blocks_for_workout(workout_id) == []


async def test_deleting_one_of_two_sets_keeps_the_block(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Бицепс")
    workout_id = await _make_finished_workout(db, user_id)
    _, block_id, [set_id, _] = await _add_exercise_block(
        db, user_id, workout_id, group_id, "Подъём на бицепс", sets=[(20.0, 10), (20.0, 8)]
    )
    state = await _make_state(user_id, workout_id)

    await edit_workout.editw_delset(_make_callback(user_id, f"editw:delset:{set_id}"), state)

    blocks = await db.list_blocks_for_workout(workout_id)
    assert len(blocks) == 1
    assert len(await db.list_sets_for_block(blocks[0]["id"])) == 1


async def test_deleting_a_set_clears_cached_ai_comment(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Бицепс")
    workout_id = await _make_finished_workout(db, user_id)
    _, _, [set_id, _] = await _add_exercise_block(
        db, user_id, workout_id, group_id, "Подъём на бицепс", sets=[(20.0, 10), (20.0, 8)]
    )
    await db.set_workout_ai_comment(workout_id, "старый комментарий")
    state = await _make_state(user_id, workout_id)

    await edit_workout.editw_delset(_make_callback(user_id, f"editw:delset:{set_id}"), state)

    workout = await db.get_workout(workout_id)
    assert workout["ai_comment"] is None


async def test_editing_a_set_clears_cached_ai_comment(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Бицепс")
    workout_id = await _make_finished_workout(db, user_id)
    _, _, [set_id] = await _add_exercise_block(
        db, user_id, workout_id, group_id, "Подъём на бицепс", sets=[(20.0, 10)]
    )
    await db.set_workout_ai_comment(workout_id, "старый комментарий")
    state = await _make_state(user_id, workout_id)
    await state.update_data(edit_set_id=set_id)

    await edit_workout.editw_editset_entered(_make_message(user_id, "22.5 8"), state)

    workout = await db.get_workout(workout_id)
    assert workout["ai_comment"] is None
