"""/testpush — админский проверочный пуш на iOS (handlers/admin.py)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import apns
import config
from handlers import admin

pytestmark = pytest.mark.asyncio

ADMIN_ID = 999


def _message(user_id: int, text: str):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="admin", language_code=None)
    message.chat = SimpleNamespace(id=user_id)
    message.text = text
    message.answer = AsyncMock()
    return message


async def _state(user_id: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


async def test_non_admin_is_ignored(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    message = _message(111, "/testpush")
    await admin.cmd_testpush(message, await _state(111))
    message.answer.assert_not_awaited()


async def test_reports_missing_token(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    monkeypatch.setattr(apns, "is_configured", lambda: True)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(apns, "send_alert", send)
    message = _message(ADMIN_ID, "/testpush")
    await admin.cmd_testpush(message, await _state(ADMIN_ID))
    send.assert_not_awaited()
    assert "нет iOS-токена" in message.answer.await_args.args[0]


async def test_sends_to_admin_token(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    monkeypatch.setattr(apns, "is_configured", lambda: True)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(apns, "send_alert", send)
    await fresh_db.get_or_create_user(telegram_id=ADMIN_ID, username="admin")
    await fresh_db.register_push_token(ADMIN_ID, "ios", "abc123")
    message = _message(ADMIN_ID, "/testpush")
    await admin.cmd_testpush(message, await _state(ADMIN_ID))
    assert send.await_args.args[:2] == (ADMIN_ID, "abc123")
    assert message.answer.await_args.args[0].startswith("✅")


async def test_explicit_user_and_rejection(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    monkeypatch.setattr(apns, "is_configured", lambda: True)
    send = AsyncMock(return_value=False)
    monkeypatch.setattr(apns, "send_alert", send)
    await fresh_db.get_or_create_user(telegram_id=42, username="u")
    await fresh_db.register_push_token(42, "ios", "tok42")
    message = _message(ADMIN_ID, "/testpush 42")
    await admin.cmd_testpush(message, await _state(ADMIN_ID))
    assert send.await_args.args[:2] == (42, "tok42")
    assert message.answer.await_args.args[0].startswith("❌")


async def test_not_configured(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    monkeypatch.setattr(apns, "is_configured", lambda: False)
    message = _message(ADMIN_ID, "/testpush")
    await admin.cmd_testpush(message, await _state(ADMIN_ID))
    assert "не настроен" in message.answer.await_args.args[0]
