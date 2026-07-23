"""'🔁 Повторить прошлую' — start a new workout pre-loaded with the last one's plan."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import keyboards
from fsm import WorkoutFlow
from handlers import workout


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    ids = iter(range(600, 700))

    async def _answer(*args, **kwargs):
        return SimpleNamespace(message_id=next(ids), chat=SimpleNamespace(id=user_id))

    message.answer = AsyncMock(side_effect=_answer)
    message.delete = AsyncMock()
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(side_effect=_answer)
    message.bot = bot
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


async def _finished_workout(db, user_id, exercise_ids):
    wid = await db.create_workout(user_id)
    for ex_id in exercise_ids:
        block_id = await db.create_block(wid, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.add_set(block_id, ex_id, 0, 0, 100.0, 8, None)
    await db.finish_workout(wid, None)
    return wid


@pytest.mark.asyncio
async def test_repeat_last_loads_previous_plan(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", group_id)
    row = await db.create_exercise(user_id, "Тяга", group_id)
    await _finished_workout(db, user_id, [bench, row])

    state = await _state(user_id)
    callback = _make_callback(user_id, "menu:repeat_last")

    await workout.repeat_last_workout(callback, state)

    data = await state.get_data()
    # First block opened for logging, the rest queued as planned_blocks.
    assert await state.get_state() == WorkoutFlow.logging_set
    assert data["open_exercises"] == [bench]
    assert data["planned_blocks"] == [{"exercise_ids": [row]}]


@pytest.mark.asyncio
async def test_repeat_last_without_history_is_gentle(fresh_db, user_id):
    state = await _state(user_id)
    callback = _make_callback(user_id, "menu:repeat_last")

    await workout.repeat_last_workout(callback, state)

    callback.answer.assert_any_await("Нет прошлой тренировки для повтора")


def test_menu_shows_repeat_button_only_when_available():
    with_btn = keyboards.main_menu(has_active_workout=False, can_repeat_last=True)
    cbs = [b.callback_data for row in with_btn.inline_keyboard for b in row]
    assert "menu:repeat_last" in cbs

    without = keyboards.main_menu(has_active_workout=False, can_repeat_last=False)
    cbs2 = [b.callback_data for row in without.inline_keyboard for b in row]
    assert "menu:repeat_last" not in cbs2

    # Never offered while a workout is already active.
    active = keyboards.main_menu(has_active_workout=True, can_repeat_last=True)
    cbs3 = [b.callback_data for row in active.inline_keyboard for b in row]
    assert "menu:repeat_last" not in cbs3
