"""Persistent reply-keyboard buttons under the input field: 'Меню', 'Тренировка',
'AI-тренер'. They must always work, even mid-flow, and the keyboard itself should
only be (re)sent once per user.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup

import keyboards
from fsm import AITrainerFlow, WorkoutFlow
from handlers import persistent_menu, workout

pytestmark = pytest.mark.asyncio


def _make_message(user_id: int):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester")
    message.bot = AsyncMock()
    message.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1000))
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=999, chat=SimpleNamespace(id=user_id)))
    message.delete = AsyncMock()
    return message


async def _make_state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


async def test_start_shows_persistent_keyboard_only_once(fresh_db, user_id):
    message = _make_message(user_id)
    state = await _make_state(user_id)

    await workout.cmd_start(message, state)

    onboarding_call = message.answer.await_args_list[1]
    assert isinstance(onboarding_call.kwargs["reply_markup"], ReplyKeyboardMarkup)
    user = await fresh_db.get_user(user_id)
    assert user["reply_keyboard_version"] == keyboards.PERSISTENT_MENU_VERSION

    message2 = _make_message(user_id)
    await workout.cmd_start(message2, state)
    assert message2.answer.await_count == 1


async def test_menu_version_bump_reshows_keyboard_for_existing_users(fresh_db, user_id):
    await fresh_db.update_user(user_id, reply_keyboard_version=1)

    message = _make_message(user_id)
    state = await _make_state(user_id)

    with patch("keyboards.PERSISTENT_MENU_VERSION", 2):
        await workout.cmd_start(message, state)

    onboarding_call = message.answer.await_args_list[1]
    assert isinstance(onboarding_call.kwargs["reply_markup"], ReplyKeyboardMarkup)
    user = await fresh_db.get_user(user_id)
    assert user["reply_keyboard_version"] == 2


async def test_menu_button_reuses_cmd_start(fresh_db, user_id):
    message = _make_message(user_id)
    message.text = keyboards.BTN_MENU
    state = await _make_state(user_id)
    await state.set_state(WorkoutFlow.logging_set)

    await persistent_menu.persistent_menu_button(message, state)

    assert await state.get_state() is None
    assert message.answer.await_count >= 1


async def test_workout_button_starts_new_workout_and_interrupts_state(fresh_db, user_id):
    message = _make_message(user_id)
    message.text = keyboards.BTN_WORKOUT
    state = await _make_state(user_id)
    await state.set_state(WorkoutFlow.creating_exercise_name)

    await persistent_menu.persistent_workout_button(message, state)

    active = await fresh_db.get_active_workout(user_id)
    assert active is not None
    assert message.delete.await_count == 1


async def test_workout_button_resumes_existing_workout(fresh_db, user_id):
    workout_id = await fresh_db.create_workout(user_id)

    message = _make_message(user_id)
    message.text = keyboards.BTN_WORKOUT
    state = await _make_state(user_id)

    await persistent_menu.persistent_workout_button(message, state)

    data = await state.get_data()
    assert data["workout_id"] == workout_id


async def test_ai_button_opens_ai_trainer_when_configured(fresh_db, user_id):
    message = _make_message(user_id)
    message.text = keyboards.BTN_AI
    state = await _make_state(user_id)
    await state.set_state(WorkoutFlow.logging_set)

    with patch("ai_trainer.is_configured", return_value=True):
        await persistent_menu.persistent_ai_button(message, state)

    assert await state.get_state() == AITrainerFlow.chatting.state
    message.answer.assert_awaited_once()


async def test_ai_button_warns_when_not_configured(fresh_db, user_id):
    message = _make_message(user_id)
    message.text = keyboards.BTN_AI
    state = await _make_state(user_id)

    with patch("ai_trainer.is_configured", return_value=False):
        await persistent_menu.persistent_ai_button(message, state)

    assert await state.get_state() is None
    assert "не настроен" in message.answer.await_args.args[0]


async def test_ai_trainer_command_reuses_same_flow(fresh_db, user_id):
    message = _make_message(user_id)
    message.text = "/ai_trainer"
    state = await _make_state(user_id)

    with patch("ai_trainer.is_configured", return_value=True):
        await persistent_menu.cmd_ai_trainer(message, state)

    assert await state.get_state() == AITrainerFlow.chatting.state
