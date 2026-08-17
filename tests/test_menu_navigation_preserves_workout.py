"""Navigating to the menu (or history) and back must not drop an in-progress
superset. state.clear() used to wipe open_exercises/active_exercise_id even
though nothing was actually lost — resuming then fell back to _reopen_exercises,
which can only ever recover the single most-recently-touched exercise, silently
dropping the rest of the superset's tabs (and making it look like weights had
vanished, since the earlier exercise's "open" tab disappeared)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import WorkoutFlow
from handlers import workout

pytestmark = pytest.mark.asyncio


def _make_message(user_id: int):
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=999))
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.bot = bot
    message.answer = AsyncMock(
        return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1)
    )
    message.answer_photo = AsyncMock(
        return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1)
    )
    message.delete = AsyncMock()
    return message


def _make_callback(user_id: int, data: str = ""):
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=999))
    message = MagicMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock(
        return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=2)
    )
    message.answer_photo = AsyncMock(
        return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=2)
    )
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.bot = bot
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def test_menu_then_resume_keeps_full_superset_open(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Руки")
    triceps = await db.create_exercise(user_id, "triceps block", group_id)
    preacher = await db.create_exercise(user_id, "preacher curls dumbbells", group_id)

    workout_id = await db.create_workout(user_id)
    triceps_block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(triceps_block, triceps, 0)
    await db.add_set(triceps_block, triceps, 1, 0, 45.0, 7)

    preacher_block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(preacher_block, preacher, 0)
    # preacher curls has no sets logged yet this session — matches the reported repro

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        workout_id=workout_id,
        open_exercises=[triceps, preacher],
        open_blocks={triceps: triceps_block, preacher: preacher_block},
        active_exercise_id=preacher,
        last_by_exercise={triceps: (45.0, 7)},
        last_session_sets={triceps: [], preacher: []},
    )

    # user taps the persistent "Меню" button
    await workout.cmd_start(_make_message(user_id), state)

    data = await state.get_data()
    assert data["open_exercises"] == [triceps, preacher]
    assert data["active_exercise_id"] == preacher
    assert data["open_blocks"] == {triceps: triceps_block, preacher: preacher_block}

    # then resumes the workout
    await workout.resume_workout(_make_callback(user_id, "menu:resume_workout"), state)

    assert await state.get_state() == WorkoutFlow.logging_set.state
    data = await state.get_data()
    assert data["open_exercises"] == [triceps, preacher]
    assert data["active_exercise_id"] == preacher
    assert data["open_blocks"] == {triceps: triceps_block, preacher: preacher_block}


async def test_show_main_menu_then_resume_keeps_full_superset_open(fresh_db, user_id):
    """Same scenario via the inline '🏠 Меню' button (_show_main_menu) instead of
    the persistent keyboard button, e.g. reached from the history screen."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Руки")
    triceps = await db.create_exercise(user_id, "triceps block", group_id)
    preacher = await db.create_exercise(user_id, "preacher curls dumbbells", group_id)

    workout_id = await db.create_workout(user_id)
    triceps_block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(triceps_block, triceps, 0)
    await db.add_set(triceps_block, triceps, 1, 0, 45.0, 7)

    preacher_block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(preacher_block, preacher, 0)

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        workout_id=workout_id,
        open_exercises=[triceps, preacher],
        open_blocks={triceps: triceps_block, preacher: preacher_block},
        active_exercise_id=preacher,
        last_by_exercise={triceps: (45.0, 7)},
        last_session_sets={triceps: [], preacher: []},
    )

    await workout._show_main_menu(_make_callback(user_id, "menu:history"), state)

    data = await state.get_data()
    assert data["open_exercises"] == [triceps, preacher]

    await workout.resume_workout(_make_callback(user_id, "menu:resume_workout"), state)

    data = await state.get_data()
    assert data["open_exercises"] == [triceps, preacher]
    assert data["active_exercise_id"] == preacher
