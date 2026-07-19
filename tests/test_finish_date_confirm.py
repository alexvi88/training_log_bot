"""When a workout's start date differs from today, finishing it should ask
for confirmation instead of silently keeping the old (possibly stale) date —
otherwise a workout resumed days after it was abandoned gets misdated.
"""
import datetime as dt
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
    message = MagicMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1, chat=SimpleNamespace(id=user_id)))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int, **extra_data) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.idle)
    await state.update_data(**extra_data)
    return state


async def _log_a_set(db, workout_id: int, user_id: int):
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)
    await db.add_set(block_id, bench, 1, 0, 100, 5)


async def test_same_day_finish_skips_confirmation(fresh_db, user_id):
    db = fresh_db
    workout_id = await db.create_workout(user_id)
    await _log_a_set(db, workout_id, user_id)

    state = await _make_state(user_id, workout_id=workout_id)
    callback = _make_callback(user_id, "live:finish_workout")

    await workout.live_finish_workout(callback, state)

    assert "Завершаем?" in callback.message.answer.await_args.args[0]
    assert await state.get_state() == WorkoutFlow.idle


async def test_cross_day_finish_asks_for_confirmation(fresh_db, user_id):
    db = fresh_db
    started = dt.date.today() - dt.timedelta(days=4)
    workout_id = await db.create_workout(user_id, started_at=f"{started.isoformat()}T10:00:00")
    await _log_a_set(db, workout_id, user_id)

    state = await _make_state(user_id, workout_id=workout_id)
    callback = _make_callback(user_id, "live:finish_workout")

    await workout.live_finish_workout(callback, state)

    text = callback.message.answer.await_args.args[0]
    assert "Всё верно?" in text
    assert await state.get_state() == WorkoutFlow.confirming_finish_date
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "finconfirm:keep" in callback_datas
    assert "finconfirm:changedate" in callback_datas
    assert "live:cancel_finish" in callback_datas


async def test_backfill_workout_skips_confirmation(fresh_db, user_id):
    db = fresh_db
    started = dt.date.today() - dt.timedelta(days=4)
    workout_id = await db.create_workout(
        user_id, started_at=f"{started.isoformat()}T12:00:00", status="backfill"
    )
    await _log_a_set(db, workout_id, user_id)

    state = await _make_state(user_id, workout_id=workout_id, is_backfill=True, bf_date=started.isoformat())
    callback = _make_callback(user_id, "live:finish_workout")

    await workout.live_finish_workout(callback, state)

    assert "Завершаем?" in callback.message.answer.await_args.args[0]
    assert await state.get_state() == WorkoutFlow.idle


async def test_keep_confirmation_proceeds_to_finish_prompt(fresh_db, user_id):
    db = fresh_db
    started = dt.date.today() - dt.timedelta(days=4)
    workout_id = await db.create_workout(user_id, started_at=f"{started.isoformat()}T10:00:00")

    state = await _make_state(user_id, workout_id=workout_id)
    await state.set_state(WorkoutFlow.confirming_finish_date)
    callback = _make_callback(user_id, "finconfirm:keep")

    await workout.finish_confirm_keep(callback, state)

    assert "Завершаем?" in callback.message.answer.await_args.args[0]
    assert await state.get_state() == WorkoutFlow.idle
    saved = await db.get_workout(workout_id)
    assert saved["started_at"] == f"{started.isoformat()}T10:00:00"


async def test_changedate_then_quick_pick_updates_started_at(fresh_db, user_id):
    db = fresh_db
    started = dt.date.today() - dt.timedelta(days=4)
    workout_id = await db.create_workout(user_id, started_at=f"{started.isoformat()}T10:00:00")

    state = await _make_state(user_id, workout_id=workout_id)
    await state.set_state(WorkoutFlow.confirming_finish_date)
    changedate_cb = _make_callback(user_id, "finconfirm:changedate")
    await workout.finish_confirm_changedate(changedate_cb, state)

    assert await state.get_state() == WorkoutFlow.awaiting_finish_date
    kb = changedate_cb.message.answer.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    today_cb = f"findate:date:{dt.date.today().isoformat()}"
    assert today_cb in callback_datas

    pick_cb = _make_callback(user_id, today_cb)
    await workout.finish_date_quick(pick_cb, state)

    assert await state.get_state() == WorkoutFlow.idle
    assert "Завершаем?" in pick_cb.message.answer.await_args.args[0]
    saved = await db.get_workout(workout_id)
    assert saved["started_at"] == f"{dt.date.today().isoformat()}T10:00:00"


async def test_changedate_custom_text_updates_started_at(fresh_db, user_id):
    db = fresh_db
    started = dt.date.today() - dt.timedelta(days=4)
    workout_id = await db.create_workout(user_id, started_at=f"{started.isoformat()}T10:00:00")

    state = await _make_state(user_id, workout_id=workout_id)
    await state.set_state(WorkoutFlow.awaiting_finish_date)

    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester")
    message.text = (dt.date.today() - dt.timedelta(days=1)).strftime("%d.%m.%Y")
    message.answer = AsyncMock()
    message.reply = AsyncMock()

    await workout.finish_date_text(message, state)

    message.reply.assert_not_awaited()
    assert "Завершаем?" in message.answer.await_args.args[0]
    assert await state.get_state() == WorkoutFlow.idle
    saved = await db.get_workout(workout_id)
    expected = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    assert saved["started_at"] == f"{expected}T10:00:00"
