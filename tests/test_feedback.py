"""/feedback — free-form feedback (text, photos, whatever) relayed to the admin."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

import config
from fsm import FeedbackFlow
from handlers import feedback

pytestmark = pytest.mark.asyncio


def _make_message(user_id: int, username: str | None = "tester"):
    message = MagicMock(spec=Message)
    message.from_user = SimpleNamespace(id=user_id, username=username)
    message.bot = AsyncMock()
    message.answer = AsyncMock()
    message.reply = AsyncMock()
    message.copy_to = AsyncMock()
    return message


async def _make_state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


async def test_feedback_command_prompts_and_sets_state(user_id):
    message = _make_message(user_id)
    state = await _make_state(user_id)

    await feedback.cmd_feedback(message, state)

    assert await state.get_state() == FeedbackFlow.awaiting_message.state
    message.answer.assert_awaited_once()


async def test_feedback_message_forwarded_to_admin(user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 999)
    message = _make_message(user_id, username="alex")
    message.text = "Всё сломалось!"
    state = await _make_state(user_id)
    await state.set_state(FeedbackFlow.awaiting_message)

    await feedback.feedback_message(message, state)

    message.bot.send_message.assert_awaited_once()
    args = message.bot.send_message.await_args.args
    assert args[0] == 999
    assert "@alex" in args[1]
    message.copy_to.assert_awaited_once_with(999)
    message.reply.assert_awaited_once()


async def test_feedback_message_without_admin_configured(user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    message = _make_message(user_id)
    state = await _make_state(user_id)
    await state.set_state(FeedbackFlow.awaiting_message)

    await feedback.feedback_message(message, state)

    message.copy_to.assert_not_awaited()
    message.reply.assert_awaited_once()


async def test_feedback_done_clears_state_and_returns_to_menu(user_id, monkeypatch):
    from handlers import workout

    show_main_menu = AsyncMock()
    monkeypatch.setattr(workout, "_show_main_menu", show_main_menu)

    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.answer = AsyncMock()
    state = await _make_state(user_id)
    await state.set_state(FeedbackFlow.awaiting_message)

    await feedback.feedback_done(callback, state)

    assert await state.get_state() is None
    show_main_menu.assert_awaited_once()
