"""/broadcast — admin sends a message to every registered user (handlers/admin.py)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import config
from fsm import AdminFlow
from handlers import admin

pytestmark = pytest.mark.asyncio

ADMIN_ID = 999


def _make_message(user_id: int, chat_id: int | None = None, message_id: int = 42):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="admin")
    message.chat = SimpleNamespace(id=chat_id if chat_id is not None else user_id)
    message.message_id = message_id
    message.answer = AsyncMock()
    message.reply = AsyncMock()
    return message


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.edit_text = AsyncMock(return_value=message)
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="admin")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    callback.bot = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


async def test_non_admin_cannot_start_broadcast(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    message = _make_message(111)
    state = await _make_state(111)

    await admin.cmd_broadcast(message, state)

    assert await state.get_state() is None
    message.answer.assert_not_awaited()


async def test_broadcast_command_prompts_and_sets_state(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    message = _make_message(ADMIN_ID)
    state = await _make_state(ADMIN_ID)

    await admin.cmd_broadcast(message, state)

    assert await state.get_state() == AdminFlow.broadcast_awaiting_message.state
    message.answer.assert_awaited_once()


async def test_broadcast_receive_asks_for_confirmation(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    await fresh_db.get_or_create_user(telegram_id=111, username="a")
    await fresh_db.get_or_create_user(telegram_id=222, username="b")
    message = _make_message(ADMIN_ID, message_id=42)
    state = await _make_state(ADMIN_ID)
    await state.set_state(AdminFlow.broadcast_awaiting_message)

    await admin.broadcast_receive(message, state)

    assert await state.get_state() == AdminFlow.broadcast_confirming.state
    data = await state.get_data()
    assert data["broadcast_chat_id"] == ADMIN_ID
    assert data["broadcast_message_id"] == 42
    message.reply.assert_awaited_once()
    assert "2" in message.reply.await_args.args[0]


async def test_broadcast_cancel_clears_state(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    callback = _make_callback(ADMIN_ID, "admin:bc:no")
    state = await _make_state(ADMIN_ID)
    await state.set_state(AdminFlow.broadcast_confirming)

    await admin.broadcast_cancel(callback, state)

    assert await state.get_state() is None
    callback.answer.assert_awaited_once()


async def test_broadcast_send_copies_to_every_user_and_reports_summary(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    monkeypatch.setattr(admin, "BROADCAST_SEND_DELAY", 0)
    await fresh_db.get_or_create_user(telegram_id=111, username="a")
    await fresh_db.get_or_create_user(telegram_id=222, username="b")
    await fresh_db.get_or_create_user(telegram_id=333, username="c")

    callback = _make_callback(ADMIN_ID, "admin:bc:yes")

    async def copy_message(chat_id, from_chat_id, message_id):
        if chat_id == 222:
            raise TelegramForbiddenError(MagicMock(), "bot was blocked by the user")
        return SimpleNamespace(message_id=1)

    callback.bot.copy_message = AsyncMock(side_effect=copy_message)

    state = await _make_state(ADMIN_ID)
    await state.set_state(AdminFlow.broadcast_confirming)
    await state.update_data(broadcast_chat_id=ADMIN_ID, broadcast_message_id=42)

    await admin.broadcast_send(callback, state)

    assert await state.get_state() is None
    assert callback.bot.copy_message.await_count == 3
    summary = callback.message.answer.await_args.args[0]
    assert "2 доставлено" in summary
    assert "1 заблокировали" in summary
    assert "0 ошибок" in summary


async def test_broadcast_send_without_pending_message_shows_alert(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    callback = _make_callback(ADMIN_ID, "admin:bc:yes")
    state = await _make_state(ADMIN_ID)
    await state.set_state(AdminFlow.broadcast_confirming)

    await admin.broadcast_send(callback, state)

    assert await state.get_state() is None
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get("show_alert") is True
