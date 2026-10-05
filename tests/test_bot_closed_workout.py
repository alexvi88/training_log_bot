"""Бот не пишет подход в закрытую или удалённую тренировку и не заводит второй
блок для упражнения, которое уже записало приложение."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import i18n
from fsm import WorkoutFlow
from handlers import workout

pytestmark = pytest.mark.asyncio


def _msg(user_id: int, text: str):
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=user_id)
    msg.message_id = 55
    msg.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    msg.text = text
    msg.delete = AsyncMock()
    msg.reply = AsyncMock()
    msg.answer = AsyncMock()
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.set_message_reaction = AsyncMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=700, chat=SimpleNamespace(id=user_id))
    )
    msg.bot = bot
    return msg


def _cb(user_id: int):
    cb = _msg(user_id, "")
    cb.data = ""
    return cb


async def _state(user_id: int, **extra) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=MemoryStorage(), key=key)
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        open_exercises=[], open_blocks={}, last_by_exercise={}, last_session_sets={},
        weight_steps={}, planned_blocks=[], exercise_targets={},
        live_chat_id=user_id, live_message_id=42, **extra,
    )
    return state


async def _open_session(db, user_id):
    gid = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим", gid)
    wid = await db.create_workout(user_id)
    state = await _state(user_id, workout_id=wid)
    await workout._on_exercise_chosen(_cb(user_id), state, ex_id)
    data = await state.get_data()
    await state.update_data(active_exercise_id=ex_id)
    return wid, ex_id, data["open_blocks"][ex_id], state


async def test_set_into_finished_workout_is_refused(fresh_db, user_id):
    db = fresh_db
    wid, ex_id, block_id, state = await _open_session(db, user_id)
    await db.finish_workout(wid)
    msg = _msg(user_id, "100 5")
    await workout.log_set_text(msg, state)
    assert await db.list_sets_for_block(block_id) == []
    msg.answer.assert_awaited_once()
    assert msg.answer.await_args.args[0] == i18n.t("workout.closed_cant_log")
    assert await state.get_state() is None


async def test_set_into_deleted_workout_is_refused(fresh_db, user_id):
    db = fresh_db
    wid, ex_id, block_id, state = await _open_session(db, user_id)
    await db.discard_workout(wid)
    msg = _msg(user_id, "100 5")
    await workout.log_set_text(msg, state)
    msg.answer.assert_awaited_once()
    assert await state.get_state() is None


async def test_repeat_button_into_finished_workout_is_refused(fresh_db, user_id):
    db = fresh_db
    wid, ex_id, block_id, state = await _open_session(db, user_id)
    await workout.log_set_text(_msg(user_id, "100 5"), state)
    await db.finish_workout(wid)
    cb = _cb(user_id)
    await workout.live_repeat_set(cb, state)
    assert len(await db.list_sets_for_block(block_id)) == 1
    cb.answer.assert_awaited()
    assert await state.get_state() is None


async def test_integrity_error_fallback(fresh_db, user_id, monkeypatch):
    db = fresh_db
    wid, ex_id, block_id, state = await _open_session(db, user_id)
    import sqlite3

    async def boom(*a, **k):
        raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")

    monkeypatch.setattr(db, "append_set", boom)
    with pytest.raises(workout.WorkoutClosedError):
        await workout._log_one(block_id, ex_id, 100, 5)


async def test_open_session_set_still_works(fresh_db, user_id):
    db = fresh_db
    wid, ex_id, block_id, state = await _open_session(db, user_id)
    await workout.log_set_text(_msg(user_id, "100 5"), state)
    assert len(await db.list_sets_for_block(block_id)) == 1


async def test_choosing_exercise_reuses_block_made_by_the_app(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим", gid)
    wid = await db.create_workout(user_id)
    app_block = await db.get_or_create_single_block_for_exercise(wid, ex_id)
    await db.append_set(app_block, ex_id, 0, 60.0, 8)
    state = await _state(user_id, workout_id=wid)
    await workout._on_exercise_chosen(_cb(user_id), state, ex_id)
    assert (await state.get_data())["open_blocks"][ex_id] == app_block
    assert len(await db.list_blocks_for_workout(wid)) == 1
