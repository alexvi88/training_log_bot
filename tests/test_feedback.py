"""/feedback — free-form feedback (text, photos, whatever) relayed to the admin."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

import config
from fsm import FeedbackFlow
from handlers import feedback

pytestmark = pytest.mark.asyncio


def _make_message(user_id: int, username: str | None = "tester"):
    message = MagicMock(spec=Message)
    message.from_user = SimpleNamespace(id=user_id, username=username)
    message.bot = AsyncMock()
    message.answer = AsyncMock()
    message.reply = AsyncMock()
    message.copy_to = AsyncMock()
    return message


def _make_callback(user_id: int, data: str = ""):
    """Кнопка на экране отзыва. Меню после неё собирается по-настоящему
    (workout._show_main_menu), поэтому сообщению нужны все способы показать
    экран — правкой, текстом или картинкой."""
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 300
    message.text = "экран"
    message.photo = None
    message.edit_text = AsyncMock(return_value=SimpleNamespace(message_id=300))
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1))
    message.answer_photo = AsyncMock(
        return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=2)
    )
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.bot = AsyncMock()
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _screen_shown(callback) -> bool:
    return bool(
        callback.message.edit_text.await_count
        or callback.message.answer.await_count
        or callback.message.answer_photo.await_count
    )


async def _make_state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


async def test_feedback_command_prompts_and_sets_state(user_id):
    message = _make_message(user_id)
    state = await _make_state(user_id)

    await feedback.cmd_feedback(message, state)

    assert await state.get_state() == FeedbackFlow.awaiting_message.state
    message.answer.assert_awaited_once()


async def test_feedback_message_forwarded_to_admin(user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 999)
    message = _make_message(user_id, username="alex")
    message.text = "Всё сломалось!"
    state = await _make_state(user_id)
    await state.set_state(FeedbackFlow.awaiting_message)

    await feedback.feedback_message(message, state)

    message.bot.send_message.assert_awaited_once()
    args = message.bot.send_message.await_args.args
    assert args[0] == 999
    assert "@alex" in args[1]
    message.copy_to.assert_awaited_once_with(999)
    message.reply.assert_awaited_once()


async def test_feedback_message_without_admin_configured(user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    message = _make_message(user_id)
    state = await _make_state(user_id)
    await state.set_state(FeedbackFlow.awaiting_message)

    await feedback.feedback_message(message, state)

    message.copy_to.assert_not_awaited()
    message.reply.assert_awaited_once()


async def test_feedback_done_clears_state_and_returns_to_menu(fresh_db, user_id):
    """Состояние снимает сам workout._show_main_menu — и снимает бережно, сохраняя
    каркас незакрытой тренировки (см. tests/test_state_scaffold_navigation.py),
    поэтому здесь он работает по-настоящему, а не заглушкой."""
    callback = _make_callback(user_id, "feedback:done")
    state = await _make_state(user_id)
    await state.set_state(FeedbackFlow.awaiting_message)

    await feedback.feedback_done(callback, state)

    assert await state.get_state() is None
    assert _screen_shown(callback)
    callback.answer.assert_awaited_once()


async def test_feedback_prompt_offers_an_explicit_way_out(user_id):
    """Передумавшему нужен видимый выход: до этого на экране была одна кнопка
    «Готово», и что делать, если писать уже не хочется, было непонятно."""
    message = _make_message(user_id)
    state = await _make_state(user_id)

    await feedback.cmd_feedback(message, state)

    text, kwargs = message.answer.await_args.args[0], message.answer.await_args.kwargs
    buttons = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert "feedback:cancel" in buttons
    assert "feedback:done" in buttons
    assert "/start" in text  # и словами тоже: командой выйти можно откуда угодно


async def test_feedback_cancel_ends_the_flow(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 999)
    callback = _make_callback(user_id, "feedback:cancel")
    state = await _make_state(user_id)
    await state.set_state(FeedbackFlow.awaiting_message)

    await feedback.feedback_cancel(callback, state)

    assert await state.get_state() is None
    assert _screen_shown(callback)
    # Отмена ничего админу не отправляет — отзыва-то и не было.
    callback.bot.send_message.assert_not_awaited()


async def test_command_does_not_become_feedback_text(user_id, monkeypatch):
    """Команда в состоянии отзыва не проходит фильтр «ловлю всё» — апдейт уходит
    следующему роутеру, к настоящему хендлеру команды."""
    monkeypatch.setattr(config, "ADMIN_ID", 999)
    handler = next(
        h for h in feedback.router.message.handlers if h.callback is feedback.feedback_message
    )
    command = _make_message(user_id)
    command.text = "/start"
    plain = _make_message(user_id)
    plain.text = "кнопка веса не нажимается"
    photo = _make_message(user_id)
    photo.text = None  # фото без подписи — отзыв скриншотом, обычное дело
    raw_state = FeedbackFlow.awaiting_message.state

    assert not (await handler.check(command, raw_state=raw_state))[0]
    assert (await handler.check(plain, raw_state=raw_state))[0]
    assert (await handler.check(photo, raw_state=raw_state))[0]
