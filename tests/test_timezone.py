"""User timezone: timeutil helpers, the settings picker, and persistence."""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import keyboards
import timeutil
from handlers import settings


def test_user_now_shifts_by_offset():
    base = timeutil.user_now({"tz_offset": 0})
    plus3 = timeutil.user_now({"tz_offset": 3})
    # ~3h apart (allow a second of wall-clock drift between the two calls).
    assert abs((plus3 - base) - dt.timedelta(hours=3)) < dt.timedelta(seconds=2)


def test_missing_offset_defaults_to_zero():
    assert timeutil._offset_hours(None) == 0
    assert timeutil._offset_hours({}) == 0


def test_to_user_local():
    ts = dt.datetime(2026, 7, 23, 22, 0, 0)
    assert timeutil.to_user_local(ts, {"tz_offset": 3}) == dt.datetime(2026, 7, 24, 1, 0, 0)


def test_format_utc_offset():
    assert keyboards.format_utc_offset(0) == "UTC"
    assert keyboards.format_utc_offset(3) == "UTC+3"
    assert keyboards.format_utc_offset(-1) == "UTC-1"


def test_picker_marks_current_and_has_all_offsets():
    kb = keyboards.timezone_picker_keyboard(3)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "settings:tzset:-1" in cbs
    assert "settings:tzset:12" in cbs
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "• UTC+3 •" in texts


def _callback(user_id, data):
    message = MagicMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    cb = MagicMock()
    cb.from_user = SimpleNamespace(id=user_id, username="t")
    cb.message = message
    cb.data = data
    cb.answer = AsyncMock()
    return cb


@pytest.mark.asyncio
async def test_setting_timezone_persists(fresh_db, user_id):
    db = fresh_db
    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    cb = _callback(user_id, "settings:tzset:5")

    await settings.settings_timezone_set(cb, state)

    user = await db.get_user(user_id)
    assert user["tz_offset"] == 5
