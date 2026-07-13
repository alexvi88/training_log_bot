"""handlers.ai_trainer: keyboard helper that reacts to an active workout, and the
'К тренировке' button that resumes it without wiping the AI chat history.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from handlers import ai_trainer

pytestmark = pytest.mark.asyncio


def _callbacks(kb) -> list[str]:
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1))
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=2))
    bot.send_chat_action = AsyncMock()
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


async def test_ai_keyboard_shows_menu_without_active_workout(fresh_db, user_id):
    kb = await ai_trainer.ai_keyboard(user_id)
    assert "ai:menu" in _callbacks(kb)


async def test_ai_keyboard_shows_resume_workout_with_active_workout(fresh_db, user_id):
    await fresh_db.create_workout(user_id, started_at="2026-07-13T10:00:00", status="active")
    kb = await ai_trainer.ai_keyboard(user_id)
    callbacks = _callbacks(kb)
    assert "ai:resume_workout" in callbacks
    assert "ai:menu" in callbacks


async def test_ai_resume_workout_does_not_delete_ai_chat_message(fresh_db, user_id):
    """The AI chat message the button is on must stay in the chat, unlike menu:resume_workout."""
    await fresh_db.create_workout(user_id, started_at="2026-07-13T10:00:00", status="active")
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "ai:resume_workout")

    await ai_trainer.ai_resume_workout(callback, state)

    callback.message.delete.assert_not_awaited()
    callback.answer.assert_awaited()


async def test_ai_resume_workout_alerts_when_no_active_workout(fresh_db, user_id):
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "ai:resume_workout")

    await ai_trainer.ai_resume_workout(callback, state)

    callback.message.delete.assert_not_awaited()
    callback.answer.assert_awaited_once_with("Нет активной тренировки", show_alert=True)
