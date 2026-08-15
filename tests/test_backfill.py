"""Находка 4: двойной тап по дате в бэкфилле создавал сирота-тренировку
(handlers/backfill.py:_date_chosen). Гвард от повторного тапа — тем же
приёмом, что _confirming в handlers/workout.py — плюс порядок, где
create_workout и state.update_data происходят раньше, чем что-либо может
свалиться на удалении старого экрана календаря."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import BackfillFlow
from handlers import backfill

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.answer = AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=2))
    message.delete = AsyncMock()
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=MemoryStorage(), key=key)
    await state.set_state(BackfillFlow.awaiting_date)
    return state


async def test_double_tap_on_same_date_creates_only_one_workout(fresh_db, user_id, monkeypatch):
    """A second tap racing in while the first is still inside `_date_chosen`
    (still awaiting `_picker_screen_groups`) must be turned away, not create
    its own workout row."""
    db = fresh_db
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "bf:date:2026-01-05")

    calls = {"n": 0}

    async def _fake_picker_screen_groups(event, state_):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate a second tap landing while the first is still in flight —
            # the guard must still be held at this point.
            second = _make_callback(user_id, "bf:date:2026-01-05")
            await backfill.bf_date_quick(second, state_)
            assert second.answer.await_count == 1  # turned away, no alert text

    monkeypatch.setattr("handlers.workout._picker_screen_groups", _fake_picker_screen_groups)

    await backfill.bf_date_quick(callback, state)

    workouts = await db.list_workouts(user_id, status="backfill")
    assert len(workouts) == 1
    data = await state.get_data()
    assert data["workout_id"] == workouts[0]["id"]
    assert calls["n"] == 1  # the racing second tap never reached the picker
    assert user_id not in backfill._picking  # guard released after completion


async def test_no_orphan_workout_when_picker_screen_fails(fresh_db, user_id, monkeypatch):
    """Even if rendering the next screen blows up (e.g. the old calendar
    message is long gone), the just-created workout is already linked into
    state — nothing before that point depended on the old message surviving."""
    db = fresh_db
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "bf:date:2026-01-05")
    # The old-style bug deleted the calendar message unsuppressed *before*
    # linking the workout into state; simulate that message being long gone.
    callback.message.delete = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="message to delete not found"))

    async def _boom(event, state_):
        raise TelegramBadRequest(method=MagicMock(), message="MESSAGE_ID_INVALID")

    monkeypatch.setattr("handlers.workout._picker_screen_groups", _boom)

    with pytest.raises(TelegramBadRequest):
        await backfill.bf_date_quick(callback, state)

    data = await state.get_data()
    assert data.get("workout_id") is not None
    workout = await db.get_workout(data["workout_id"])
    assert workout is not None
    assert workout["status"] == "backfill"
    # _date_chosen no longer deletes the old calendar prompt itself — that's
    # left to the picker screen's own suppressed cleanup — so a stale/gone
    # message there can no longer abort linking the workout into state.
    callback.message.delete.assert_not_awaited()
    # The guard is released even when the flow raises.
    assert user_id not in backfill._picking


async def test_typed_date_double_tap_is_guarded(fresh_db, user_id, monkeypatch):
    """Same guard, text-input entry point (bf_date_text)."""
    state = await _make_state(user_id)

    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester")
    message.text = "05.01.2026"
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.answer = AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=2))

    calls = {"n": 0}

    async def _fake_picker_screen_groups(event, state_):
        calls["n"] += 1

    monkeypatch.setattr("handlers.workout._picker_screen_groups", _fake_picker_screen_groups)
    # Hold the guard as if a tap is already in flight.
    backfill._picking.add(user_id)
    try:
        await backfill.bf_date_text(message, state)
    finally:
        backfill._picking.discard(user_id)

    assert calls["n"] == 0  # turned away while the guard was held
    data = await state.get_data()
    assert data.get("workout_id") is None
