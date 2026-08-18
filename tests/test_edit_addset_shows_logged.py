"""Экран «➕ Добавить подход» при правке тренировки показывает уже записанное."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from handlers import edit_workout

pytestmark = pytest.mark.asyncio


def _callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.text = "экран правки"
    message.edit_text = AsyncMock(return_value=message)
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=message)
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    callback.bot = AsyncMock()
    return callback


def _screen_text(callback) -> str:
    """Текст экрана, каким бы путём ui.safe_edit его ни поставил: правкой
    сообщения, ответом в чат или новым сообщением от бота."""
    for call in (
        callback.message.edit_text.await_args,
        callback.message.answer.await_args,
        callback.bot.send_message.await_args,
    ):
        if call is not None:
            return call.args[-1] if len(call.args) > 1 else call.args[0]
    raise AssertionError("экран не отправлен ни одним из путей")


async def test_prompt_lists_the_sets_already_logged_for_this_exercise(fresh_db, user_id):
    """Экран обещает взять вес «с прошлого подхода», а самого подхода не
    показывал: чтобы вспомнить, с чего продолжаешь, приходилось отменять ввод,
    смотреть список и заходить заново."""
    group_id = await fresh_db.create_muscle_group(user_id, "Грудь")
    ex_id = await fresh_db.create_exercise(user_id, "Жим лёжа", group_id)
    other_id = await fresh_db.create_exercise(user_id, "Присед", group_id)
    workout_id = await fresh_db.create_workout(user_id)
    block_id = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_set(block_id, ex_id, 1, 0, 100.0, 8)
    await fresh_db.add_set(block_id, ex_id, 2, 0, 100.0, 6)
    await fresh_db.add_set(block_id, other_id, 3, 0, 60.0, 12)

    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )
    callback = _callback(user_id, f"editw:addset:{block_id}:{ex_id}")

    await edit_workout.editw_addset_prompt(callback, state)

    text = _screen_text(callback)
    assert "100×8, 100×6" in text
    # Чужие подходы из того же блока сюда не лезут.
    assert "60×12" not in text
    assert "Жим лёжа" in text


async def test_prompt_without_logged_sets_says_nothing_extra(fresh_db, user_id):
    group_id = await fresh_db.create_muscle_group(user_id, "Грудь")
    ex_id = await fresh_db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await fresh_db.create_workout(user_id)
    block_id = await fresh_db.create_block(workout_id, "single")

    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )
    callback = _callback(user_id, f"editw:addset:{block_id}:{ex_id}")

    await edit_workout.editw_addset_prompt(callback, state)

    text = _screen_text(callback)
    assert "Уже записал" not in text
