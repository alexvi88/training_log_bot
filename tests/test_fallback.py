"""Что происходит с вводом, который не подхватил ни один обработчик.

Про текст это было и раньше. Про кнопки — нет, и это была самая заметная поломка
в боте: больше сотни обработчиков стоят под `StateFilter`, после `state.clear()`
их callback не брал никто, а Telegram без `answer()` крутит спиннер секунд
десять и гасит его молча. Ни ответа, ни ошибки, ни строчки в логах.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery

from handlers import fallback


def _callback(user_id: int = 1, data: str = "pick:grp:7", *, inaccessible: bool = False):
    """Кнопка, нажатая на экране, который уже никем не обслуживается.

    `inaccessible=True` — то, что Telegram присылает для слишком старых или
    удалённых сообщений: `InaccessibleMessage`, у которого нет ни `text`, ни
    `answer` — только чат и id.
    """
    message = MagicMock(spec=["chat", "message_id"] if inaccessible else None)
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 10
    if not inaccessible:
        message.answer = AsyncMock(return_value=SimpleNamespace(message_id=11))
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.bot = MagicMock()
    callback.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=12))
    callback.bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=12))
    return callback


async def test_unhandled_text_gets_a_pointer_back_to_start():
    message = MagicMock()
    message.from_user = SimpleNamespace(id=1)
    message.text = "какая-то ерунда"
    message.reply = AsyncMock()

    await fallback.unhandled_text(message)

    message.reply.assert_awaited_once()
    assert "/start" in message.reply.await_args.args[0]


async def test_an_unhandled_button_answers_and_opens_the_menu(fresh_db, user_id):
    """Спиннер обязан погаснуть, и человек обязан оказаться там, откуда можно
    продолжить. «Экран устарел, нажми /start» — это работа, переложенная на него."""
    callback = _callback(user_id)
    state = MagicMock()
    state.get_data = AsyncMock(return_value={})
    state.clear = AsyncMock()
    state.update_data = AsyncMock()

    await fallback.unhandled_callback(callback, state)

    callback.answer.assert_awaited_once()
    callback.bot.send_message.assert_awaited()
    assert callback.bot.send_message.await_args.args[0] == user_id


async def test_a_button_from_an_inaccessible_message_does_not_crash(fresh_db, user_id):
    """Сообщение старше суток Telegram отдаёт как `InaccessibleMessage`: у него
    нет ни `.text`, ни `.answer`. Обращаться к ним — `AttributeError` вместо
    экрана, а это ровно тот случай, когда кнопку и нажимают."""
    callback = _callback(user_id, inaccessible=True)
    state = MagicMock()
    state.get_data = AsyncMock(return_value={})
    state.clear = AsyncMock()
    state.update_data = AsyncMock()

    await fallback.unhandled_callback(callback, state)

    callback.answer.assert_awaited_once()
    callback.bot.send_message.assert_awaited()
