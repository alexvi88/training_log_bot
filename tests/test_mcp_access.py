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


async def test_command_shows_intro_without_issuing_anything(fresh_db, user_id):
    """Ни токен, ни код не выдаются самим фактом захода на экран: пока человек не
    выбрал приложение, наружу открывать нечего."""
    msg = _message(user_id)
    await mcp_access.cmd_mcp(msg, await _state(user_id))

    assert "https://training-log.example.com/mcp" in msg.answer.call_args.args[0]
    assert await fresh_db.get_mcp_token(user_id) is None
    cur = await fresh_db.conn().execute("SELECT COUNT(*) AS n FROM oauth_link_codes")
    assert (await cur.fetchone())["n"] == 0


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


def _rows(kb) -> list[list[str]]:
    return [[b.callback_data for b in row] for row in kb.inline_keyboard]


def test_connector_path_is_offered_without_any_token():
    """Коннектор доступен сразу и всем: код связывания и инструкции под Claude с
    ChatGPT токена не требуют, и прятать их за «сначала выдай токен» значило бы
    закрыть единственный путь, где человек ничего не настраивает."""
    assert _buttons(keyboards.mcp_keyboard(False)) == [
        "mcp:how:claude",
        "mcp:how:chatgpt",
        # Claude Code — тоже коннектором: он умеет OAuth сам, токен ему нужен
        # только там, где браузер открыть некому.
        "mcp:how:claude_code",
        # Код — после инструкций: он живёт минуты и нужен по ходу подключения,
        # а взятый до него успевает истечь.
        "mcp:code",
        "mcp:issue",
        "menu:settings",
    ]


def test_only_revoking_needs_a_token():
    """Инструкции доступны все и всегда: токена не требует ни одна, включая
    терминальную. От токена зависит только то, что с ним можно сделать."""
    assert "mcp:revoke" in _buttons(keyboards.mcp_keyboard(True))
    assert "mcp:revoke" not in _buttons(keyboards.mcp_keyboard(False))
    assert "mcp:how:claude_code" in _buttons(keyboards.mcp_keyboard(False))


def test_the_screen_is_grouped_into_rows_not_one_long_column():
    """Девять кнопок в одну колонку — простыня, в которой глазу не за что
    зацепиться. Группы очевидны: клиенты, код с приложениями, токен."""
    rows = _rows(keyboards.mcp_keyboard(True, True))

    assert rows[0] == ["mcp:how:claude", "mcp:how:chatgpt"]
    assert rows[1] == ["mcp:how:claude_code"]
    assert rows[2] == ["mcp:code"]
    assert rows[3] == ["mcp:apps"]
    assert rows[4] == ["mcp:issue", "mcp:revoke"]
    assert rows[-1] == ["menu:settings"]
    # Ни один ряд не длиннее двух: три кнопки в ряд Telegram сжимает до
    # нечитаемых обрубков подписей.
    assert max(len(row) for row in rows) == 2


def test_connected_apps_appear_only_when_there_is_something_to_disconnect():
    assert "mcp:apps" not in _buttons(keyboards.mcp_keyboard(True, False))
    assert "mcp:apps" in _buttons(keyboards.mcp_keyboard(True, True))


def test_every_button_has_a_guide_behind_it():
    """Кнопка без инструкции — это тап в никуда, и заметен он только руками."""
    assert {kind for kind, _ in keyboards.MCP_CLIENTS} == set(mcp_access.GUIDES)


async def test_the_terminal_guide_still_offers_the_token_when_there_is_one(fresh_db, user_id):
    """Токен в хвосте инструкции — для скриптов и облачных сессий, где браузер
    открыть некому. Есть токен — показываем команду целиком, нет — говорим, где
    его взять, и на сам коннектор это не влияет."""
    token = await fresh_db.issue_mcp_token(user_id)
    callback = _callback(user_id, "mcp:how:claude_code")
    await mcp_access.mcp_guide(callback, await _state(user_id))

    text = _sent_text(callback)
    assert token in text
    assert "https://training-log.example.com/mcp" in text
    assert _buttons(callback.message.answer.call_args.kwargs["reply_markup"]) == [
        "mcp:how:claude_code:new",
        "mcp:open",
    ]


@pytest.mark.parametrize("kind", sorted(mcp_access.OAUTH_GUIDES))
async def test_connector_guide_carries_the_code_on_the_same_screen(fresh_db, user_id, kind):
    """Главное про эти экраны: инструкция самодостаточна. Адрес и код лежат в том
    же сообщении, где шаги, — уходить за ними на другой экран и возвращаться
    значит терять код, который живёт минуты, ровно в середине подключения.

    Токена при этом не требуется: человек, у которого его нет и не будет, — это и
    есть читатель этих инструкций.
    """
    callback = _callback(user_id, f"mcp:how:{kind}")
    await mcp_access.mcp_guide(callback, await _state(user_id))

    text = _sent_text(callback)
    cur = await fresh_db.conn().execute(
        "SELECT code FROM oauth_link_codes WHERE user_id = ?", (user_id,)
    )
    code = (await cur.fetchone())["code"]
    assert code in text
    assert "https://training-log.example.com/mcp" in text
    # «Новый код» перерисовывает эту же инструкцию, а не уводит на третий экран.
    assert _buttons(callback.message.answer.call_args.kwargs["reply_markup"]) == [
        f"mcp:how:{kind}:new",
        "mcp:open",
    ]


@pytest.mark.parametrize("kind", sorted(mcp_access.OAUTH_GUIDES))
async def test_reopening_a_guide_keeps_the_code_already_copied(fresh_db, user_id, kind):
    """Человек скопировал код, вставил его в браузере и вернулся в бота
    перечитать шаг — и код от этого умирать не должен.

    Именно так и ломалось: каждое открытие инструкции выдавало новый, а тот, что
    уже лежал в поле на странице подтверждения, становился мёртвым.
    """
    first = _callback(user_id, f"mcp:how:{kind}")
    await mcp_access.mcp_guide(first, await _state(user_id))
    cur = await fresh_db.conn().execute("SELECT code FROM oauth_link_codes")
    code = (await cur.fetchone())["code"]

    second = _callback(user_id, f"mcp:how:{kind}")
    await mcp_access.mcp_guide(second, await _state(user_id))

    assert code in _sent_text(second)
    cur = await fresh_db.conn().execute("SELECT code FROM oauth_link_codes")
    assert [row["code"] for row in await cur.fetchall()] == [code]


@pytest.mark.parametrize("kind", sorted(mcp_access.OAUTH_GUIDES))
async def test_the_new_code_button_does_rotate_it(fresh_db, user_id, kind):
    """А явная кнопка — меняет: «код истёк» лечится на том же экране."""
    first = _callback(user_id, f"mcp:how:{kind}")
    await mcp_access.mcp_guide(first, await _state(user_id))
    cur = await fresh_db.conn().execute("SELECT code FROM oauth_link_codes")
    old = (await cur.fetchone())["code"]

    rotated = _callback(user_id, f"mcp:how:{kind}:new")
    await mcp_access.mcp_guide(rotated, await _state(user_id))

    text = _sent_text(rotated)
    cur = await fresh_db.conn().execute("SELECT code FROM oauth_link_codes")
    codes = [row["code"] for row in await cur.fetchall()]
    assert codes != [old]
    assert len(codes) == 1
    assert codes[0] in text


@pytest.mark.parametrize("kind", list(mcp_access.GUIDES))
async def test_guide_fits_into_one_telegram_message(fresh_db, user_id, kind):
    """4096 символов — жёсткий лимит Telegram: инструкция длиннее не отправится
    вовсе, и вместо неё пользователь увидит ошибку."""
    token = await fresh_db.issue_mcp_token(user_id)
    assert len(mcp_access.GUIDES[kind][1](token, "123456")) < 4096


async def test_the_main_screen_fits_into_one_telegram_message(fresh_db, user_id):
    """Тот же лимит и у самого экрана: он вырос — вводная, адрес, список
    подключённых приложений и токен, — и упереться в 4096 стало реально."""
    await fresh_db.issue_mcp_token(user_id)
    for i in range(10):
        await _connect_app(fresh_db, user_id, f"client-{i}", f"Приложение номер {i}")
    msg = _message(user_id)
    await mcp_access.cmd_mcp(msg, await _state(user_id))

    assert len(msg.answer.call_args.args[0]) < 4096


async def test_the_terminal_guide_works_without_a_token_too(fresh_db, user_id):
    """Claude Code подключается коннектором: `claude mcp add` без заголовка,
    дальше `/mcp` → Authenticate и та же страница согласия. Токен упоминается
    только там, где браузер открыть некому — в скриптах и облачных сессиях."""
    callback = _callback(user_id, "mcp:how:claude_code")
    await mcp_access.mcp_guide(callback, await _state(user_id))

    text = _sent_text(callback)
    assert "Authenticate" in text
    assert "--header" not in text
    cur = await fresh_db.conn().execute(
        "SELECT code FROM oauth_link_codes WHERE user_id = ?", (user_id,)
    )
    assert (await cur.fetchone())["code"] in text


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
    # Срок берётся из константы, а не вписан числом: разъехавшийся с кодом текст
    # обманывает человека ровно в тот момент, когда он ждёт «ещё успею».
    assert mcp_access._code_ttl() in text


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


async def test_dates_are_shown_in_the_users_own_hours(fresh_db, user_id):
    """В базе время серверное (UTC), а сверяет его человек со своими часами: без
    сдвига «последнее обращение» показывалось бы на несколько часов в прошлом, и
    выглядело бы это как «приложение не ходило за данными»."""
    await fresh_db.update_user(user_id, tz_offset=3)
    await _connect_app(fresh_db, user_id, "client-1", "Claude")
    await fresh_db.conn().execute(
        "UPDATE oauth_tokens SET connected_at = ?, last_used_at = ? WHERE client_id = ?",
        ("2026-08-04T10:00:00", "2026-08-04T12:30:00", "client-1"),
    )
    await fresh_db.conn().commit()

    callback = _callback(user_id, "mcp:apps")
    await mcp_access.mcp_apps(callback, await _state(user_id))

    text = _sent_text(callback)
    assert "04.08.2026 13:00" in text  # 10:00 UTC + 3
    assert "04.08.2026 15:30" in text


async def test_disconnecting_someone_elses_app_does_nothing(fresh_db, user_id):
    """callback_data приходит от клиента и подставить в неё можно что угодно:
    отзыв обязан искать клиента среди своих подключений, а не верить кнопке."""
    other = await fresh_db.get_or_create_user(telegram_id=222, username="other")
    await _connect_app(fresh_db, other["telegram_id"], "client-9", "Claude")
    callback = _callback(user_id, "mcp:off:client-9")
    await mcp_access.mcp_disconnect(callback, await _state(user_id))

    assert len(await fresh_db.list_oauth_connections(other["telegram_id"])) == 1
