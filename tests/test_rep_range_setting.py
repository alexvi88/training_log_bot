"""Диапазон повторов по умолчанию — личная настройка (users.rep_range_min/max).

Подсказка «Цель» без схемы программы тянется к верху выбранного диапазона;
схема программы и правило прогрессии по-прежнему старше. Настройка видна в
боте (handlers.settings) и в `/v1/settings`.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import analytics
import api_v1
import i18n
import keyboards
import progression_data
from handlers import settings

# ---------- расчёт ----------


def test_default_range_is_five_to_twelve_without_setting():
    assert analytics.user_rep_range({"rep_range_min": None, "rep_range_max": None}) == (5, 12)
    assert analytics.user_rep_range({}) == (5, 12)
    # Пара не из пресетов (ручная правка базы) — тоже 5–12, а не сломанная цель.
    assert analytics.user_rep_range({"rep_range_min": 7, "rep_range_max": 9}) == (5, 12)
    assert analytics.user_rep_range({"rep_range_min": 8, "rep_range_max": 15}) == (8, 15)


def test_user_range_moves_the_weight_bump_point():
    last = [(60.0, 12), (60.0, 12)]
    # По умолчанию 12 — верх: пора прибавлять вес.
    default = analytics.suggest_progression(last)
    assert default.action == "add_weight"
    # 8–15: до верха ещё три повтора — подсказка просит повтор, а не вес.
    wide = analytics.suggest_progression(last, default_range=(8, 15))
    assert wide.action == "add_reps"
    assert (wide.target_weight, wide.target_reps) == (60.0, 13)
    # 3–6: шесть — уже верх, а двенадцать и подавно.
    strength = analytics.suggest_progression([(100.0, 6)], default_range=(3, 6))
    assert strength.action == "add_weight"
    assert 3 <= strength.target_reps <= 6


def test_endurance_range_restarts_inside_itself():
    hint = analytics.suggest_progression([(40.0, 20)], default_range=(12, 20))
    assert hint.action == "add_weight"
    assert 12 <= hint.target_reps <= 20


def test_program_scheme_overrides_user_range():
    last = [(60.0, 12)]
    # Схема 3×6–12 — верх 12 взят, прибавка веса, хоть в настройках 8–15.
    hint = analytics.suggest_progression(last, planned_reps=(6, 12), default_range=(8, 15))
    assert hint.action == "add_weight"
    # Через progression_data — тот же итог из текста схемы на карточке.
    data = progression_data.hint(
        [(60.0, 12, None)], [], unit="kg", formula="epley",
        target="3×6–12", rep_range=(8, 15),
    )
    assert data["target_weight"] > 60.0


def test_progression_rule_overrides_user_range():
    rule = {"rule": "double_progression", "reps_top": 10}
    hint = analytics.suggest_progression([(60.0, 10)], rule=rule, default_range=(12, 20))
    assert hint.action == "add_weight"
    assert hint.target_reps <= 10


def test_progression_data_hint_uses_user_range():
    with i18n.use_lang("ru"):
        data = progression_data.hint(
            [(60.0, 12, None)], [], unit="kg", formula="epley", rep_range=(8, 15),
        )
    assert (data["target_weight"], data["target_reps"]) == (60.0, 13)


def test_live_tracker_hint_uses_user_range():
    from handlers.workout import _logging_hint

    with i18n.use_lang("ru"):
        text = _logging_hint(
            [(60.0, 12, None)], has_sets=True, show_instruction=False, rep_range=(8, 15),
        )
    assert "🎯 Цель: 60×13" in text


# ---------- бот: экран настроек ----------


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


def _buttons(callback):
    kb = callback.message.answer.call_args.kwargs["reply_markup"]
    return [b for row in kb.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_settings_row_shows_range_and_hides_when_hints_off(fresh_db, user_id):
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "menu:settings")
    await settings.show_settings(callback, state)
    labels = {b.callback_data: b.text for b in _buttons(callback)}
    assert labels["settings:rep_range"] == "🔢 Диапазон повторов: 5–12"

    await fresh_db.update_user(user_id, progression_hint_enabled=0)
    callback = _make_callback(user_id, "menu:settings")
    await settings.show_settings(callback, state)
    assert "settings:rep_range" not in [b.callback_data for b in _buttons(callback)]


@pytest.mark.asyncio
async def test_bot_picker_flow_saves_preset(fresh_db, user_id):
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "settings:rep_range")
    await settings.settings_rep_range(callback, state)
    text = callback.message.answer.call_args.args[0]
    assert "не дошёл до верха — подскажу повтор, дошёл — вес" in text
    buttons = _buttons(callback)
    assert [b.text for b in buttons] == [
        "3–6 · сила", "✓ 5–12 · по умолчанию", "8–12", "8–15", "12–20 · выносливость", "⬅️ Назад",
    ]

    callback = _make_callback(user_id, "settings:rep_range:8:15")
    await settings.settings_rep_range_set(callback, state)
    user = await fresh_db.get_user(user_id)
    assert (user["rep_range_min"], user["rep_range_max"]) == (8, 15)
    callback.answer.assert_awaited_with("Поставил 8–15", show_alert=False)
    labels = {b.callback_data: b.text for b in _buttons(callback)}
    assert labels["settings:rep_range"] == "🔢 Диапазон повторов: 8–15"


@pytest.mark.asyncio
async def test_bot_picker_ignores_unknown_range(fresh_db, user_id):
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "settings:rep_range:7:9")
    await settings.settings_rep_range_set(callback, state)
    user = await fresh_db.get_user(user_id)
    assert user["rep_range_min"] is None


@pytest.mark.asyncio
async def test_picker_speaks_english():
    with i18n.use_lang("en"):
        kb = keyboards.rep_range_keyboard((12, 20))
        texts = [b.text for row in kb.inline_keyboard for b in row]
        assert "✓ 12–20 · endurance" in texts
        assert "3–6 · strength" in texts
        main = keyboards.settings_keyboard("kg", "epley", True, True, True, rep_range=(3, 6))
        labels = [b.text for row in main.inline_keyboard for b in row]
        assert "🔢 Rep range: 3–6" in labels


# ---------- REST /v1/settings ----------


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


@pytest.mark.asyncio
async def test_api_get_returns_default_range(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    body = (await client.get("/settings")).json()
    assert (body["rep_range_min"], body["rep_range_max"]) == (5, 12)


@pytest.mark.asyncio
async def test_api_patch_sets_range(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.patch("/settings", json={"rep_range_min": 12, "rep_range_max": 20})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["rep_range_min"], resp.json()["rep_range_max"]) == (12, 20)
    user = await fresh_db.get_user(111)
    assert (user["rep_range_min"], user["rep_range_max"]) == (12, 20)
    again = (await client.get("/settings")).json()
    assert (again["rep_range_min"], again["rep_range_max"]) == (12, 20)


@pytest.mark.parametrize(
    "payload",
    [
        {"rep_range_min": 7, "rep_range_max": 9},
        {"rep_range_min": 8},
        {"rep_range_max": 12},
        {"rep_range_min": "8", "rep_range_max": "12"},
        {"rep_range_min": True, "rep_range_max": 12},
        {"rep_range_min": 12, "rep_range_max": 8},
    ],
)
@pytest.mark.asyncio
async def test_api_patch_rejects_non_presets(fresh_db, client_factory, payload):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.patch("/settings", json={**payload, "lang": "en"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "bad_request"
    assert "3–6, 5–12, 8–12, 8–15" in body["message"]
    user = await fresh_db.get_user(111)
    # Ни одно поле не записалось — даже язык из того же тела.
    assert user["rep_range_min"] is None
    assert user["lang"] == "ru"
