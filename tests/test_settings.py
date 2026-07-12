"""Settings screen: unit/formula toggles and the pushes on/off switch."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from handlers import settings

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


async def test_settings_pushes_toggles_off_then_on(fresh_db, user_id):
    db = fresh_db
    state = await _make_state(user_id)

    callback = _make_callback(user_id, "settings:pushes")
    await settings.settings_pushes(callback, state)
    assert (await db.get_user(user_id))["pushes_enabled"] == 0

    callback = _make_callback(user_id, "settings:pushes")
    await settings.settings_pushes(callback, state)
    assert (await db.get_user(user_id))["pushes_enabled"] == 1


async def test_show_settings_reflects_pushes_state_in_keyboard_labels(fresh_db, user_id):
    db = fresh_db
    state = await _make_state(user_id)

    callback = _make_callback(user_id, "menu:settings")
    await settings.show_settings(callback, state)
    sent_text = callback.message.answer.call_args.kwargs["reply_markup"]
    labels_on = [b.text for row in sent_text.inline_keyboard for b in row]
    assert any("включены" in label for label in labels_on)

    await db.update_user(user_id, pushes_enabled=0)
    callback = _make_callback(user_id, "menu:settings")
    await settings.show_settings(callback, state)
    sent_text = callback.message.answer.call_args.kwargs["reply_markup"]
    labels_off = [b.text for row in sent_text.inline_keyboard for b in row]
    assert any("выключены" in label for label in labels_off)
