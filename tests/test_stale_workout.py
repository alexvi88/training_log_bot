"""/start warns about a long-abandoned workout and offers one-tap actions to
resolve it (finish retroactively, or delete) instead of leaving the user to
figure out how to close it themselves.
"""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import config
from handlers import workout

pytestmark = pytest.mark.asyncio


def _make_message(user_id: int):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester")
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=999))
    return message


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1, chat=SimpleNamespace(id=user_id)))
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


async def test_start_warns_and_offers_buttons_for_stale_workout(fresh_db, user_id):
    db = fresh_db
    stale_started = dt.datetime.now() - dt.timedelta(hours=config.STALE_WORKOUT_HOURS + 1)
    workout_id = await db.create_workout(user_id, started_at=stale_started.isoformat())

    message = _make_message(user_id)
    state = await _make_state(user_id)

    await workout.cmd_start(message, state)

    assert message.answer.await_count == 2
    warning_call = message.answer.await_args_list[1]
    assert "висит тренировка" in warning_call.args[0]
    kb = warning_call.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"stale:finish:{workout_id}" in callback_datas
    assert f"stale:delete:{workout_id}" in callback_datas


async def test_start_does_not_warn_for_recent_workout(fresh_db, user_id):
    db = fresh_db
    await db.create_workout(user_id)

    message = _make_message(user_id)
    state = await _make_state(user_id)

    await workout.cmd_start(message, state)

    assert message.answer.await_count == 1


async def test_stale_finish_marks_workout_finished_with_original_date(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    started = dt.datetime.now() - dt.timedelta(hours=config.STALE_WORKOUT_HOURS + 1)
    workout_id = await db.create_workout(user_id, started_at=started.isoformat())
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)
    await db.add_set(block_id, bench, 1, 0, 100, 8)

    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"stale:finish:{workout_id}")

    await workout.stale_finish_workout(callback, state)

    saved = await db.get_workout(workout_id)
    assert saved["status"] == "finished"
    assert saved["finished_at"] == started.isoformat()
    assert "завершена" in callback.message.answer.await_args.args[0]


async def test_stale_finish_discards_empty_workout(fresh_db, user_id):
    db = fresh_db
    started = dt.datetime.now() - dt.timedelta(hours=config.STALE_WORKOUT_HOURS + 1)
    workout_id = await db.create_workout(user_id, started_at=started.isoformat())

    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"stale:finish:{workout_id}")

    await workout.stale_finish_workout(callback, state)

    assert await db.get_workout(workout_id) is None


async def test_start_workout_creates_and_enters_picker_immediately(fresh_db, user_id):
    db = fresh_db
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "menu:start_workout")

    await workout.start_workout(callback, state)

    active = await db.get_active_workout(user_id)
    assert active is not None
    data = await state.get_data()
    assert data["workout_id"] == active["id"]

    kb = callback.bot.send_message.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "rt:manage" in callback_datas
    assert "pick:cancel" in callback_datas


async def test_stale_delete_requires_confirmation_then_deletes(fresh_db, user_id):
    db = fresh_db
    started = dt.datetime.now() - dt.timedelta(hours=config.STALE_WORKOUT_HOURS + 1)
    workout_id = await db.create_workout(user_id, started_at=started.isoformat())

    state = await _make_state(user_id)
    confirm_callback = _make_callback(user_id, f"stale:delete:{workout_id}")
    await workout.stale_delete_confirm(confirm_callback, state)

    kb = confirm_callback.message.answer.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"stale:delyes:{workout_id}" in callback_datas

    # Still there until the yes button is actually tapped.
    assert await db.get_workout(workout_id) is not None

    delete_callback = _make_callback(user_id, f"stale:delyes:{workout_id}")
    await workout.stale_delete(delete_callback, state)

    assert await db.get_workout(workout_id) is None
