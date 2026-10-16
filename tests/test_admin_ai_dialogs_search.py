"""/ai_dialogs: поиск атлета по id, @нику, части ника/имени/почты — аргументом
команды и кнопкой «🔎 Найти атлета» на списке (handlers/admin.py)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

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
    message.answer = AsyncMock()
    return message


def _callback(data: str, user_id: int = ADMIN_ID):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.text = None  # экран без текста — safe_edit удаляет и шлёт новый через answer
    message.edit_text = AsyncMock(return_value=message)
    message.delete = AsyncMock()
    message.answer = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)  # _show_ai_users_list различает по isinstance
    callback.from_user = SimpleNamespace(id=user_id, username="admin", language_code=None)
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _state(user_id: int = ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=BOT_ID, chat_id=user_id, user_id=user_id))


async def _athlete(db, telegram_id: int, username=None, first_name=None, question="как жать?"):
    await db.get_or_create_user(telegram_id=telegram_id, username=username)
    if first_name:
        await db.conn().execute("UPDATE users SET first_name = ? WHERE telegram_id = ?", (first_name, telegram_id))
        await db.conn().commit()
    if question:
        await db.add_ai_chat_message(telegram_id, "user", question)
        await db.add_ai_chat_message(telegram_id, "assistant", f"ответ для {telegram_id}")
    return telegram_id


def _texts(mock) -> list[str]:
    return [c.args[0] for c in mock.await_args_list]


def _buttons(markup) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


async def test_arg_by_id_opens_dialogs(fresh_db):
    uid = await _athlete(fresh_db, 222, "alpha", question="вопрос альфы")
    await _athlete(fresh_db, 333, "beta", question="вопрос беты")
    message = _message()

    await admin.cmd_ai_dialogs(message, _state(), SimpleNamespace(args=str(uid)))

    text = "\n".join(_texts(message.answer))
    assert "вопрос альфы" in text and "@alpha" in text
    assert "вопрос беты" not in text
    assert _buttons(message.answer.await_args.kwargs["reply_markup"])[0][1] == "admin:aib:0"


async def test_arg_by_username_is_case_and_at_insensitive(fresh_db):
    await _athlete(fresh_db, 222, "Alpha", question="вопрос альфы")
    message = _message()

    await admin.cmd_ai_dialogs(message, _state(), SimpleNamespace(args="@ALPHA"))

    assert "вопрос альфы" in "\n".join(_texts(message.answer))


async def test_app_only_user_by_negative_id(fresh_db):
    await _athlete(fresh_db, -5001, None, question="вопрос из приложения")
    message = _message()

    await admin.cmd_ai_dialogs(message, _state(), SimpleNamespace(args="-5001"))

    assert "вопрос из приложения" in "\n".join(_texts(message.answer))


async def test_single_partial_match_by_first_name_opens_directly(fresh_db):
    await _athlete(fresh_db, 222, None, first_name="Сергей", question="вопрос сергея")
    await _athlete(fresh_db, 333, "beta", first_name="Иван")
    message = _message()

    await admin.cmd_ai_dialogs(message, _state(), SimpleNamespace(args="серг"))

    assert "вопрос сергея" in "\n".join(_texts(message.answer))


async def test_several_partial_matches_show_pick_list(fresh_db):
    await _athlete(fresh_db, 222, "lifter_one")
    await _athlete(fresh_db, 333, "lifter_two", first_name="Пётр")
    await _athlete(fresh_db, 444, "runner")
    message = _message()

    await admin.cmd_ai_dialogs(message, _state(), SimpleNamespace(args="LIFTER"))

    message.answer.assert_awaited_once()
    assert "подходят 2" in message.answer.await_args.args[0]
    buttons = _buttons(message.answer.await_args.kwargs["reply_markup"])
    picks = [cb for _, cb in buttons if cb.startswith("admin:aiu:")]
    assert sorted(picks) == ["admin:aiu:222", "admin:aiu:333"]
    assert any("Пётр" in label for label, _ in buttons)
    assert buttons[-1][1] == "admin:aib:0"

    # Кнопка из списка открывает тот же экран диалогов.
    callback = _callback("admin:aiu:333")
    await admin.admin_ai_dialogs_show(callback, _state())
    assert "ответ для 333" in "\n".join(_texts(callback.message.answer))


async def test_not_found(fresh_db):
    await _athlete(fresh_db, 222, "alpha")
    message = _message()

    await admin.cmd_ai_dialogs(message, _state(), SimpleNamespace(args="@nobody"))

    text = message.answer.await_args.args[0]
    assert text.startswith("Не нашёл атлета") and "@nobody" in text
    assert "reply_markup" not in message.answer.await_args.kwargs


async def test_found_without_dialogs_says_so(fresh_db):
    await _athlete(fresh_db, 222, "quiet", question=None)
    message = _message()

    await admin.cmd_ai_dialogs(message, _state(), SimpleNamespace(args="@quiet"))

    assert "пока нет диалогов" in message.answer.await_args.args[0]


async def test_button_flow(fresh_db):
    await _athlete(fresh_db, 222, "alpha", question="вопрос альфы")
    state = _state()
    message = _message()
    await admin.cmd_ai_dialogs(message, state, SimpleNamespace(args=None))
    list_kb = message.answer.await_args.kwargs["reply_markup"]
    assert ("🔎 Найти атлета", "admin:ais") in _buttons(list_kb)
    assert any(cb == "admin:aiu:222" for _, cb in _buttons(list_kb))  # список на месте

    callback = _callback("admin:ais")
    await admin.admin_ai_dialogs_search_start(callback, state)
    assert await state.get_state() == AdminFlow.ai_dialogs_search.state
    prompt_kb = callback.message.answer.await_args.kwargs["reply_markup"]
    assert _buttons(prompt_kb) == [("Отмена", "admin:ais:no:0")]

    reply = _message("alph")
    await admin.admin_ai_dialogs_search_receive(reply, state)
    assert "вопрос альфы" in "\n".join(_texts(reply.answer))
    assert await state.get_state() == AdminFlow.browsing_ai_users.state


async def test_button_cancel_returns_to_list(fresh_db):
    await _athlete(fresh_db, 222, "alpha")
    state = _state()
    await admin.admin_ai_dialogs_search_start(_callback("admin:ais"), state)

    callback = _callback("admin:ais:no:0")
    await admin.admin_ai_dialogs_search_cancel(callback, state)

    assert await state.get_state() == AdminFlow.browsing_ai_users.state
    assert "выбери пользователя" in callback.message.answer.await_args.args[0]


async def test_non_admin_blocked(fresh_db):
    await _athlete(fresh_db, 222, "alpha", question="вопрос альфы")
    state = _state(555)

    message = _message(user_id=555)
    await admin.cmd_ai_dialogs(message, state, SimpleNamespace(args="@alpha"))
    message.answer.assert_not_awaited()

    callback = _callback("admin:ais", user_id=555)
    await admin.admin_ai_dialogs_search_start(callback, state)
    assert await state.get_state() is None
    callback.message.answer.assert_not_awaited()

    await state.set_state(AdminFlow.ai_dialogs_search)
    reply = _message("alpha", user_id=555)
    await admin.admin_ai_dialogs_search_receive(reply, state)
    reply.answer.assert_not_awaited()
