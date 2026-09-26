"""Смена кг↔lb: то, что лежит вне таблиц весов, и повтор того же PATCH.

- FSM бота (кэши тренировки, припаркованные «555 кг? да/нет» и взвешивание,
  черновик тренера, кнопки «↩️ Отменить») пересчитывается и из кнопки бота,
  и из PATCH /v1/settings — для аккаунта, привязанного к Telegram;
- описание отката удалённого взвешивания (`bodyweight_restore`, в том числе
  внутри `batch`) пересчитывается и в FSM, и в таблице ai_undo_actions;
- повтор того же PATCH, прочитавший старую единицу до захвата флага, не
  пересчитывает историю второй раз.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import account_deletion
import api_v1
import api_v1_account
import config
import db
import fsm_unit_rescale
from fsm_storage import JSONFileStorage
from handlers import settings

LB = config.LB_PER_KG


def _fsm_data() -> dict:
    return {
        "last_by_exercise": {7: (100.0, 5)},
        "last_session_sets": {7: [(100.0, 5, None)]},
        "weight_steps": {7: 2.5},
        "confirmed_weights": {7: 555.0},
        "pending_weight_confirm": {
            "exercise_id": 7, "sets": [[555.0, 3, None], [None, 10, None]],
            "source": "text", "chat_id": 1, "message_id": 2, "prompt_message_id": 3,
        },
        "bw_pending_weight": 80.0,
        "ai_undo": {
            "u1": {"kind": "bodyweight_restore", "weight": 80.0, "logged_at": "2026-09-01T08:00:00"},
            "u2": {"kind": "batch", "items": [
                {"kind": "bodyweight", "id": 5},
                {"kind": "bodyweight_restore", "weight": 81.0, "logged_at": None},
            ]},
            "u3": {"kind": "food", "id": 9},
        },
    }


def _assert_rescaled(data: dict) -> None:
    assert data["last_by_exercise"][7][0] == pytest.approx(100 * LB)
    assert data["last_session_sets"][7][0][0] == pytest.approx(100 * LB)
    assert data["weight_steps"][7] == pytest.approx(2.5 * LB)
    assert data["confirmed_weights"][7] == pytest.approx(555 * LB)
    pending = data["pending_weight_confirm"]
    assert pending["sets"][0][0] == pytest.approx(555 * LB)
    assert pending["sets"][1][0] is None
    assert pending["prompt_message_id"] == 3
    assert data["bw_pending_weight"] == pytest.approx(80 * LB, abs=0.05)
    assert data["ai_undo"]["u1"]["weight"] == pytest.approx(80 * LB, abs=0.05)
    assert data["ai_undo"]["u2"]["items"][1]["weight"] == pytest.approx(81 * LB, abs=0.05)
    assert data["ai_undo"]["u2"]["items"][0] == {"kind": "bodyweight", "id": 5}
    assert data["ai_undo"]["u3"] == {"kind": "food", "id": 9}


def test_weight_cache_updates_covers_every_weight_in_fsm():
    data = _fsm_data()
    updates = fsm_unit_rescale.weight_cache_updates(data, LB)
    _assert_rescaled({**data, **updates})


def test_weight_cache_updates_leaves_empty_state_alone():
    assert fsm_unit_rescale.weight_cache_updates({}, LB) == {}
    assert fsm_unit_rescale.weight_cache_updates({"bw_pending_weight": None}, LB) == {}


# ---------- кнопка бота ----------


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


async def test_bot_unit_switch_rescales_pending_and_undo(fresh_db, user_id):
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )
    await state.update_data(**_fsm_data())
    await settings.settings_unit(_make_callback(user_id, "settings:unityes"), state)
    assert (await db.get_user(user_id))["unit"] == "lb"
    _assert_rescaled(await state.get_data())


# ---------- PATCH /v1/settings ----------


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


@pytest.fixture
def registered_storage(tmp_path):
    storage = JSONFileStorage(str(tmp_path / "fsm.json"))
    account_deletion.set_fsm_storage(storage)
    try:
        yield storage
    finally:
        account_deletion.set_fsm_storage(None)


async def test_app_unit_switch_rescales_bot_fsm(fresh_db, client_factory, registered_storage):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    key = StorageKey(bot_id=42, chat_id=user_id, user_id=user_id)
    await registered_storage.set_data(key, _fsm_data())
    other = StorageKey(bot_id=42, chat_id=222, user_id=222)
    await registered_storage.set_data(other, {"weight_steps": {1: 2.5}})

    resp = await client.patch("/settings", json={"unit": "lb"})
    assert resp.status_code == 200, resp.text

    _assert_rescaled(await registered_storage.get_data(key))
    # Чужое состояние не тронуто.
    assert (await registered_storage.get_data(other))["weight_steps"] == {1: 2.5}


async def test_app_unit_switch_without_storage_still_succeeds(fresh_db, client_factory):
    account_deletion.set_fsm_storage(None)
    client = await _linked_client(fresh_db, client_factory, telegram_id=111)
    resp = await client.patch("/settings", json={"unit": "lb"})
    assert resp.status_code == 200
    assert (await db.get_user(111))["unit"] == "lb"


async def test_storage_without_user_keys_is_skipped(fresh_db, user_id):
    assert await fsm_unit_rescale.rescale_user_state(user_id, LB, storage=MemoryStorage()) == 0


async def test_app_unit_switch_rescales_app_undo_actions(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    await db.add_ai_undo_actions(user_id, None, [
        ("↩️", {"kind": "bodyweight_restore", "weight": 80.0, "logged_at": None}),
        ("↩️", {"kind": "batch", "items": [
            {"kind": "bodyweight_restore", "weight": 70.0, "logged_at": None},
        ]}),
        ("↩️", {"kind": "food", "id": 1}),
    ])

    resp = await client.patch("/settings", json={"unit": "lb"})
    assert resp.status_code == 200

    cur = await db.conn().execute(
        "SELECT payload_json FROM ai_undo_actions WHERE telegram_id = ? ORDER BY id", (user_id,)
    )
    payloads = [json.loads(r["payload_json"]) for r in await cur.fetchall()]
    assert payloads[0]["weight"] == pytest.approx(80 * LB, abs=0.05)
    assert payloads[1]["items"][0]["weight"] == pytest.approx(70 * LB, abs=0.05)
    assert payloads[2] == {"kind": "food", "id": 1}


async def test_retried_unit_patch_does_not_rescale_twice(fresh_db, client_factory, monkeypatch):
    """Повтор того же PATCH прочитал пользователя (ещё kg) до `await
    _json_body`; пока он ждал тело, первый запрос успел переключить единицы
    и отпустить флаг. Под флагом единица перечитывается — второго пересчёта
    нет."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    await db.add_bodyweight_log(user_id, 80.0)

    real_json_body = api_v1_account._json_body

    async def _json_body_after_first_request_finished(request):
        body = await real_json_body(request)
        await api_v1_account._apply_unit_change(user_id, "lb")
        return body

    monkeypatch.setattr(api_v1_account, "_json_body", _json_body_after_first_request_finished)
    resp = await client.patch("/settings", json={"unit": "lb"})
    assert resp.status_code == 200

    logs = await db.list_bodyweight_logs(user_id)
    assert logs[0]["weight"] == pytest.approx(80 * LB, abs=0.05)
