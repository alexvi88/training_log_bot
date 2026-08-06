"""Sharing the workout as a picture (🖼 Картинка) necessarily replaces the text
card as the bottom-of-chat message — a photo can't carry the text card's
keyboard. It must not be a dead end: it gets a caption and a way back.

The picture is also the product's only viral surface, so it carries a referral
URL button (see acquisition.py) that keeps working after a forward."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

import acquisition
from handlers import history, sharing

pytestmark = pytest.mark.asyncio

BOT_USERNAME = "kachalka_bot"


@pytest.fixture(autouse=True)
def _bot_username_cache():
    """sharing caches the bot's username in a module global — reset it so the
    fake one from this module can't leak into (or arrive from) another test."""
    sharing._bot_username = None
    yield
    sharing._bot_username = None


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=501))
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    callback.bot = MagicMock()
    callback.bot.get_me = AsyncMock(return_value=SimpleNamespace(username=BOT_USERNAME))
    return callback


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


async def _finished_workout_with_set(db, user_id: int) -> int:
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 1, 0, 100.0, 5)
    await db.finish_workout(workout_id)
    return workout_id


async def test_hist_card_has_caption_and_back_button(fresh_db, user_id):
    db = fresh_db
    workout_id = await _finished_workout_with_set(db, user_id)
    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"hist:card:{workout_id}")

    await history.hist_card(callback, state)

    callback.message.answer_photo.assert_awaited_once()
    kwargs = callback.message.answer_photo.await_args.kwargs
    assert kwargs["caption"]
    kb = kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"hist:item:{workout_id}" in callback_datas


async def test_hist_card_carries_referral_link(fresh_db, user_id):
    """Картинку уносят в чужой чат, а callback-кнопки пересылку не переживают —
    приглашение обязано быть URL-кнопкой, и с id того, кто поделился."""
    db = fresh_db
    workout_id = await _finished_workout_with_set(db, user_id)
    callback = _make_callback(user_id, f"hist:card:{workout_id}")

    await history.hist_card(callback, await _make_state(user_id))

    kb = callback.message.answer_photo.await_args.kwargs["reply_markup"]
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    assert urls == [acquisition.referral_link(BOT_USERNAME, user_id)]
