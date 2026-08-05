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


async def test_settings_food_macros_toggles_off_then_on(fresh_db, user_id):
    db = fresh_db
    state = await _make_state(user_id)

    assert (await db.get_user(user_id))["food_macros_enabled"] == 1  # по умолчанию считаем

    callback = _make_callback(user_id, "settings:food_macros")
    await settings.settings_food_macros(callback, state)
    assert (await db.get_user(user_id))["food_macros_enabled"] == 0

    callback = _make_callback(user_id, "settings:food_macros")
    await settings.settings_food_macros(callback, state)
    assert (await db.get_user(user_id))["food_macros_enabled"] == 1


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


# ---------- «🧬 Обо мне»: что AI-тренер записал с твоих слов ----------


async def test_profile_screen_shows_what_the_trainer_wrote(fresh_db, user_id):
    """Поля профиля пишет тренер, не дожидаясь просьбы (см.
    ai_trainer.save_athlete_profile) — до этого экрана их нельзя было ни
    увидеть, ни поправить, при том что от них зависит подбор программ."""
    import ai_trainer
    import ui

    await ai_trainer.execute_tool(
        user_id, "save_athlete_profile",
        {"goal": "масса", "days_per_week": 4, "equipment": ["штанга", "гантели"]},
    )

    seen = {}

    async def fake_edit(callback, text, **kwargs):
        seen["text"] = text
        return MagicMock()

    original, ui.safe_edit = ui.safe_edit, fake_edit
    try:
        await settings.settings_profile(_make_callback(user_id, "settings:profile"), await _make_state(user_id))
    finally:
        ui.safe_edit = original

    assert "масса" in seen["text"]
    assert "4" in seen["text"]
    # Оборудование лежит JSON-строкой, а показываться должно по-человечески.
    assert "штанга, гантели" in seen["text"]
    assert '["' not in seen["text"]


async def test_profile_screen_marks_what_is_still_unknown(fresh_db, user_id):
    """Прочерк, а не пропущенная строка: половина ценности экрана в том, чтобы
    видеть, чего тренер про тебя ещё не знает."""
    import ui

    seen = {}

    async def fake_edit(callback, text, **kwargs):
        seen["text"] = text
        return MagicMock()

    original, ui.safe_edit = ui.safe_edit, fake_edit
    try:
        await settings.settings_profile(_make_callback(user_id, "settings:profile"), await _make_state(user_id))
    finally:
        ui.safe_edit = original

    # Считаем прочерки-значения, а не все тире в тексте — во вводной фразе
    # экрана своё.
    empty = [line for line in seen["text"].split("\n") if line.endswith("</b> —")]
    assert len(empty) == 5
    assert "Ограничения" in seen["text"]


async def test_profile_can_be_cleared(fresh_db, user_id):
    import ai_trainer
    import ui

    await ai_trainer.execute_tool(user_id, "save_athlete_profile", {"limitations": "болит плечо"})

    async def fake_edit(callback, text, **kwargs):
        return MagicMock()

    original, ui.safe_edit = ui.safe_edit, fake_edit
    try:
        await settings.settings_profile_clear(
            _make_callback(user_id, "settings:profileclear"), await _make_state(user_id)
        )
    finally:
        ui.safe_edit = original

    assert (await fresh_db.get_user(user_id))["limitations"] is None


async def test_profile_button_is_on_the_settings_screen():
    import keyboards

    kb = keyboards.settings_keyboard("kg", "epley", True, True, True)
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]

    assert "settings:profile" in callbacks
