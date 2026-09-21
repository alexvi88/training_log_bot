"""Bot (handlers/settings.py) and iOS REST API (api_v1_account.py) each guard
their own kg<->lb switch against a *second tap on the same surface* with a
module-level `_converting` set — but the two sets are separate objects, so
neither guard sees the other. The same account (Telegram bot + linked iOS app)
switching units on both surfaces at once rescales the whole history twice.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import api_v1
import db
from fsm import SettingsFlow
from handlers import settings

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.text = "🔧 Настройки:"
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=MemoryStorage(), key=key)
    await state.set_state(SettingsFlow.menu)
    return state


async def test_bot_and_api_unit_switch_at_once_do_not_double_scale(fresh_db):
    user_id = 111
    await fresh_db.get_or_create_user(telegram_id=user_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(user_id, ttl_seconds=600, digits=8)

    transport = httpx.ASGITransport(app=api_v1.build_app())
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"

    ex_id = await db.create_exercise(user_id, "Жим лёжа", None)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 0, 0, 100.0, 5, None)

    state = await _make_state(user_id)
    callback = _make_callback(user_id, "settings:unityes")

    # Одновременно: атлет жмёт "Переключить на lb" в боте и в тот же момент
    # тем же нажатием в iOS-приложении (или приложение уже стояло на экране
    # настроек и отправило то же PATCH). Гонка — не двойной тап по одной
    # кнопке (это уже прикрыто своим _converting в каждом модуле), а два
    # НЕЗАВИСИМЫХ модуля, каждый со своим множеством в памяти процесса.
    await asyncio.gather(
        settings.settings_unit(callback, state),
        client.patch("/settings", json={"unit": "lb"}),
    )

    sets = await db.list_sets_for_block(block_id)
    assert len(sets) == 1
    # 100 kg -> lb ровно один раз это ~220.5 lb. Если гонка отработала, оба
    # модуля успели прочитать unit="kg" и оба умножили вес на тот же
    # коэффициент — получится ~486 lb (в квадрате).
    assert sets[0]["weight"] == pytest.approx(100.0 * db_conf_factor(), abs=0.5)

    user = await db.get_user(user_id)
    assert user["unit"] == "lb"


def db_conf_factor():
    import config
    return config.LB_PER_KG
