"""The global dp.errors() handler — the last-resort net for anything a specific
handler didn't catch. Before this existed, an unhandled exception left a tapped
button's callback unanswered (Telegram spins it for ~10s, then gives up
silently) or a typed message with no reply at all.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import ErrorEvent

import main

pytestmark = pytest.mark.asyncio


def _make_event(*, callback_query=None, message=None):
    update = SimpleNamespace(update_id=1, callback_query=callback_query, message=message)
    return ErrorEvent.model_construct(update=update, exception=RuntimeError("boom"))


async def test_answers_the_stuck_callback_with_an_alert():
    callback_query = MagicMock()
    callback_query.answer = AsyncMock()
    event = _make_event(callback_query=callback_query)

    handled = await main.on_unhandled_error(event)

    assert handled is True
    callback_query.answer.assert_awaited_once()
    assert callback_query.answer.await_args.kwargs.get("show_alert") is True
    assert "пошло не так" in callback_query.answer.await_args.args[0]


async def test_replies_to_a_message_update():
    message = MagicMock()
    message.reply = AsyncMock()
    event = _make_event(message=message)

    await main.on_unhandled_error(event)

    message.reply.assert_awaited_once()
    assert "пошло не так" in message.reply.await_args.args[0]


async def test_never_raises_even_if_notifying_the_user_also_fails():
    """A second Telegram error while trying to report the first must not escape —
    that would take down the whole update loop instead of just this one update."""
    callback_query = MagicMock()
    callback_query.answer = AsyncMock(side_effect=RuntimeError("also broken"))
    event = _make_event(callback_query=callback_query)

    handled = await main.on_unhandled_error(event)

    assert handled is True


async def test_neither_callback_nor_message_is_a_noop():
    event = _make_event()
    assert await main.on_unhandled_error(event) is True
