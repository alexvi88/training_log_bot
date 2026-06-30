"""A redeploy restarts the process mid-workout: MemoryStorage would wipe FSM
state, and even with JSONFileStorage a naive JSON round-trip stringifies the
int-keyed dicts handlers rely on (open_blocks, last_by_exercise). These tests
drive the real handlers — not just the storage layer — through a simulated
restart to confirm logging a set still works immediately afterwards, both
when the FSM file survives and when it's gone entirely.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import WorkoutFlow
from fsm_storage import JSONFileStorage
from handlers import workout

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str):
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=999))
    message = MagicMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock(
        return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1)
    )
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.bot = bot
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _make_message(user_id: int, text: str):
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=999))
    bot.set_message_reaction = AsyncMock()
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester")
    message.bot = bot
    message.text = text
    message.delete = AsyncMock()
    message.reply = AsyncMock()
    return message


async def test_log_set_survives_restart_with_correct_block_and_weight(fresh_db, user_id, tmp_path):
    """A redeploy where the FSM file on the persistent volume survives: the new
    process loads a brand-new JSONFileStorage from the same path, and logging
    a set (including bare-reps weight inference) must keep working right away.
    """
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)

    fsm_path = str(tmp_path / "fsm.json")
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)

    storage = JSONFileStorage(fsm_path)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        workout_id=workout_id, live_chat_id=user_id, live_message_id=1,
        open_exercises=[ex_id], open_blocks={ex_id: block_id},
        active_exercise_id=ex_id, last_by_exercise={}, last_session_sets={},
    )

    await workout.log_set_text(_make_message(user_id, "100 8"), state)

    sets = await db.list_sets_for_block(block_id)
    assert len(sets) == 1
    assert sets[0]["weight"] == 100.0 and sets[0]["reps"] == 8

    # --- simulate redeploy: a fresh process re-reads the same on-disk FSM file ---
    restarted_state = FSMContext(storage=JSONFileStorage(fsm_path), key=key)

    assert await restarted_state.get_state() == WorkoutFlow.logging_set.state
    data = await restarted_state.get_data()
    assert data["open_blocks"][ex_id] == block_id  # int dict key survived the JSON round-trip

    message2 = _make_message(user_id, "8")  # bare reps: weight must come from restored last_by_exercise
    await workout.log_set_text(message2, restarted_state)

    sets = await db.list_sets_for_block(block_id)
    assert len(sets) == 2
    assert sets[1]["weight"] == 100.0 and sets[1]["reps"] == 8
    message2.delete.assert_awaited()  # handler ran its normal success path, no crash


async def test_resume_workout_rebuilds_state_after_total_fsm_loss(fresh_db, user_id):
    """Worse than a clean redeploy: the FSM file itself is gone (fresh volume,
    corrupted file, etc.) so the new process starts with empty FSM storage.
    Tapping "Продолжить тренировку" must rebuild open exercises/blocks from the
    DB and immediately accept input again.
    """
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Тяга штанги", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 1, 0, 80.0, 10)

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))

    await workout.resume_workout(_make_callback(user_id, "menu:resume_workout"), state)

    assert await state.get_state() == WorkoutFlow.logging_set.state
    data = await state.get_data()
    assert data["open_exercises"] == [ex_id]
    assert data["open_blocks"][ex_id] == block_id
    assert data["active_exercise_id"] == ex_id

    message = _make_message(user_id, "85 8")
    await workout.log_set_text(message, state)

    sets = await db.list_sets_for_block(block_id)
    assert len(sets) == 2
    assert sets[-1]["weight"] == 85.0 and sets[-1]["reps"] == 8
