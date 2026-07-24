"""Screens are edited in place while they're still the last message in the chat,
and only deleted-and-resent once something has landed below them."""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message

import chat_bottom
import ui
from fsm import WorkoutFlow
from handlers import workout

pytestmark = pytest.mark.asyncio

CHAT_ID = 777
SCREEN_ID = 10


@pytest.fixture(autouse=True)
def clean_tracker():
    chat_bottom.reset()
    yield
    chat_bottom.reset()


def _make_callback(text: str | None = "экран", photo=None):
    message = MagicMock()
    message.chat = SimpleNamespace(id=CHAT_ID)
    message.message_id = SCREEN_ID
    message.text = text
    message.photo = photo
    message.edit_text = AsyncMock(return_value=True)
    message.edit_media = AsyncMock(return_value=True)
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=SCREEN_ID + 1))
    message.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=SCREEN_ID + 1))
    callback = MagicMock()
    callback.message = message
    return callback


async def test_safe_edit_edits_in_place_when_screen_is_at_bottom():
    chat_bottom.note_message(CHAT_ID, SCREEN_ID)
    callback = _make_callback()

    await ui.safe_edit(callback, "новый текст")

    callback.message.edit_text.assert_awaited_once()
    callback.message.delete.assert_not_awaited()
    callback.message.answer.assert_not_awaited()


async def test_safe_edit_resends_when_something_landed_below():
    chat_bottom.note_message(CHAT_ID, SCREEN_ID)
    chat_bottom.note_message(CHAT_ID, SCREEN_ID + 1)  # a push, a kept 🔥 set, anything
    callback = _make_callback()

    await ui.safe_edit(callback, "новый текст")

    callback.message.edit_text.assert_not_awaited()
    callback.message.delete.assert_awaited_once()
    callback.message.answer.assert_awaited_once()


async def test_safe_edit_resends_when_the_chat_is_unknown():
    """After a restart the tracker knows nothing — fall back to the safe path."""
    callback = _make_callback()

    await ui.safe_edit(callback, "новый текст")

    callback.message.edit_text.assert_not_awaited()
    callback.message.delete.assert_awaited_once()


async def test_safe_edit_replaces_a_photo_screen_with_text():
    """A photo message can't be edited into a text one, even at the bottom."""
    chat_bottom.note_message(CHAT_ID, SCREEN_ID)
    callback = _make_callback(text=None, photo=[SimpleNamespace(file_id="x")])

    await ui.safe_edit(callback, "новый текст")

    callback.message.edit_text.assert_not_awaited()
    callback.message.delete.assert_awaited_once()
    callback.message.answer.assert_awaited_once()


async def test_safe_edit_falls_back_to_resend_when_the_edit_is_rejected():
    chat_bottom.note_message(CHAT_ID, SCREEN_ID)
    callback = _make_callback()
    callback.message.edit_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="message to edit not found")
    )

    await ui.safe_edit(callback, "новый текст")

    callback.message.delete.assert_awaited_once()
    callback.message.answer.assert_awaited_once()


async def test_safe_edit_treats_unchanged_content_as_done():
    """A double tap must not turn into a pointless delete+resend."""
    chat_bottom.note_message(CHAT_ID, SCREEN_ID)
    callback = _make_callback()
    callback.message.edit_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: message is not modified")
    )

    result = await ui.safe_edit(callback, "новый текст")

    assert result is callback.message
    callback.message.delete.assert_not_awaited()
    callback.message.answer.assert_not_awaited()


async def test_safe_edit_keeps_the_message_when_delete_is_off():
    chat_bottom.note_message(CHAT_ID, SCREEN_ID)
    callback = _make_callback()

    await ui.safe_edit(callback, "новый текст", delete=False)

    callback.message.edit_text.assert_not_awaited()
    callback.message.delete.assert_not_awaited()
    callback.message.answer.assert_awaited_once()


async def test_safe_edit_photo_swaps_media_in_place_at_bottom():
    chat_bottom.note_message(CHAT_ID, SCREEN_ID)
    callback = _make_callback(text=None, photo=[SimpleNamespace(file_id="x")])

    await ui.safe_edit_photo(callback, b"png", "chart.png", "подпись")

    callback.message.edit_media.assert_awaited_once()
    callback.message.delete.assert_not_awaited()
    callback.message.answer_photo.assert_not_awaited()


async def test_safe_edit_photo_replaces_a_text_screen():
    chat_bottom.note_message(CHAT_ID, SCREEN_ID)
    callback = _make_callback()

    await ui.safe_edit_photo(callback, b"png", "chart.png", "подпись")

    callback.message.edit_media.assert_not_awaited()
    callback.message.delete.assert_awaited_once()
    callback.message.answer_photo.assert_awaited_once()


# ---------- the live workout tracker ----------


def _make_bot():
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=SCREEN_ID + 5))
    return bot


async def _live_state(user_id: int, workout_id: int) -> FSMContext:
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=CHAT_ID, user_id=user_id))
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(live_chat_id=CHAT_ID, live_message_id=SCREEN_ID, workout_id=workout_id)
    return state


async def test_live_screen_is_edited_after_the_typed_set_is_tidied_away(fresh_db, user_id):
    """The normal logging round trip: the user's message is deleted, so the
    tracker is the bottom message again and can just be redrawn."""
    workout_id = await fresh_db.create_workout(user_id)
    state = await _live_state(user_id, workout_id)
    bot = _make_bot()
    chat_bottom.note_message(CHAT_ID, SCREEN_ID)
    chat_bottom.note_message(CHAT_ID, SCREEN_ID + 1)
    chat_bottom.note_deleted(CHAT_ID, SCREEN_ID + 1)

    user = await fresh_db.get_user(user_id)
    await workout._refresh_live(bot, state, user, workout_id, None, None)

    bot.edit_message_text.assert_awaited_once()
    bot.delete_message.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    assert (await state.get_data())["live_message_id"] == SCREEN_ID


async def test_live_screen_is_resent_when_a_record_set_stays_in_the_chat(fresh_db, user_id):
    """A record keeps its 🔥 message in the chat, so an edit would strand the
    tracker above it — that path still deletes and resends."""
    workout_id = await fresh_db.create_workout(user_id)
    state = await _live_state(user_id, workout_id)
    bot = _make_bot()
    chat_bottom.note_message(CHAT_ID, SCREEN_ID)
    chat_bottom.note_message(CHAT_ID, SCREEN_ID + 1)

    user = await fresh_db.get_user(user_id)
    await workout._refresh_live(bot, state, user, workout_id, None, None)

    bot.edit_message_text.assert_not_awaited()
    bot.delete_message.assert_awaited_once()
    bot.send_message.assert_awaited_once()
    assert (await state.get_data())["live_message_id"] == SCREEN_ID + 5


async def test_live_screen_falls_back_to_resend_when_the_edit_is_rejected(fresh_db, user_id):
    workout_id = await fresh_db.create_workout(user_id)
    state = await _live_state(user_id, workout_id)
    bot = _make_bot()
    bot.edit_message_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="message to edit not found")
    )
    chat_bottom.note_message(CHAT_ID, SCREEN_ID)

    user = await fresh_db.get_user(user_id)
    await workout._refresh_live(bot, state, user, workout_id, None, None)

    bot.send_message.assert_awaited_once()
    assert (await state.get_data())["live_message_id"] == SCREEN_ID + 5


async def test_live_screen_treats_unchanged_content_as_done(fresh_db, user_id):
    workout_id = await fresh_db.create_workout(user_id)
    state = await _live_state(user_id, workout_id)
    bot = _make_bot()
    bot.edit_message_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: message is not modified")
    )
    chat_bottom.note_message(CHAT_ID, SCREEN_ID)

    user = await fresh_db.get_user(user_id)
    await workout._refresh_live(bot, state, user, workout_id, None, None)

    bot.delete_message.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    assert (await state.get_data())["live_message_id"] == SCREEN_ID


async def test_unhandled_message_below_the_screen_forces_a_resend(fresh_db, user_id):
    """A sticker nothing handles still pushes the screen up — the incoming
    middleware counts it before any filter gets a say."""
    workout_id = await fresh_db.create_workout(user_id)
    state = await _live_state(user_id, workout_id)
    bot = _make_bot()
    chat_bottom.note_message(CHAT_ID, SCREEN_ID)

    sticker = Message(
        message_id=SCREEN_ID + 1,
        date=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        chat=Chat(id=CHAT_ID, type="private"),
    )

    async def no_handler(event, data):
        return None

    await chat_bottom.TrackIncomingMessages()(no_handler, sticker, {})

    user = await fresh_db.get_user(user_id)
    await workout._refresh_live(bot, state, user, workout_id, None, None)

    bot.edit_message_text.assert_not_awaited()
    bot.send_message.assert_awaited_once()
