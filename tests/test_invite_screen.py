"""«🤝 Пригласить» — отдельный вход к той же реферальной ссылке, что уже едет
под карточкой тренировки (acquisition.referral_link), но раньше её нельзя было
позвать нигде, кроме шаринга уже законченной тренировки.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

import acquisition
import keyboards
from handlers import history, sharing

BOT_USERNAME = "kachalka_bot"


@pytest.fixture(autouse=True)
def _bot_username_cache():
    sharing._bot_username = None
    yield
    sharing._bot_username = None


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.text = "экран"
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    callback.bot = MagicMock()
    callback.bot.get_me = AsyncMock(return_value=SimpleNamespace(username=BOT_USERNAME))
    return callback


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


async def test_invite_show_renders_the_link_in_a_pre_block(fresh_db, user_id):
    callback = _make_callback(user_id, "invite:show")

    await history.invite_show(callback, await _make_state(user_id))

    callback.message.answer.assert_awaited_once()
    text = callback.message.answer.await_args.args[0]
    link = acquisition.referral_link(BOT_USERNAME, user_id)
    assert f"<pre>{link}</pre>" in text
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "hist:menu"


def test_achievements_keyboard_offers_invite_button():
    """Кнопка стоит прямо в handlers.history.menu_achievements — тест собирает
    ту же клавиатуру руками, как экран, а не заново пишет её конструкцию."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    import i18n

    kb = InlineKeyboardBuilder()
    kb.button(text=i18n.t("history.ranks_button"), callback_data="rank:ladder")
    kb.button(text=i18n.t("btn.invite_friend"), callback_data="invite:show")
    kb.button(text=i18n.t("btn.back"), callback_data="hist:menu")
    kb.adjust(1)
    cbs = [b.callback_data for row in kb.as_markup().inline_keyboard for b in row]
    assert "invite:show" in cbs


def test_settings_keyboard_ends_with_invite_then_menu():
    kb = keyboards.settings_keyboard(
        unit="kg", formula="epley", pushes_enabled=True, ai_comments_enabled=True,
        progression_enabled=True,
    )
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert cbs[-2:] == ["invite:show", "settings:back"]
