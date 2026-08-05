"""Persistent reply-keyboard buttons under the input field: 'Меню', 'Тренировка',
'AI-тренер'. They must always work, even mid-flow, and the keyboard itself should
stay in sync for every user via RefreshPersistentMenuMiddleware.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardMarkup

import keyboards
from fsm import AITrainerFlow, WorkoutFlow
from handlers import persistent_menu
from main import RefreshPersistentMenuMiddleware

pytestmark = pytest.mark.asyncio


def _make_message(user_id: int):
    message = MagicMock(spec=Message)
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


async def test_middleware_refreshes_keyboard_on_any_message_for_stale_users(fresh_db, user_id):
    await fresh_db.update_user(user_id, reply_keyboard_version=0)
    message = _make_message(user_id)
    message.text = "100 8"  # an ordinary set-logging message, not /start or a menu button
    handler = AsyncMock(return_value="handled")
    middleware = RefreshPersistentMenuMiddleware()

    result = await middleware(handler, message, {})

    assert result == "handled"
    handler.assert_awaited_once_with(message, {})
    refresh_call = message.answer.await_args
    assert isinstance(refresh_call.kwargs["reply_markup"], ReplyKeyboardMarkup)
    user = await fresh_db.get_user(user_id)
    assert user["reply_keyboard_version"] == keyboards.PERSISTENT_MENU_VERSION


async def test_middleware_is_a_noop_once_up_to_date(fresh_db, user_id):
    await fresh_db.update_user(user_id, reply_keyboard_version=keyboards.PERSISTENT_MENU_VERSION)
    message = _make_message(user_id)
    handler = AsyncMock(return_value="handled")
    middleware = RefreshPersistentMenuMiddleware()

    await middleware(handler, message, {})

    message.answer.assert_not_awaited()


async def test_middleware_skips_users_never_seen_before(fresh_db):
    message = _make_message(999999)
    handler = AsyncMock(return_value="handled")
    middleware = RefreshPersistentMenuMiddleware()

    await middleware(handler, message, {})

    message.answer.assert_not_awaited()


async def test_first_start_attaches_keyboard_without_the_update_notice(fresh_db):
    """Новичок на первом /start получает клавиатуру, но не сообщение «⌨️ Обновил
    меню под полем ввода»: обновлять ему нечего, а первый экран — единственный,
    который продаёт бота."""
    from handlers import workout

    message = _make_message(222222)
    carrier = AsyncMock()
    message.answer = AsyncMock(
        return_value=SimpleNamespace(
            message_id=999, chat=SimpleNamespace(id=222222), delete=carrier
        )
    )
    state = await _make_state(222222)

    await workout.cmd_start(message, state)

    texts = [call.args[0] for call in message.answer.await_args_list if call.args]
    assert not any("Обновил меню" in text for text in texts)
    keyboard_calls = [
        call for call in message.answer.await_args_list
        if isinstance(call.kwargs.get("reply_markup"), ReplyKeyboardMarkup)
    ]
    assert len(keyboard_calls) == 1
    carrier.assert_awaited_once()  # носитель клавиатуры сразу удалён
    user = await fresh_db.get_user(222222)
    assert user["reply_keyboard_version"] == keyboards.PERSISTENT_MENU_VERSION

    # Мидлварь идёт после хендлера и перечитывает строку — увидит актуальную
    # версию и промолчит.
    message.answer.reset_mock()
    await RefreshPersistentMenuMiddleware()(AsyncMock(), message, {})
    message.answer.assert_not_awaited()


async def test_second_start_does_not_resend_the_keyboard(fresh_db, user_id):
    """Уже заведённому юзеру носитель клавиатуры не шлётся заново — иначе
    каждое /start мигало бы лишним сообщением."""
    from handlers import workout

    message = _make_message(user_id)
    state = await _make_state(user_id)

    await workout.cmd_start(message, state)

    assert not [
        call for call in message.answer.await_args_list
        if isinstance(call.kwargs.get("reply_markup"), ReplyKeyboardMarkup)
    ]


async def test_menu_button_reuses_cmd_start(fresh_db, user_id):
    message = _make_message(user_id)
    message.text = keyboards.BTN_MENU
    state = await _make_state(user_id)
    await state.set_state(WorkoutFlow.logging_set)

    await persistent_menu.persistent_menu_button(message, state)

    assert await state.get_state() is None
    assert message.answer.await_count >= 1


async def test_workout_button_starts_immediately_and_interrupts_state(fresh_db, user_id):
    message = _make_message(user_id)
    message.text = keyboards.BTN_WORKOUT
    state = await _make_state(user_id)
    await state.set_state(WorkoutFlow.creating_exercise_name)

    await persistent_menu.persistent_workout_button(message, state)

    active = await fresh_db.get_active_workout(user_id)
    assert active is not None
    assert await state.get_state() == WorkoutFlow.picking_group.state
    assert message.delete.await_count == 1
    assert "Тренировка начата" in message.answer.await_args.args[0]


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
