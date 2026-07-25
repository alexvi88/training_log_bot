"""Switching kg/lb rescales the whole set/bodyweight history, and a round trip
loses precision to rounding — so it needs a yes/no confirmation, not a single
accidental tap (settings.py:settings_unit*)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import SettingsFlow
from handlers import settings

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.text = "🔧 Настройки:"
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=MemoryStorage(), key=key)
    await state.set_state(SettingsFlow.menu)
    return state


async def test_unit_tap_asks_before_converting(fresh_db, user_id):
    db = fresh_db
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "settings:unit")

    await settings.settings_unit_confirm(callback, state)

    user = await db.get_user(user_id)
    assert user["unit"] == "kg"  # nothing converted yet
    text = callback.message.answer.await_args.args[0]
    assert "lb" in text
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "settings:unityes" in callback_datas
    assert "settings:unitno" in callback_datas


async def test_unit_yes_converts_history(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 0, 0, 100.0, 5, None)

    state = await _make_state(user_id)
    callback = _make_callback(user_id, "settings:unityes")

    await settings.settings_unit(callback, state)

    user = await db.get_user(user_id)
    assert user["unit"] == "lb"
    sets = await db.list_sets_for_block(block_id)
    assert sets[0]["weight"] > 200  # 100kg converted to lb


async def test_unit_no_cancels_without_converting(fresh_db, user_id):
    db = fresh_db
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "settings:unitno")

    await settings.settings_unit_cancel(callback, state)

    user = await db.get_user(user_id)
    assert user["unit"] == "kg"
