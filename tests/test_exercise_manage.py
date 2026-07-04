from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import ExerciseManage
from handlers import exercises

pytestmark = pytest.mark.asyncio


def _make_message(user_id: int, text: str):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.text = text
    message.answer = AsyncMock()
    return message


async def _make_state(user_id: int, **extra_data) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(ExerciseManage.picking_exercise)
    await state.update_data(exm_group_id=None)
    if extra_data:
        await state.update_data(**extra_data)
    return state


async def test_typing_in_exercise_list_searches_instead_of_being_ignored(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Bench press", group_id)
    await db.create_exercise(user_id, "Triceps pushdown", group_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    message = _make_message(user_id, "bench")

    await exercises.exm_search_text(message, state)

    kb = message.answer.await_args.kwargs["reply_markup"]
    button_texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "Bench press" in button_texts
    assert not any("Triceps" in t for t in button_texts)
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "exm:newex" in callback_datas


async def test_typing_no_match_in_exercise_list_shows_empty_state(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Bench press", group_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    message = _make_message(user_id, "squat")

    await exercises.exm_search_text(message, state)

    sent_text = message.answer.await_args.args[0]
    assert "Ничего не нашлось" in sent_text
