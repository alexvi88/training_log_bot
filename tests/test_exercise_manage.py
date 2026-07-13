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


def _make_exercise_callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=500))
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


async def test_viewing_second_exercise_deletes_first_exercises_images(fresh_db, user_id):
    """_send_exercise_images (handlers/exercises.py) used to leave every
    previously sent media group in the chat as the user paged through
    exercise detail screens. Viewing a new exercise must clean up the
    previous one's photos instead of piling them up.
    """
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим гантелей лёжа", group_id)
    press = await db.create_exercise(user_id, "Жим ногами", group_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    callback1 = _make_exercise_callback(user_id, f"exm:ex:{bench}")
    await exercises.exm_pick_exercise(callback1, state)

    assert callback1.message.answer_media_group.await_count == 1
    assert callback1.bot.delete_message.await_count == 0

    callback2 = _make_exercise_callback(user_id, f"exm:ex:{press}")
    callback2.message.bot = callback1.message.bot  # same chat/bot across taps
    callback2.bot = callback1.bot
    await exercises.exm_pick_exercise(callback2, state)

    # The first exercise's two photo messages must be deleted before the
    # second exercise's photos are sent.
    assert callback1.bot.delete_message.await_count == 2
    deleted_ids = {c.args[1] for c in callback1.bot.delete_message.await_args_list}
    assert deleted_ids == {1, 2}
    assert callback2.message.answer_media_group.await_count == 1


async def test_returning_to_exercise_list_clears_pending_images(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим гантелей лёжа", group_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    callback = _make_exercise_callback(user_id, f"exm:ex:{bench}")
    await exercises.exm_pick_exercise(callback, state)
    assert callback.bot.delete_message.await_count == 0

    back_callback = _make_exercise_callback(user_id, "exm:backlist")
    back_callback.message.bot = callback.message.bot
    back_callback.bot = callback.bot
    await exercises.exm_back_to_list(back_callback, state)

    assert callback.bot.delete_message.await_count == 2
