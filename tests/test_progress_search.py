"""Typing a name while browsing Progress searches the user's own exercises
instead of falling through to the fallback router's "Не понял" — mirrors the
search already available in the workout picker and ⚙️ Упражнения."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

from fsm import ProgressFlow
from handlers import history

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str = ""):
    message = MagicMock()
    message.delete = AsyncMock()
    message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _make_message(user_id: int, text: str):
    msg = MagicMock()
    msg.from_user = SimpleNamespace(id=user_id, username="tester")
    msg.text = text
    msg.answer = AsyncMock()
    return msg


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


async def test_entering_progress_sets_a_searchable_state(fresh_db, user_id):
    state = await _make_state(user_id)
    await history.show_progress_entry(_make_callback(user_id), state)
    assert await state.get_state() == ProgressFlow.picking_group


async def test_picking_a_group_sets_a_searchable_state(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    state = await _make_state(user_id)

    await history._render_progress_exercise_list(_make_callback(user_id), state, str(group_id), page=0)

    assert await state.get_state() == ProgressFlow.picking_exercise


async def test_typing_a_name_finds_the_exercise(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим штанги лёжа", group_id)
    await db.create_exercise(user_id, "Присед", group_id)
    state = await _make_state(user_id)
    await state.set_state(ProgressFlow.picking_group)

    message = _make_message(user_id, "жим")
    await history.prog_search_text(message, state)

    message.answer.assert_awaited_once()
    kb = message.answer.await_args.kwargs["reply_markup"]
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"prog:ex:{ex_id}:all" in callbacks
    assert await state.get_state() == ProgressFlow.picking_exercise


async def test_no_match_says_so_instead_of_crashing(fresh_db, user_id):
    state = await _make_state(user_id)
    await state.set_state(ProgressFlow.picking_exercise)
    message = _make_message(user_id, "нонсенс")

    await history.prog_search_text(message, state)

    text = message.answer.await_args.args[0]
    assert "Ничего не нашлось" in text


async def test_blank_text_is_ignored(fresh_db, user_id):
    state = await _make_state(user_id)
    await state.set_state(ProgressFlow.picking_group)
    message = _make_message(user_id, "   ")

    await history.prog_search_text(message, state)

    message.answer.assert_not_awaited()
