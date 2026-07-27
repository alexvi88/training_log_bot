"""Settings screen: unit/formula toggles and the pushes on/off switch."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from handlers import settings

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


async def test_settings_pushes_toggles_off_then_on(fresh_db, user_id):
    db = fresh_db
    state = await _make_state(user_id)

    callback = _make_callback(user_id, "settings:pushes")
    await settings.settings_pushes(callback, state)
    assert (await db.get_user(user_id))["pushes_enabled"] == 0

    callback = _make_callback(user_id, "settings:pushes")
    await settings.settings_pushes(callback, state)
    assert (await db.get_user(user_id))["pushes_enabled"] == 1


async def test_show_settings_reflects_pushes_state_in_keyboard_labels(fresh_db, user_id):
    db = fresh_db
    state = await _make_state(user_id)

    callback = _make_callback(user_id, "menu:settings")
    await settings.show_settings(callback, state)
    sent_text = callback.message.answer.call_args.kwargs["reply_markup"]
    labels_on = [b.text for row in sent_text.inline_keyboard for b in row]
    assert any("включены" in label for label in labels_on)

    await db.update_user(user_id, pushes_enabled=0)
    callback = _make_callback(user_id, "menu:settings")
    await settings.show_settings(callback, state)
    sent_text = callback.message.answer.call_args.kwargs["reply_markup"]
    labels_off = [b.text for row in sent_text.inline_keyboard for b in row]
    assert any("выключены" in label for label in labels_off)


async def test_settings_progression_toggles_off_then_on(fresh_db, user_id):
    db = fresh_db
    state = await _make_state(user_id)
    assert (await db.get_user(user_id))["progression_hint_enabled"] == 1  # on by default

    callback = _make_callback(user_id, "settings:progression")
    await settings.settings_progression(callback, state)
    assert (await db.get_user(user_id))["progression_hint_enabled"] == 0

    callback = _make_callback(user_id, "settings:progression")
    await settings.settings_progression(callback, state)
    assert (await db.get_user(user_id))["progression_hint_enabled"] == 1


async def test_formula_switch_asks_before_rewriting_every_e1rm(fresh_db, user_id):
    """Switching the formula recomputes every e1RM, record and chart in the
    history — bigger than the unit switch, which already asked."""
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "settings:formula")

    await settings.settings_formula_confirm(callback, state)

    user = await fresh_db.get_user(user_id)
    assert user["e1rm_formula"] == "epley"  # unchanged — it only asked
    sent = callback.message.answer.await_args
    text = sent.args[0] if sent.args else sent.kwargs["text"]
    assert "brzycki" in text
    assert "пересчитаются" in text


async def test_confirming_the_formula_switch_applies_it(fresh_db, user_id):
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "settings:formulayes")

    await settings.settings_formula(callback, state)

    user = await fresh_db.get_user(user_id)
    assert user["e1rm_formula"] == "brzycki"


async def test_cancelling_the_formula_switch_changes_nothing(fresh_db, user_id):
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "settings:formulano")

    await settings.settings_formula_cancel(callback, state)

    user = await fresh_db.get_user(user_id)
    assert user["e1rm_formula"] == "epley"
