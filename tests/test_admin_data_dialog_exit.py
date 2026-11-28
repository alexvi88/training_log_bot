"""/data закрывается, когда админ ушёл: меню, другая кнопка, команда.

Владелец: «ушёл в меню, потом что-то набираю, а оказывается ещё открыт этот
режим». Middleware висит на диспетчере (handlers.admin.DropDataDialogOnExit).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

import config
import i18n
import keyboards
from fsm import AdminFlow
from handlers import admin

pytestmark = pytest.mark.asyncio

ADMIN = 4242


@pytest.fixture(autouse=True)
def _admin(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN)


async def _state(user_id: int = ADMIN) -> FSMContext:
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(AdminFlow.data_dialog)
    await state.update_data(admin_data_history=[{"role": "user", "content": "сколько новичков"}], workout_id=7)
    return state


def _message(text: str, user_id: int = ADMIN):
    msg = MagicMock(spec=Message)
    msg.from_user = SimpleNamespace(id=user_id)
    msg.text = text
    return msg


def _callback(data: str, user_id: int = ADMIN):
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = SimpleNamespace(id=user_id)
    cb.data = data
    return cb


async def _run(event, state):
    handler = AsyncMock()
    await admin.DropDataDialogOnExit()(handler, event, {"state": state})
    handler.assert_awaited_once()  # апдейт уходит дальше своему хендлеру


@pytest.mark.parametrize("event", [
    _callback("menu:main"),
    _message("/start"),
    _message("/metrics"),
])
async def test_leaving_closes_the_dialog_and_keeps_workout(event):
    state = await _state()
    await _run(event, state)
    assert await state.get_state() is None
    data = await state.get_data()
    assert data.get("admin_data_history") is None
    assert data.get("workout_id") == 7


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_persistent_menu_button_closes_the_dialog(lang):
    state = await _state()
    with i18n.use_lang(lang):
        text = keyboards.BTN_MENU.current()
    await _run(_message(text), state)
    assert await state.get_state() is None


@pytest.mark.parametrize("event", [
    _message("а теперь то же по iOS"),
    _message("/data"),
    _message("/data сколько атлетов"),
    _callback("admin:data:new"),
    _callback("admin:data:stop"),
])
async def test_dialog_itself_keeps_it_open(event):
    state = await _state()
    await _run(event, state)
    assert await state.get_state() == AdminFlow.data_dialog.state
    assert (await state.get_data()).get("admin_data_history")


async def test_not_admin_is_untouched():
    state = await _state(user_id=1)
    await _run(_message("/start", user_id=1), state)
    assert await state.get_state() == AdminFlow.data_dialog.state
