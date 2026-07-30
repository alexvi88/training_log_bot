"""When a workout's start date differs from today, finishing it should ask
for confirmation instead of silently keeping the old (possibly stale) date —
otherwise a workout resumed days after it was abandoned gets misdated.

Finishing itself is now immediate (no note-prompt detour, see #170) — these
tests assert the completion card gets rendered directly once the date is
settled, one way or another.
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


def _make_bot():
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.delete_message = AsyncMock()
    return bot


def _make_callback(user_id: int, data: str, bot):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.text = "prompt"
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1, chat=SimpleNamespace(id=user_id)))
    message.bot = bot
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int, **extra_data) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.idle)
    await state.update_data(live_chat_id=user_id, live_message_id=1, **extra_data)
    return state


async def _log_a_set(db, workout_id: int, user_id: int):
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)
    await db.add_set(block_id, bench, 1, 0, 100, 5)


async def test_finish_button_asks_for_confirmation(fresh_db, user_id):
    db = fresh_db
    workout_id = await db.create_workout(user_id)
    await _log_a_set(db, workout_id, user_id)

    state = await _make_state(user_id, workout_id=workout_id)
    bot = _make_bot()
    callback = _make_callback(user_id, "live:finish_workout", bot)

    await workout.live_finish_workout(callback, state)

    assert await state.get_state() == WorkoutFlow.confirming_finish
    text = callback.message.answer.await_args.args[0]
    assert "Завершить тренировку?" in text
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "live:finish_confirmed" in callback_datas
    assert "live:cancel_finish" in callback_datas


async def test_same_day_finish_finalizes_directly(fresh_db, user_id):
    db = fresh_db
    workout_id = await db.create_workout(user_id)
    await _log_a_set(db, workout_id, user_id)

    state = await _make_state(user_id, workout_id=workout_id)
    await state.set_state(WorkoutFlow.confirming_finish)
    bot = _make_bot()
    callback = _make_callback(user_id, "live:finish_confirmed", bot)

    await workout.live_finish_workout_confirmed(callback, state)

    bot.edit_message_text.assert_awaited_once()
    assert await state.get_state() is None
    saved = await db.get_workout(workout_id)
    assert saved["status"] == "finished"


async def test_cross_day_finish_asks_for_confirmation(fresh_db, user_id):
    db = fresh_db
    started = dt.date.today() - dt.timedelta(days=4)
    workout_id = await db.create_workout(user_id, started_at=f"{started.isoformat()}T10:00:00")
    await _log_a_set(db, workout_id, user_id)

    state = await _make_state(user_id, workout_id=workout_id)
    await state.set_state(WorkoutFlow.confirming_finish)
    bot = _make_bot()
    callback = _make_callback(user_id, "live:finish_confirmed", bot)

    await workout.live_finish_workout_confirmed(callback, state)

    bot.edit_message_text.assert_not_awaited()
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

    state = await _make_state(
        user_id, workout_id=workout_id, is_backfill=True, bf_date=started.isoformat()
    )
    await state.set_state(WorkoutFlow.confirming_finish)
    bot = _make_bot()
    callback = _make_callback(user_id, "live:finish_confirmed", bot)

    await workout.live_finish_workout_confirmed(callback, state)

    bot.edit_message_text.assert_awaited_once()
    assert await state.get_state() is None
    saved = await db.get_workout(workout_id)
    assert saved["status"] == "finished"


async def test_keep_confirmation_finalizes(fresh_db, user_id):
    db = fresh_db
    started = dt.date.today() - dt.timedelta(days=4)
    workout_id = await db.create_workout(user_id, started_at=f"{started.isoformat()}T10:00:00")
    await _log_a_set(db, workout_id, user_id)

    state = await _make_state(user_id, workout_id=workout_id)
    await state.set_state(WorkoutFlow.confirming_finish_date)
    bot = _make_bot()
    callback = _make_callback(user_id, "finconfirm:keep", bot)

    await workout.finish_confirm_keep(callback, state)

    bot.edit_message_text.assert_awaited_once()
    assert await state.get_state() is None
    saved = await db.get_workout(workout_id)
    assert saved["status"] == "finished"
    assert saved["started_at"] == f"{started.isoformat()}T10:00:00"


async def test_changedate_then_quick_pick_finalizes(fresh_db, user_id):
    db = fresh_db
    started = dt.date.today() - dt.timedelta(days=4)
    workout_id = await db.create_workout(user_id, started_at=f"{started.isoformat()}T10:00:00")
    await _log_a_set(db, workout_id, user_id)

    state = await _make_state(user_id, workout_id=workout_id)
    await state.set_state(WorkoutFlow.confirming_finish_date)
    bot = _make_bot()
    changedate_cb = _make_callback(user_id, "finconfirm:changedate", bot)
    await workout.finish_confirm_changedate(changedate_cb, state)

    assert await state.get_state() == WorkoutFlow.awaiting_finish_date
    kb = changedate_cb.message.answer.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    today_cb = f"findate:date:{dt.date.today().isoformat()}"
    assert today_cb in callback_datas

    pick_cb = _make_callback(user_id, today_cb, bot)
    await workout.finish_date_quick(pick_cb, state)

    bot.edit_message_text.assert_awaited_once()
    assert await state.get_state() is None
    saved = await db.get_workout(workout_id)
    assert saved["status"] == "finished"
    assert saved["started_at"] == f"{dt.date.today().isoformat()}T10:00:00"


async def test_changedate_custom_text_finalizes(fresh_db, user_id):
    db = fresh_db
    started = dt.date.today() - dt.timedelta(days=4)
    workout_id = await db.create_workout(user_id, started_at=f"{started.isoformat()}T10:00:00")
    await _log_a_set(db, workout_id, user_id)

    state = await _make_state(user_id, workout_id=workout_id)
    await state.set_state(WorkoutFlow.awaiting_finish_date)

    bot = _make_bot()
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester")
    message.text = (dt.date.today() - dt.timedelta(days=1)).strftime("%d.%m.%Y")
    message.bot = bot
    message.reply = AsyncMock()

    await workout.finish_date_text(message, state)

    message.reply.assert_not_awaited()
    bot.edit_message_text.assert_awaited_once()
    assert await state.get_state() is None
    saved = await db.get_workout(workout_id)
    assert saved["status"] == "finished"
    expected = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    assert saved["started_at"] == f"{expected}T10:00:00"
