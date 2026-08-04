"""Экран /mcp: выдача, перевыпуск и отзыв токена глазами пользователя.

Ключевое здесь не вёрстка, а две вещи: токен виден целиком (его надо копировать
в конфиг клиента, и обрезанный токен — это молча не работающее подключение) и
раздел вообще не показывается, когда бот развёрнут без публичного адреса.
"""

import json
import time
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


def _buttons(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_connector_path_is_offered_without_any_token():
    """Коннектор доступен сразу и всем: код связывания и инструкции под Claude с
    ChatGPT токена не требуют, и прятать их за «сначала выдай токен» значило бы
    закрыть единственный путь, где человек ничего не настраивает."""
    assert _buttons(keyboards.mcp_keyboard(False)) == [
        "mcp:code",
        "mcp:how:claude_web",
        "mcp:how:chatgpt",
        "mcp:how:claude_desktop",
        "mcp:issue",
        "menu:settings",
    ]


def test_token_guides_and_revoke_appear_only_with_a_token():
    """А вот инструкции для терминала без токена показывать нечего — в них
    нечего вставлять."""
    assert _buttons(keyboards.mcp_keyboard(True)) == [
        "mcp:code",
        "mcp:how:claude_web",
        "mcp:how:chatgpt",
        "mcp:how:claude_desktop",
        "mcp:how:claude_code",
        "mcp:how:cursor",
        "mcp:how:vscode",
        "mcp:how:other",
        "mcp:issue",
        "mcp:revoke",
        "menu:settings",
    ]


def test_connected_apps_appear_only_when_there_is_something_to_disconnect():
    assert "mcp:apps" not in _buttons(keyboards.mcp_keyboard(True, False))
    assert "mcp:apps" in _buttons(keyboards.mcp_keyboard(True, True))


def test_every_button_has_a_guide_behind_it():
    """Кнопка без инструкции — это тап в никуда, и заметен он только руками."""
    assert {kind for kind, _ in keyboards.MCP_CLIENTS} == set(mcp_access.GUIDES)


TOKEN_GUIDES = [kind for kind in mcp_access.GUIDES if kind not in mcp_access.OAUTH_GUIDES]


@pytest.mark.parametrize("kind", TOKEN_GUIDES)
async def test_guide_screen_carries_token_and_address(fresh_db, user_id, kind):
    token = await fresh_db.issue_mcp_token(user_id)
    callback = _callback(user_id, f"mcp:how:{kind}")
    await mcp_access.mcp_guide(callback, await _state(user_id))

    text = _sent_text(callback)
    assert token in text
    assert "https://training-log.example.com/mcp" in text
    # Инструкция без пути назад — тупик: экран с токеном уже уехал вверх.
    assert _buttons(callback.message.answer.call_args.kwargs["reply_markup"]) == ["mcp:open"]


@pytest.mark.parametrize("kind", sorted(mcp_access.OAUTH_GUIDES))
async def test_connector_guide_needs_no_token_at_all(fresh_db, user_id, kind):
    """Инструкции под коннектор открываются с нуля: человек, у которого токена
    нет и не будет, — это и есть их читатель."""
    callback = _callback(user_id, f"mcp:how:{kind}")
    await mcp_access.mcp_guide(callback, await _state(user_id))

    text = _sent_text(callback)
    assert "https://training-log.example.com/mcp" in text
    assert "Код для подключения" in text
    assert _buttons(callback.message.answer.call_args.kwargs["reply_markup"]) == ["mcp:open"]


@pytest.mark.parametrize("kind", list(mcp_access.GUIDES))
async def test_guide_fits_into_one_telegram_message(fresh_db, user_id, kind):
    """4096 символов — жёсткий лимит Telegram: инструкция длиннее не отправится
    вовсе, и вместо неё пользователь увидит ошибку."""
    token = await fresh_db.issue_mcp_token(user_id)
    assert len(mcp_access.GUIDES[kind][1](token)) < 4096


async def test_the_main_screen_fits_into_one_telegram_message(fresh_db, user_id):
    """Тот же лимит и у самого экрана: он вырос — вводная, адрес, список
    подключённых приложений и токен, — и упереться в 4096 стало реально."""
    await fresh_db.issue_mcp_token(user_id)
    for i in range(10):
        await _connect_app(fresh_db, user_id, f"client-{i}", f"Приложение номер {i}")
    msg = _message(user_id)
    await mcp_access.cmd_mcp(msg, await _state(user_id))

    assert len(msg.answer.call_args.args[0]) < 4096


async def test_token_guide_falls_back_to_the_main_screen_without_a_token(fresh_db, user_id):
    """Токен могли отозвать с другого устройства, пока экран висел открытым."""
    callback = _callback(user_id, "mcp:how:cursor")
    await mcp_access.mcp_guide(callback, await _state(user_id))

    assert "нужен токен" in _sent_text(callback)
    assert _buttons(callback.message.answer.call_args.kwargs["reply_markup"]) == _buttons(
        keyboards.mcp_keyboard(False)
    )


async def test_unknown_guide_does_not_crash(fresh_db, user_id):
    await fresh_db.issue_mcp_token(user_id)
    callback = _callback(user_id, "mcp:how:нет-такого")
    await mcp_access.mcp_guide(callback, await _state(user_id))
    assert _sent_text(callback)


# ---------- код для подключения ----------


async def test_the_link_code_screen_shows_six_digits_and_the_address(fresh_db, user_id):
    """Код и адрес — всё, что человек несёт на страницу согласия. Обрезанный код
    или адрес без /mcp — это молча не работающее подключение."""
    callback = _callback(user_id, "mcp:code")
    await mcp_access.mcp_code(callback, await _state(user_id))

    text = _sent_text(callback)
    cur = await fresh_db.conn().execute("SELECT code FROM oauth_link_codes WHERE user_id = ?", (user_id,))
    code = (await cur.fetchone())["code"]
    assert len(code) == 6
    assert code in text
    assert "https://training-log.example.com/mcp" in text
    assert "5 минут" in text


async def test_a_new_code_button_replaces_the_previous_code(fresh_db, user_id):
    """Код одноразовый и живёт минуты, поэтому «не успел» — обычный исход. Второй
    тап обязан выдать другой код, а не показать прежний."""
    first = _callback(user_id, "mcp:code")
    await mcp_access.mcp_code(first, await _state(user_id))
    second = _callback(user_id, "mcp:code")
    await mcp_access.mcp_code(second, await _state(user_id))

    cur = await fresh_db.conn().execute("SELECT code FROM oauth_link_codes WHERE user_id = ?", (user_id,))
    codes = [row["code"] for row in await cur.fetchall()]
    assert len(codes) == 1
    assert codes[0] in _sent_text(second)


async def test_the_code_is_refused_when_mcp_is_not_deployed(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    callback = _callback(user_id, "mcp:code")
    await mcp_access.mcp_code(callback, await _state(user_id))

    cur = await fresh_db.conn().execute("SELECT COUNT(*) AS n FROM oauth_link_codes")
    assert (await cur.fetchone())["n"] == 0
    assert callback.answer.call_args.kwargs.get("show_alert") is True


# ---------- подключённые приложения ----------


async def _connect_app(db, user_id: int, client_id: str, name: str) -> None:
    await db.save_oauth_client(client_id, None, json.dumps({"client_name": name}))
    await db.create_oauth_token(
        access_token=f"access-{client_id}",
        refresh_token=f"refresh-{client_id}",
        client_id=client_id,
        user_id=user_id,
        scopes="[]",
        resource=None,
        expires_at=time.time() + 3600,
        refresh_expires_at=time.time() + 3600,
    )


async def test_connected_apps_are_named_on_the_main_screen(fresh_db, user_id):
    """Человек должен видеть, что доступ у кого-то есть, не проваливаясь в
    подраздел: иначе забытый коннектор остаётся невидимым."""
    await _connect_app(fresh_db, user_id, "client-1", "Claude")
    msg = _message(user_id)
    await mcp_access.cmd_mcp(msg, await _state(user_id))

    assert "Claude" in msg.answer.call_args.args[0]
    assert "mcp:apps" in _buttons(msg.answer.call_args.kwargs["reply_markup"])


async def test_each_app_gets_its_own_disconnect_button(fresh_db, user_id):
    await _connect_app(fresh_db, user_id, "client-1", "Claude")
    await _connect_app(fresh_db, user_id, "client-2", "ChatGPT")
    callback = _callback(user_id, "mcp:apps")
    await mcp_access.mcp_apps(callback, await _state(user_id))

    text = _sent_text(callback)
    assert "Claude" in text and "ChatGPT" in text
    assert _buttons(callback.message.answer.call_args.kwargs["reply_markup"]) == [
        "mcp:off:client-1",
        "mcp:off:client-2",
        "mcp:open",
    ]


async def test_disconnect_kills_only_that_app(fresh_db, user_id):
    await _connect_app(fresh_db, user_id, "client-1", "Claude")
    await _connect_app(fresh_db, user_id, "client-2", "ChatGPT")
    callback = _callback(user_id, "mcp:off:client-1")
    await mcp_access.mcp_disconnect(callback, await _state(user_id))

    left = await fresh_db.list_oauth_connections(user_id)
    assert [row["client_id"] for row in left] == ["client-2"]
    # Остались подключения — человек остаётся в списке, а не улетает на главный.
    assert "ChatGPT" in _sent_text(callback)


async def test_disconnecting_the_last_app_returns_to_the_main_screen(fresh_db, user_id):
    await _connect_app(fresh_db, user_id, "client-1", "Claude")
    callback = _callback(user_id, "mcp:off:client-1")
    await mcp_access.mcp_disconnect(callback, await _state(user_id))

    assert await fresh_db.list_oauth_connections(user_id) == []
    assert "mcp:apps" not in _buttons(callback.message.answer.call_args.kwargs["reply_markup"])
    assert callback.answer.call_args.kwargs.get("show_alert") is True


async def test_disconnecting_someone_elses_app_does_nothing(fresh_db, user_id):
    """callback_data приходит от клиента и подставить в неё можно что угодно:
    отзыв обязан искать клиента среди своих подключений, а не верить кнопке."""
    other = await fresh_db.get_or_create_user(telegram_id=222, username="other")
    await _connect_app(fresh_db, other["telegram_id"], "client-9", "Claude")
    callback = _callback(user_id, "mcp:off:client-9")
    await mcp_access.mcp_disconnect(callback, await _state(user_id))

    assert len(await fresh_db.list_oauth_connections(other["telegram_id"])) == 1
