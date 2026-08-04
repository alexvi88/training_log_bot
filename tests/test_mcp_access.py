"""Экран /mcp: выдача, перевыпуск и отзыв токена глазами пользователя.

Ключевое здесь не вёрстка, а две вещи: токен виден целиком (его надо копировать
в конфиг клиента, и обрезанный токен — это молча не работающее подключение) и
раздел вообще не показывается, когда бот развёрнут без публичного адреса.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

import config
import keyboards
from handlers import mcp_access

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def public_url(monkeypatch):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "https://training-log.example.com")
    monkeypatch.setattr(config, "MCP_ENABLED", True)


def _callback(user_id: int, data: str):
    # spec=CallbackQuery — обработчик различает «пришли командой» и «нажали
    # кнопку» через isinstance, и голый MagicMock проходил бы как сообщение.
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.text = "экран"
    message.edit_text = AsyncMock(return_value=SimpleNamespace(message_id=10))
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=11))
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _message(user_id: int):
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=user_id)
    msg.from_user = SimpleNamespace(id=user_id, username="tester")
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=12))
    return msg


async def _state(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


def _sent_text(mock_target) -> str:
    """Текст последнего показанного экрана — независимо от того, ушёл он новым
    сообщением или правкой прежнего."""
    for candidate in (mock_target.message.answer, mock_target.message.edit_text):
        if candidate.call_args is not None:
            return candidate.call_args.args[0]
    raise AssertionError("экран не показан")


async def test_command_shows_intro_without_issuing_a_token(fresh_db, user_id):
    """Токен не выдаётся самим фактом захода на экран: пока человек не нажал
    кнопку, наружу открывать нечего."""
    msg = _message(user_id)
    await mcp_access.cmd_mcp(msg, await _state(user_id))

    text = msg.answer.call_args.args[0]
    assert "MCP" in text
    assert await fresh_db.get_mcp_token(user_id) is None


async def test_issue_shows_the_whole_token_and_the_address(fresh_db, user_id):
    callback = _callback(user_id, "mcp:issue")
    await mcp_access.mcp_issue(callback, await _state(user_id))

    token = (await fresh_db.get_mcp_token(user_id))["token"]
    text = _sent_text(callback)
    assert token in text
    assert "https://training-log.example.com/mcp" in text


async def test_reissue_replaces_the_old_token_on_screen(fresh_db, user_id):
    callback = _callback(user_id, "mcp:issue")
    await mcp_access.mcp_issue(callback, await _state(user_id))
    first = (await fresh_db.get_mcp_token(user_id))["token"]

    again = _callback(user_id, "mcp:issue")
    await mcp_access.mcp_issue(again, await _state(user_id))
    second = (await fresh_db.get_mcp_token(user_id))["token"]

    assert second != first
    text = _sent_text(again)
    assert second in text
    assert first not in text
    # Про смерть прежнего токена человеку говорят явно — иначе он не поймёт,
    # почему настроенный вчера клиент вдруг отвалился.
    assert again.answer.call_args.kwargs.get("show_alert") is True


async def test_revoke_removes_the_token(fresh_db, user_id):
    await fresh_db.issue_mcp_token(user_id)
    callback = _callback(user_id, "mcp:revoke")
    await mcp_access.mcp_revoke(callback, await _state(user_id))

    assert await fresh_db.get_mcp_token(user_id) is None
    assert callback.answer.call_args.kwargs.get("show_alert") is True


async def test_screen_is_dead_end_when_mcp_is_not_deployed(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "")
    msg = _message(user_id)
    await mcp_access.cmd_mcp(msg, await _state(user_id))

    assert "выключено" in msg.answer.call_args.args[0]
    assert await fresh_db.get_mcp_token(user_id) is None


async def test_issue_refuses_when_mcp_is_not_deployed(fresh_db, user_id, monkeypatch):
    """Кнопки из старого экрана могут дожить до выключения фичи — токен по ним
    выдаваться не должен."""
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    callback = _callback(user_id, "mcp:issue")
    await mcp_access.mcp_issue(callback, await _state(user_id))

    assert await fresh_db.get_mcp_token(user_id) is None
    callback.answer.assert_awaited()


def test_settings_shows_the_entry_only_when_deployed():
    def buttons(show_mcp: bool) -> list[str]:
        kb = keyboards.settings_keyboard(
            "kg", "epley", True, False, True, show_mcp=show_mcp
        )
        return [b.callback_data for row in kb.inline_keyboard for b in row]

    assert "settings:mcp" in buttons(True)
    assert "settings:mcp" not in buttons(False)


def test_mcp_keyboard_offers_revoke_only_with_a_token():
    def buttons(has_token: bool) -> list[str]:
        kb = keyboards.mcp_keyboard(has_token)
        return [b.callback_data for row in kb.inline_keyboard for b in row]

    assert buttons(False) == ["mcp:issue", "menu:settings"]
    assert buttons(True) == ["mcp:issue", "mcp:revoke", "menu:settings"]
