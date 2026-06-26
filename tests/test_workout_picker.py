from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import WorkoutFlow
from handlers import workout

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str):
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=999))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id)
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int, **extra_data) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.picking_exercise)
    await state.update_data(
        workout_id=1, live_chat_id=user_id, live_message_id=1, pending_group_id=None,
        **extra_data,
    )
    return state


async def test_pick_page_advances_to_second_page_and_keeps_remainder(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    for i in range(10):
        await db.create_exercise(user_id, f"Exercise {i:02d}", group_id)

    state = await _make_state(user_id, pick_page=0)
    callback = _make_callback(user_id, "pick:page:1")

    await workout.pick_page(callback, state)

    data = await state.get_data()
    assert data["pick_page"] == 1

    # Second page should contain the remaining 2 exercises, sent as the new live message.
    sent_text = callback.bot.send_message.await_args.kwargs["text"]
    assert sent_text.count("Exercise") == 2


async def test_pick_page_first_page_has_no_back_button(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    for i in range(10):
        await db.create_exercise(user_id, f"Exercise {i:02d}", group_id)

    state = await _make_state(user_id, pick_page=1)
    callback = _make_callback(user_id, "pick:page:0")

    await workout.pick_page(callback, state)

    kb = callback.bot.send_message.await_args.kwargs["reply_markup"]
    callback_datas = [
        button.callback_data for row in kb.inline_keyboard for button in row
    ]
    assert "pick:page:-1" not in callback_datas
    assert "pick:page:1" in callback_datas  # next-page button still present
