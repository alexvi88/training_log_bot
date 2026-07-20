"""The 'Карточка упражнения' button on the active-logging screen: shows the
exercise's photo/technique info as a dismissable card and returns to the
live tracker cleanly (mirrors handlers.exercises' media send/clear pattern)."""
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
    next_answer_id = iter(range(600, 700))

    async def _answer(*args, **kwargs):
        return SimpleNamespace(message_id=next(next_answer_id))

    message.answer = AsyncMock(side_effect=_answer)
    message.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=501))
    next_media_id = iter(range(1, 100))

    async def _answer_media_group(*args, **kwargs):
        return [SimpleNamespace(message_id=next(next_media_id)) for _ in range(2)]

    message.answer_media_group = AsyncMock(side_effect=_answer_media_group)
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    message.bot = bot
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id)
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.logging_set)
    return state


def test_logging_keyboard_has_card_button_for_active_exercise():
    kb = keyboards.logging_keyboard([(1, "Bench press")], active_id=1, has_sets=False)
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "live:card:1" in callback_datas


def test_logging_keyboard_omits_card_button_without_active_exercise():
    kb = keyboards.logging_keyboard([], active_id=None, has_sets=False)
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert not any(cb.startswith("live:card:") for cb in callback_datas)


def test_logging_keyboard_omits_card_button_once_sets_are_logged():
    kb = keyboards.logging_keyboard([(1, "Bench press")], active_id=1, has_sets=True)
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert not any(cb.startswith("live:card:") for cb in callback_datas)


@pytest.mark.asyncio
async def test_live_card_show_sends_info_and_back_button(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Совсем новое упражнение XYZ", group_id)

    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"live:card:{bench}")

    await workout.live_card_show(callback, state)

    assert callback.message.answer.await_count == 2
    info_call, back_call = callback.message.answer.await_args_list
    assert "Совсем новое упражнение XYZ" in info_call.args[0]
    kb = back_call.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert callback_datas == ["live:card_back"]

    data = await state.get_data()
    assert data["live_card_msg_ids"] == [600, 601]


@pytest.mark.asyncio
async def test_live_card_show_prefers_custom_photo(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", group_id)
    await db.set_exercise_photo(bench, "custom_file_id_123")

    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"live:card:{bench}")

    await workout.live_card_show(callback, state)

    callback.message.answer_photo.assert_awaited_once()
    assert callback.message.answer_photo.await_args.args[0] == "custom_file_id_123"
    callback.message.answer.assert_awaited_once()  # the back-button message

    data = await state.get_data()
    assert data["live_card_msg_ids"] == [501, 600]


@pytest.mark.asyncio
async def test_live_card_back_deletes_tracked_messages(user_id):
    state = await _make_state(user_id)
    await state.update_data(live_card_msg_ids=[500, 501])
    callback = _make_callback(user_id, "live:card_back")

    await workout.live_card_back(callback, state)

    assert callback.bot.delete_message.await_count == 2
    deleted_ids = {c.args[1] for c in callback.bot.delete_message.await_args_list}
    assert deleted_ids == {500, 501}

    data = await state.get_data()
    assert data["live_card_msg_ids"] is None


@pytest.mark.asyncio
async def test_live_card_show_rejects_other_users_exercise(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    other_user = (await db.get_or_create_user(telegram_id=999, username="other"))["telegram_id"]
    other_ex = await db.create_exercise(other_user, "Чужое упражнение", group_id)

    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"live:card:{other_ex}")

    await workout.live_card_show(callback, state)

    callback.message.answer.assert_not_awaited()
    callback.answer.assert_awaited_once_with("Упражнение не найдено", show_alert=True)
