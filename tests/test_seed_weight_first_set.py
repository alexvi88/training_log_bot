"""Bare-reps input ("9" with no weight) on the first set of a new session must
carry forward the FIRST set's weight from last time, not the last (usually
lighter, fatigue-discounted) one — same rule as the "тот же вес" reps row
(see handlers.workout._reps_row_basis). Regression for a report where typing
"9" logged the previous session's last-set weight instead of its first."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import WorkoutFlow
from handlers import workout

pytestmark = pytest.mark.asyncio


def _make_message(user_id: int, text: str, message_id: int = 55):
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=user_id)
    msg.message_id = message_id
    msg.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    msg.text = text
    msg.delete = AsyncMock()
    msg.reply = AsyncMock()
    msg.answer = AsyncMock()
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.set_message_reaction = AsyncMock()

    async def _send(*args, **kwargs):
        return SimpleNamespace(message_id=700, chat=SimpleNamespace(id=user_id))

    bot.send_message = AsyncMock(side_effect=_send)
    msg.bot = bot
    return msg


def _make_callback(user_id: int):
    cb = MagicMock()
    cb.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    cb.answer = AsyncMock()
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.edit_message_text = AsyncMock()

    async def _send(*args, **kwargs):
        return SimpleNamespace(message_id=701, chat=SimpleNamespace(id=user_id))

    bot.send_message = AsyncMock(side_effect=_send)
    cb.bot = bot
    return cb


async def _last_session_id(db, user_id: int) -> int:
    workout_id = await db.create_workout(user_id)
    await db.finish_workout(workout_id, finished_at="2026-08-10T12:00:00")
    return workout_id


async def _state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        open_exercises=[], open_blocks={}, last_by_exercise={}, last_session_sets={},
        weight_steps={}, planned_blocks=[], exercise_targets={},
        live_chat_id=user_id, live_message_id=42,
    )
    return state


async def test_bare_reps_carries_first_set_weight_from_last_session(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Пресс")
    ex_id = await db.create_exercise(user_id, "abs - pull down block", group_id)

    prev_workout_id = await _last_session_id(db, user_id)
    prev_block_id = await db.create_block(prev_workout_id, "single")
    await db.add_block_exercise(prev_block_id, ex_id, 0)
    for weight, reps in [(34.3, 6), (34.3, 5), (32.0, 10)]:
        await db.add_set(prev_block_id, ex_id, 0, 0, weight, reps, None)

    new_workout_id = await db.create_workout(user_id)
    state = await _state(user_id)
    await state.update_data(workout_id=new_workout_id)

    await workout._on_exercise_chosen(_make_callback(user_id), state, ex_id)
    await workout.log_set_text(_make_message(user_id, "9"), state)

    sets = await db.list_sets_for_workout_exercise(new_workout_id, ex_id)
    assert (sets[-1]["weight"], sets[-1]["reps"]) == (34.3, 9)
