"""The global dp.errors() handler — the last-resort net for anything a specific
handler didn't catch. Before this existed, an unhandled exception left a tapped
button's callback unanswered (Telegram spins it for ~10s, then gives up
silently) or a typed message with no reply at all.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Chat, ErrorEvent, InaccessibleMessage

import main

pytestmark = pytest.mark.asyncio

CHAT_ID = 4242


def _make_event(*, callback_query=None, message=None):
    update = SimpleNamespace(update_id=1, callback_query=callback_query, message=message)
    return ErrorEvent.model_construct(update=update, exception=RuntimeError("boom"))


def _make_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


def _make_message():
    message = MagicMock()
    message.chat = SimpleNamespace(id=CHAT_ID)
    return message


async def test_answers_the_stuck_callback_with_an_alert():
    callback_query = MagicMock()
    callback_query.answer = AsyncMock()
    callback_query.message = _make_message()
    event = _make_event(callback_query=callback_query)

    handled = await main.on_unhandled_error(event, _make_bot())

    assert handled is True
    callback_query.answer.assert_awaited_once()
    assert callback_query.answer.await_args.kwargs.get("show_alert") is True
    assert "пошло не так" in callback_query.answer.await_args.args[0]


async def test_writes_to_the_chat_of_a_message_update():
    message = _make_message()
    bot = _make_bot()
    event = _make_event(message=message)

    await main.on_unhandled_error(event, bot)

    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == CHAT_ID
    assert "пошло не так" in bot.send_message.await_args.args[1]


async def test_offers_a_way_back_to_the_menu():
    """Реюзаем готовый колбэк live:back_to_menu — человеку не обязательно знать
    про /start, если под сообщением есть кнопка."""
    message = _make_message()
    bot = _make_bot()
    event = _make_event(message=message)

    await main.on_unhandled_error(event, bot)

    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "live:back_to_menu"


async def test_offers_a_way_to_report_the_problem_when_feedback_is_configured(monkeypatch):
    """Вторая кнопка — тот же вход, что и «💬 Отзыв» в настройках
    (handlers.feedback.feedback_open, feedback:open) — и скрыта без ADMIN_ID."""
    import config

    monkeypatch.setattr(config, "ADMIN_ID", 424242)
    message = _make_message()
    bot = _make_bot()
    event = _make_event(message=message)

    await main.on_unhandled_error(event, bot)

    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[1][0].callback_data == "feedback:open"


async def test_hides_the_report_button_without_a_feedback_recipient(monkeypatch):
    import config

    monkeypatch.setattr(config, "ADMIN_ID", None)
    message = _make_message()
    bot = _make_bot()
    event = _make_event(message=message)

    await main.on_unhandled_error(event, bot)

    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert len(markup.inline_keyboard) == 1


async def test_writes_to_the_chat_even_when_the_screen_is_already_deleted():
    """ui.safe_edit удаляет старый экран прямо перед отправкой нового, так что
    «сообщения уже нет» — обычный расклад: reply в нём падает сам, и человек не
    видит вообще ничего. Пишем по chat_id."""
    message = _make_message()
    message.reply = AsyncMock(side_effect=RuntimeError("message to be replied not found"))
    bot = _make_bot()
    event = _make_event(message=message)

    handled = await main.on_unhandled_error(event, bot)

    assert handled is True
    message.reply.assert_not_awaited()
    bot.send_message.assert_awaited_once()


async def test_writes_to_the_chat_of_an_inaccessible_message():
    """У InaccessibleMessage нет reply вовсе — только chat."""
    callback_query = MagicMock()
    callback_query.answer = AsyncMock()
    callback_query.message = InaccessibleMessage(chat=Chat(id=CHAT_ID, type="private"), message_id=7, date=0)
    bot = _make_bot()

    handled = await main.on_unhandled_error(_make_event(callback_query=callback_query), bot)

    assert handled is True
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == CHAT_ID


async def test_a_failed_alert_still_leaves_a_message_in_the_chat():
    """Всплывашка могла устареть — сообщение в чате не должно от этого пропасть."""
    callback_query = MagicMock()
    callback_query.answer = AsyncMock(side_effect=RuntimeError("query is too old"))
    callback_query.message = _make_message()
    bot = _make_bot()

    await main.on_unhandled_error(_make_event(callback_query=callback_query), bot)

    bot.send_message.assert_awaited_once()


async def test_never_raises_even_if_notifying_the_user_also_fails():
    """A second Telegram error while trying to report the first must not escape —
    that would take down the whole update loop instead of just this one update."""
    callback_query = MagicMock()
    callback_query.answer = AsyncMock(side_effect=RuntimeError("also broken"))
    callback_query.message = _make_message()
    bot = _make_bot()
    bot.send_message = AsyncMock(side_effect=RuntimeError("also broken"))
    event = _make_event(callback_query=callback_query)

    handled = await main.on_unhandled_error(event, bot)

    assert handled is True


async def test_neither_callback_nor_message_is_a_noop():
    event = _make_event()
    assert await main.on_unhandled_error(event, _make_bot()) is True


async def test_survives_without_a_bot_in_the_context():
    """Обработчик ошибок не имеет права падать сам — даже если bot не пришёл."""
    event = _make_event(message=_make_message())
    assert await main.on_unhandled_error(event) is True
