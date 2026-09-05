"""Finishing no longer stops to ask about a note (see #170) — instead the
completion card carries a "📝 Заметка" button that lets you attach/edit one
afterward, editing the card message in place."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import keyboards
from fsm import WorkoutFlow
from handlers import workout


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 42
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=99))
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    message.bot = bot
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


async def _finished_workout(db, user_id: int) -> int:
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)
    await db.add_set(block_id, bench, 1, 0, 100, 5)
    await db.finish_workout(workout_id)
    return workout_id


def test_workout_card_keyboard_has_note_button():
    kb = keyboards.workout_card_keyboard(7)
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "live:addnote:7" in callback_datas


@pytest.mark.asyncio
async def test_addnote_prompt_asks_for_text(fresh_db, user_id):
    db = fresh_db
    workout_id = await _finished_workout(db, user_id)
    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"live:addnote:{workout_id}")

    await workout.workout_card_note_prompt(callback, state)

    assert await state.get_state() == WorkoutFlow.editing_finished_note
    data = await state.get_data()
    assert data["note_workout_id"] == workout_id
    assert data["note_chat_id"] == user_id
    assert data["note_message_id"] == 42
    callback.message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_addnote_prompt_shows_clear_hint_only_when_a_note_exists(fresh_db, user_id):
    db = fresh_db
    workout_id = await _finished_workout(db, user_id)
    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"live:addnote:{workout_id}")

    await workout.workout_card_note_prompt(callback, state)
    text = callback.message.answer.await_args.args[0]
    assert "Пришли «-»" not in text

    await db.update_workout_note(workout_id, "болело плечо")
    callback2 = _make_callback(user_id, f"live:addnote:{workout_id}")
    await workout.workout_card_note_prompt(callback2, state)
    text2 = callback2.message.answer.await_args.args[0]
    assert "Пришли «-»" in text2


@pytest.mark.asyncio
async def test_addnote_entered_saves_and_edits_card_in_place(fresh_db, user_id):
    db = fresh_db
    workout_id = await _finished_workout(db, user_id)
    state = await _make_state(user_id)
    await state.set_state(WorkoutFlow.editing_finished_note)
    await state.update_data(note_workout_id=workout_id, note_chat_id=user_id, note_message_id=42)

    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.text = "болело плечо"
    message.reply = AsyncMock()
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    message.bot = bot

    await workout.workout_card_note_entered(message, state)

    saved = await db.get_workout(workout_id)
    assert saved["note"] == "болело плечо"
    bot.edit_message_text.assert_awaited_once()
    kwargs = bot.edit_message_text.await_args.kwargs
    assert kwargs["chat_id"] == user_id
    assert kwargs["message_id"] == 42
    assert "болело плечо" in kwargs["text"]
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_addnote_dash_clears_an_existing_note(fresh_db, user_id):
    db = fresh_db
    workout_id = await _finished_workout(db, user_id)
    await db.update_workout_note(workout_id, "болело плечо")
    state = await _make_state(user_id)
    await state.set_state(WorkoutFlow.editing_finished_note)
    await state.update_data(note_workout_id=workout_id, note_chat_id=user_id, note_message_id=42)

    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.text = "-"
    message.reply = AsyncMock()
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    message.bot = bot

    await workout.workout_card_note_entered(message, state)

    saved = await db.get_workout(workout_id)
    assert saved["note"] is None


@pytest.mark.asyncio
async def test_addnote_cancel_clears_state_without_saving(fresh_db, user_id):
    db = fresh_db
    workout_id = await _finished_workout(db, user_id)
    state = await _make_state(user_id)
    await state.set_state(WorkoutFlow.editing_finished_note)
    await state.update_data(note_workout_id=workout_id, note_chat_id=user_id, note_message_id=42)
    callback = _make_callback(user_id, "live:addnote_cancel")

    await workout.workout_card_note_cancel(callback, state)

    assert await state.get_state() is None
    saved = await db.get_workout(workout_id)
    assert saved["note"] is None


@pytest.mark.asyncio
async def test_addnote_rejects_other_users_workout(fresh_db, user_id):
    db = fresh_db
    other_user = (await db.get_or_create_user(telegram_id=999, username="other"))["telegram_id"]
    workout_id = await _finished_workout(db, other_user)
    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"live:addnote:{workout_id}")

    await workout.workout_card_note_prompt(callback, state)

    callback.answer.assert_awaited_once_with("Не нашёл эту тренировку — экран устарел. Вернись назад и открой заново", show_alert=True)
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_note_on_an_old_card_returns_to_the_live_session(fresh_db, user_id):
    """Finished cards keep their 📝 button forever, so this flow can start in
    the middle of a live session. Clearing the state afterwards took the active
    workout's open tabs and carried weights with it: the tracker went dead and
    typed sets fell through to the "Не понял 🤔" fallback."""
    db = fresh_db
    old_workout = await _finished_workout(db, user_id)

    live_workout = await db.create_workout(user_id)
    group_id = await db.create_muscle_group(user_id, "Спина")
    row_ex = await db.create_exercise(user_id, "Тяга", group_id)
    live_block = await db.create_block(live_workout, "single")
    await db.add_block_exercise(live_block, row_ex, 0)

    state = await _make_state(user_id)
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        workout_id=live_workout, live_chat_id=user_id, live_message_id=7,
        open_exercises=[row_ex], open_blocks={row_ex: live_block},
        active_exercise_id=row_ex, last_by_exercise={row_ex: (80.0, 8)},
    )

    await workout.workout_card_note_prompt(
        _make_callback(user_id, f"live:addnote:{old_workout}"), state
    )
    assert await state.get_state() == WorkoutFlow.editing_finished_note

    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.text = "спина была уставшая"
    message.reply = AsyncMock()
    message.bot = MagicMock()
    message.bot.edit_message_text = AsyncMock()

    await workout.workout_card_note_entered(message, state)

    assert (await db.get_workout(old_workout))["note"] == "спина была уставшая"
    assert await state.get_state() == WorkoutFlow.logging_set
    data = await state.get_data()
    assert data["workout_id"] == live_workout
    assert data["open_blocks"] == {row_ex: live_block}
    assert data["active_exercise_id"] == row_ex


@pytest.mark.asyncio
async def test_note_cancel_on_an_old_card_returns_to_the_live_session(fresh_db, user_id):
    db = fresh_db
    old_workout = await _finished_workout(db, user_id)
    live_workout = await db.create_workout(user_id)

    state = await _make_state(user_id)
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(workout_id=live_workout, open_exercises=[], open_blocks={})

    await workout.workout_card_note_prompt(
        _make_callback(user_id, f"live:addnote:{old_workout}"), state
    )
    await workout.workout_card_note_cancel(_make_callback(user_id, "live:addnote_cancel"), state)

    assert await state.get_state() == WorkoutFlow.logging_set
    assert (await state.get_data())["workout_id"] == live_workout
