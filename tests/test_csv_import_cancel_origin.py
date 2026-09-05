"""«Отмена» импорта CSV обязана вернуть туда, откуда за ним зашли.

Раньше кнопка всегда вела в ⚙️ Настройки — даже если человек зашёл за
импортом из пустого главного меню (кнопка «📥 Перенести историю»), и «Отмена»
уводила его в раздел, которого он не открывал.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import handlers.csv_import as csv_import
from fsm import ImportFlow


async def _state(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


def _callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.text = "экран"
    message.photo = None
    message.edit_text = AsyncMock(return_value=True)
    message.answer = AsyncMock(
        return_value=SimpleNamespace(message_id=999, chat=SimpleNamespace(id=user_id))
    )
    message.answer_photo = AsyncMock(
        return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1000)
    )
    message.delete = AsyncMock()
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.answer = AsyncMock()
    callback.message = message
    callback.data = data
    return callback


@pytest.mark.asyncio
async def test_import_start_from_settings_remembers_settings_origin(fresh_db, user_id):
    state = await _state(user_id)
    callback = _callback(user_id, "settings:import")

    await csv_import.import_start(callback, state)

    assert (await state.get_data())["import_origin"] == "settings"
    assert await state.get_state() == ImportFlow.awaiting_file.state


@pytest.mark.asyncio
async def test_import_start_from_empty_menu_remembers_menu_origin(fresh_db, user_id):
    state = await _state(user_id)
    callback = _callback(user_id, "settings:import:menu")

    await csv_import.import_start(callback, state)

    assert (await state.get_data())["import_origin"] == "menu"


@pytest.mark.asyncio
async def test_import_cancel_from_settings_origin_opens_settings(fresh_db, user_id, monkeypatch):
    state = await _state(user_id)
    await state.set_state(ImportFlow.awaiting_file)
    await state.update_data(import_origin="settings")

    called = {}

    async def fake_show_settings(callback, state):
        called["screen"] = "settings"

    monkeypatch.setattr("handlers.settings.show_settings", fake_show_settings)

    callback = _callback(user_id, "imp:cancel")
    await csv_import.import_cancel(callback, state)

    assert called.get("screen") == "settings"


@pytest.mark.asyncio
async def test_import_cancel_from_menu_origin_opens_main_menu(fresh_db, user_id, monkeypatch):
    state = await _state(user_id)
    await state.set_state(ImportFlow.awaiting_file)
    await state.update_data(import_origin="menu")

    called = {}

    async def fake_show_main_menu(callback, state):
        called["screen"] = "menu"

    monkeypatch.setattr("handlers.workout._show_main_menu", fake_show_main_menu)

    callback = _callback(user_id, "imp:cancel")
    await csv_import.import_cancel(callback, state)

    assert called.get("screen") == "menu"


@pytest.mark.asyncio
async def test_import_cancel_defaults_to_settings_when_origin_missing(fresh_db, user_id, monkeypatch):
    """Обратная совместимость: если import_origin по какой-то причине не
    записан (например, состояние пережило деплой), «Отмена» ведёт в
    настройки — прежнее поведение, а не падение."""
    state = await _state(user_id)
    await state.set_state(ImportFlow.awaiting_file)

    called = {}

    async def fake_show_settings(callback, state):
        called["screen"] = "settings"

    monkeypatch.setattr("handlers.settings.show_settings", fake_show_settings)

    callback = _callback(user_id, "imp:cancel")
    await csv_import.import_cancel(callback, state)

    assert called.get("screen") == "settings"
