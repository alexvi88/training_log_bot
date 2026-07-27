"""CSV import's escape hatches: getting out of (or back through) the column
mapping, and not having to hand-resolve dozens of unknown exercise names."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

import keyboards
from fsm import ResolveFlow
from handlers import exercise_resolve


def _callback_datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _make_callback(user_id: int, data: str = ""):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 5
    message.text = "экран"
    message.photo = None
    message.delete = AsyncMock()
    message.edit_text = AsyncMock(return_value=True)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=6))
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


@pytest.fixture
def no_import_handoff(monkeypatch):
    """_next() hands control back to the importer once nothing is pending —
    stubbed so these tests stay about resolving, not about the import itself."""

    async def _noop(event, state):
        return None

    monkeypatch.setattr(exercise_resolve, "_dispatch_done", _noop)


# ---------- column mapping ----------


def test_column_mapping_keyboard_offers_a_way_out():
    """A mistapped column used to be unrecoverable — the only exit was /start."""
    kb = keyboards.csv_column_options_keyboard(["Date", "Weight"], prefix="impcol:date")
    cbs = _callback_datas(kb)
    assert "impcol:date:0" in cbs and "impcol:date:1" in cbs
    assert "imp:mapback" in cbs
    assert "imp:cancel" in cbs


# ---------- resolving unknown exercise names ----------


def test_resolve_keyboard_offers_bulk_create_only_when_more_remain():
    candidates = []
    with_more = keyboards.exercise_resolve_keyboard(candidates, "Barbell Row", "resolve", remaining=24)
    assert "resolve:createall" in _callback_datas(with_more)
    bulk = next(
        b for row in with_more.inline_keyboard for b in row if b.callback_data == "resolve:createall"
    )
    assert "25" in bulk.text  # this one plus the 24 still queued

    last_one = keyboards.exercise_resolve_keyboard(candidates, "Barbell Row", "resolve", remaining=0)
    assert "resolve:createall" not in _callback_datas(last_one)


async def test_resolve_screen_shows_position_in_the_queue(fresh_db, user_id):
    state = await _make_state(user_id)
    callback = _make_callback(user_id)

    await exercise_resolve.start(callback, state, ["Barbell Row", "Pendlay Row", "T-Bar Row"])

    sent = callback.message.answer.await_args
    text = sent.args[0] if sent.args else sent.kwargs["text"]
    assert "Упражнение 1 из 3" in text
    assert "Barbell Row" in text


async def test_create_all_resolves_every_remaining_name(fresh_db, user_id, no_import_handoff):
    db = fresh_db
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "resolve:createall")
    names = ["Barbell Row", "Pendlay Row", "T-Bar Row"]
    await state.update_data(
        resolve_pending=names, resolve_resolved={}, resolve_total=len(names),
        resolve_current_name=names[0],
    )
    await state.set_state(ResolveFlow.picking)

    await exercise_resolve.resolve_create_all(callback, state)

    data = await state.get_data()
    assert data["resolve_pending"] == []
    assert sorted(data["resolve_resolved"]) == sorted(names)
    created = {e["display_name"] for e in await db.list_user_exercises(user_id)}
    assert set(names).issubset(created)


async def test_create_all_puts_them_in_a_real_group(fresh_db, user_id, no_import_handoff):
    """They land somewhere sane and editable rather than group-less."""
    db = fresh_db
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "resolve:createall")
    await state.update_data(
        resolve_pending=["Barbell Row"], resolve_resolved={}, resolve_total=1,
        resolve_current_name="Barbell Row",
    )
    await state.set_state(ResolveFlow.picking)

    await exercise_resolve.resolve_create_all(callback, state)

    row = next(e for e in await db.list_user_exercises(user_id) if e["display_name"] == "Barbell Row")
    assert row["primary_group_id"] is not None
    group = await db.get_muscle_group(row["primary_group_id"])
    assert group["name"] == "Другое"
