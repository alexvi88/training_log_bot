"""Чат атлетов: вход появляется только вместе с адресом группы, ведёт прямо в
Telegram и не ломает раскладку главного меню."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import keyboards
import main
from handlers import community

CHAT_URL = "https://t.me/+abcdef"


def _make_message(user_id: int):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester")
    message.answer = AsyncMock()
    return message


# ---------- когда раздел вообще существует ----------


def test_community_hidden_without_url(monkeypatch):
    monkeypatch.setattr(config, "COMMUNITY_CHAT_URL", "")
    assert config.community_available() is False


def test_community_hidden_for_non_telegram_url(monkeypatch):
    """В переменную легко положить название чата или ссылку на сайт — кнопка с
    таким адресом либо не откроется, либо уведёт человека из Telegram."""
    for value in ("качалка", "https://example.com/chat", "t.me/mychat"):
        monkeypatch.setattr(config, "COMMUNITY_CHAT_URL", value)
        assert config.community_available() is False, value


def test_community_visible_for_telegram_invite(monkeypatch):
    monkeypatch.setattr(config, "COMMUNITY_CHAT_URL", CHAT_URL)
    assert config.community_available() is True


# ---------- кнопка в главном меню ----------


def _button_texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_main_menu_has_no_community_button_without_url():
    markup = keyboards.main_menu(has_active_workout=False)
    assert not any("Чат атлетов" in text for text in _button_texts(markup))


def test_main_menu_community_button_opens_the_group():
    markup = keyboards.main_menu(has_active_workout=False, community_url=CHAT_URL)
    (button,) = [b for row in markup.inline_keyboard for b in row if "Чат атлетов" in b.text]
    assert button.url == CHAT_URL
    assert button.callback_data is None


@pytest.mark.parametrize("show_quick_log", [False, True])
def test_community_button_sits_alone_on_the_last_row(show_quick_log):
    """Раскладка меню задаётся adjust(...) руками: лишняя кнопка без своей
    цифры съезжает в чужую пару и ломает соседний ряд."""
    markup = keyboards.main_menu(
        has_active_workout=False, show_quick_log=show_quick_log, community_url=CHAT_URL
    )
    without = keyboards.main_menu(has_active_workout=False, show_quick_log=show_quick_log)
    assert [b.text for b in markup.inline_keyboard[-1]] == ["💬 Чат атлетов"]
    assert markup.inline_keyboard[:-1] == without.inline_keyboard


# ---------- команда ----------


async def test_cmd_community_replies_with_link(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "COMMUNITY_CHAT_URL", CHAT_URL)
    message = _make_message(user_id)

    await community.cmd_community(message)

    (button,) = message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0]
    assert button.url == CHAT_URL
    assert "ЧАТ АТЛЕТОВ" in message.answer.await_args.args[0]


async def test_cmd_community_without_url_says_so(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "COMMUNITY_CHAT_URL", "")
    message = _make_message(user_id)

    await community.cmd_community(message)

    assert "пока нет" in message.answer.await_args.args[0]
    assert message.answer.await_args.kwargs.get("reply_markup") is None


def test_slash_menu_lists_community_only_when_it_leads_somewhere(monkeypatch):
    monkeypatch.setattr(config, "COMMUNITY_CHAT_URL", "")
    assert "community" not in [c.command for c in main._public_commands()]
    monkeypatch.setattr(config, "COMMUNITY_CHAT_URL", CHAT_URL)
    assert "community" in [c.command for c in main._public_commands()]
