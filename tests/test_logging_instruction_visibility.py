"""_render_logging_screen hides the "weight and reps, separated by a space"
instruction once the athlete has trained often enough recently to know the
format (analytics.is_seasoned) — this wires that decision to the real
finished-workout history in the DB, complementing the pure _logging_hint
tests in test_formatting.py."""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import analytics
from fsm import WorkoutFlow
from handlers import workout

_INSTRUCTION = "Вес и повторы через пробел"


def _make_bot():
    bot = MagicMock()
    bot.delete_message = AsyncMock()

    async def _send(*args, **kwargs):
        text = kwargs.get("text", "")
        _make_bot.sent_text = text
        return SimpleNamespace(message_id=900, chat=SimpleNamespace(id=1))

    bot.send_message = AsyncMock(side_effect=_send)
    return bot


async def _setup(db, user_id: int, n_recent_workouts: int, days_ago_step: int = 1):
    """Finished workouts spaced `days_ago_step` days apart, most recent = today,
    for exercises unrelated to the one being logged (so they don't add sets to
    the exercise under test, only to the "has this person trained lately" count)."""
    today = dt.date.today()
    other_group = await db.create_muscle_group(user_id, "Ноги")
    other_ex = await db.create_exercise(user_id, "Присед", other_group)
    for i in range(n_recent_workouts):
        day = today - dt.timedelta(days=i * days_ago_step)
        wid = await db.create_workout(user_id, started_at=f"{day.isoformat()}T12:00:00")
        block_id = await db.create_block(wid, "single")
        await db.add_block_exercise(block_id, other_ex, 0)
        await db.add_set(block_id, other_ex, 0, 0, 100.0, 5, None)
        await db.finish_workout(wid, finished_at=f"{day.isoformat()}T12:30:00")

    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)

    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        workout_id=workout_id, live_chat_id=1, live_message_id=1,
        open_exercises=[ex_id], open_blocks={ex_id: block_id}, active_exercise_id=ex_id,
        last_by_exercise={}, last_session_sets={},
    )
    return state


@pytest.mark.asyncio
async def test_instruction_shown_for_a_fresh_user(fresh_db, user_id):
    db = fresh_db
    state = await _setup(db, user_id, n_recent_workouts=0)
    user = await db.get_user(user_id)
    bot = _make_bot()

    await workout._render_logging_screen(bot, state, user)

    assert _INSTRUCTION in _make_bot.sent_text


@pytest.mark.asyncio
async def test_instruction_hidden_once_seasoned(fresh_db, user_id):
    db = fresh_db
    threshold = analytics.RECENT_TRAINING_THRESHOLD
    state = await _setup(db, user_id, n_recent_workouts=threshold, days_ago_step=2)
    user = await db.get_user(user_id)
    bot = _make_bot()

    await workout._render_logging_screen(bot, state, user)

    assert _INSTRUCTION not in _make_bot.sent_text


@pytest.mark.asyncio
async def test_instruction_still_shown_one_workout_short_of_threshold(fresh_db, user_id):
    db = fresh_db
    state = await _setup(db, user_id, n_recent_workouts=analytics.RECENT_TRAINING_THRESHOLD - 1, days_ago_step=2)
    user = await db.get_user(user_id)
    bot = _make_bot()

    await workout._render_logging_screen(bot, state, user)

    assert _INSTRUCTION in _make_bot.sent_text
