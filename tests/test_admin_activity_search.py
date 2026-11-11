"""/activity: поиск атлета по id, @нику, части ника/имени — аргументом команды
и кнопкой «🔎 Найти атлета» на списке (handlers/admin.py)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

import activity_log
import config
from fsm import AdminFlow
from handlers import admin

pytestmark = pytest.mark.asyncio

ADMIN_ID = 999
BOT_ID = 1


@pytest.fixture(autouse=True)
def _admin(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)


def _message(text: str = "", user_id: int = ADMIN_ID):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="admin", language_code=None)
    message.chat = SimpleNamespace(id=user_id)
    message.text = text
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=7))
    return message


def _callback(data: str, user_id: int = ADMIN_ID):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.text = None
    message.edit_text = AsyncMock(return_value=message)
    message.delete = AsyncMock()
    message.answer = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="admin", language_code=None)
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _state(user_id: int = ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=BOT_ID, chat_id=user_id, user_id=user_id))


async def _athlete(db, telegram_id: int, username=None, first_name=None, action="жим 80 8"):
    await db.get_or_create_user(telegram_id=telegram_id, username=username)
    if first_name:
        await db.conn().execute("UPDATE users SET first_name = ? WHERE telegram_id = ?", (first_name, telegram_id))
        await db.conn().commit()
    await db.log_user_event(telegram_id, activity_log.KIND_AI_REPLY, action)
    return telegram_id


def _texts(mock) -> list[str]:
    return [c.args[0] for c in mock.await_args_list]


def _buttons(markup) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


async def test_arg_by_nick_opens_feed(fresh_db):
    await _athlete(fresh_db, 222, "alpha", action="лента альфы")
    await _athlete(fresh_db, 333, "beta", action="лента беты")
    message = _message()
    await admin.cmd_activity(message, _state(), SimpleNamespace(args="@alpha"))
    text = "\n".join(_texts(message.answer))
    assert "лента альфы" in text
    assert "лента беты" not in text


async def test_arg_by_id_and_partial_name(fresh_db):
    await _athlete(fresh_db, 222, "alpha", action="лента альфы")
    await _athlete(fresh_db, 444, None, first_name="Гоша", action="лента гоши")
    message = _message()
    await admin.cmd_activity(message, _state(), SimpleNamespace(args="222"))
    assert "лента альфы" in "\n".join(_texts(message.answer))

    message = _message()
    await admin.cmd_activity(message, _state(), SimpleNamespace(args="гош"))
    assert "лента гоши" in "\n".join(_texts(message.answer))


async def test_several_matches_give_buttons_to_feeds(fresh_db):
    await _athlete(fresh_db, 222, "ivan_one")
    await _athlete(fresh_db, 333, "ivan_two")
    message = _message()
    await admin.cmd_activity(message, _state(), SimpleNamespace(args="ivan"))
    buttons = _buttons(message.answer.await_args.kwargs["reply_markup"])
    assert {cb for _, cb in buttons} >= {"admin:acu:222", "admin:acu:333"}


async def test_not_found_says_what_to_do(fresh_db):
    await _athlete(fresh_db, 222, "alpha")
    message = _message()
    await admin.cmd_activity(message, _state(), SimpleNamespace(args="zzz"))
    assert "Не нашёл атлета «zzz»" in message.answer.await_args.args[0]


async def test_button_flow_and_cancel(fresh_db):
    await _athlete(fresh_db, 222, "alpha", action="лента альфы")
    state = _state()
    message = _message()
    await admin.cmd_activity(message, state, SimpleNamespace(args=None))
    assert ("🔎 Найти атлета", "admin:acq") in _buttons(message.answer.await_args.kwargs["reply_markup"])

    callback = _callback("admin:acq")
    await admin.admin_activity_search_start(callback, state)
    assert await state.get_state() == AdminFlow.activity_search.state
    assert _buttons(callback.message.answer.await_args.kwargs["reply_markup"]) == [("Отмена", "admin:acp:0")]

    reply = _message("alph")
    await admin.admin_activity_search_receive(reply, state)
    assert "лента альфы" in "\n".join(_texts(reply.answer))
    assert await state.get_state() == AdminFlow.browsing_activity.state

    await admin.admin_activity_search_start(_callback("admin:acq"), state)
    cancel = _callback("admin:acp:0")
    await admin.admin_activity_users_page(cancel, state)
    assert await state.get_state() == AdminFlow.browsing_activity_users.state


async def test_non_admin_blocked(fresh_db):
    await _athlete(fresh_db, 222, "alpha")
    state = _state(555)
    message = _message(user_id=555)
    await admin.cmd_activity(message, state, SimpleNamespace(args="@alpha"))
    message.answer.assert_not_awaited()

    callback = _callback("admin:acq", user_id=555)
    await admin.admin_activity_search_start(callback, state)
    assert await state.get_state() is None

    await state.set_state(AdminFlow.activity_search)
    reply = _message("alpha", user_id=555)
    await admin.admin_activity_search_receive(reply, state)
    reply.answer.assert_not_awaited()
