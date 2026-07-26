"""Editing a saved routine's exercise list: remove a single exercise (no
confirmation — trivially undone) and add one via the groups→exercises picker,
including a catalog template forked on the fly."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

from fsm import RoutineFlow
from handlers import routines

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str = ""):
    message = MagicMock()
    message.delete = AsyncMock()
    message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _make_message(user_id: int, text: str):
    msg = MagicMock()
    msg.from_user = SimpleNamespace(id=user_id, username="tester")
    msg.text = text
    msg.answer = AsyncMock()
    return msg


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


async def _make_routine(db, user_id: int, exercise_names: list[str]) -> tuple[int, int]:
    group_id = await db.create_muscle_group(user_id, "Грудь")
    routine_id = await db.create_routine(user_id, "Push day")
    for i, name in enumerate(exercise_names):
        ex_id = await db.create_exercise(user_id, name, group_id)
        await db.add_routine_exercise(routine_id, ex_id, i)
    return routine_id, group_id


# ---------- remove ----------


async def test_remove_drops_only_that_exercise(fresh_db, user_id):
    db = fresh_db
    routine_id, _ = await _make_routine(db, user_id, ["Жим лёжа", "Разведения"])
    entries = await db.list_routine_exercises(routine_id)
    target = next(e for e in entries if e["display_name"] == "Жим лёжа")
    state = await _make_state(user_id)

    await routines.rt_remove_exercise(_make_callback(user_id, f"rt:rmex:{routine_id}:{target['id']}"), state)

    remaining = await db.list_routine_exercises(routine_id)
    assert [r["display_name"] for r in remaining] == ["Разведения"]


async def test_remove_is_immediate_no_confirmation_step(fresh_db, user_id):
    """One tap, one DB write — unlike deleting the whole routine, which asks first."""
    db = fresh_db
    routine_id, _ = await _make_routine(db, user_id, ["Жим лёжа"])
    entries = await db.list_routine_exercises(routine_id)
    re_id = entries[0]["id"]
    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"rt:rmex:{routine_id}:{re_id}")

    await routines.rt_remove_exercise(callback, state)

    callback.answer.assert_awaited_once()
    assert await db.list_routine_exercises(routine_id) == []


async def test_remove_rejects_someone_elses_routine(fresh_db, user_id):
    db = fresh_db
    other_group = await db.create_muscle_group(999, "Грудь")
    other_ex = await db.create_exercise(999, "Жим", other_group)
    other_routine = await db.create_routine(999, "Not yours")
    await db.add_routine_exercise(other_routine, other_ex, 0)
    entries = await db.list_routine_exercises(other_routine)
    state = await _make_state(user_id)

    callback = _make_callback(user_id, f"rt:rmex:{other_routine}:{entries[0]['id']}")
    await routines.rt_remove_exercise(callback, state)

    callback.answer.assert_awaited_once_with("Программа не найдена", show_alert=True)
    assert await db.list_routine_exercises(other_routine) == entries  # untouched


# ---------- add: groups → exercises picker ----------


async def test_add_entry_sets_state_and_lists_groups(fresh_db, user_id):
    db = fresh_db
    routine_id, _ = await _make_routine(db, user_id, [])
    state = await _make_state(user_id)

    await routines.rt_add_exercise_start(_make_callback(user_id, f"rt:addex:{routine_id}"), state)

    assert await state.get_state() == RoutineFlow.adding_exercise_group
    assert (await state.get_data())["rtadd_routine_id"] == routine_id


async def test_picking_group_then_exercise_appends_it(fresh_db, user_id):
    db = fresh_db
    routine_id, group_id = await _make_routine(db, user_id, ["Жим лёжа"])
    ex_id = await db.create_exercise(user_id, "Отжимания", group_id)
    state = await _make_state(user_id)
    await state.update_data(rtadd_routine_id=routine_id)

    await routines.rtadd_pick_group(_make_callback(user_id, f"rtadd:grp:{group_id}"), state)
    assert await state.get_state() == RoutineFlow.adding_exercise_pick

    await routines.rtadd_pick_exercise(_make_callback(user_id, f"rtadd:ex:{ex_id}"), state)

    names = [r["display_name"] for r in await db.list_routine_exercises(routine_id)]
    assert names == ["Жим лёжа", "Отжимания"]  # appended after the existing one


async def test_adding_a_template_forks_it_first(fresh_db, user_id):
    db = fresh_db
    routine_id, _ = await _make_routine(db, user_id, [])
    template = await db._find_global_template_by_name("Жим штанги лёжа")
    assert template is not None, "seed template must exist for this test to mean anything"
    state = await _make_state(user_id)
    await state.update_data(rtadd_routine_id=routine_id)

    await routines.rtadd_pick_template(_make_callback(user_id, f"rtadd:tpladd:{template['id']}"), state)

    names = [r["display_name"] for r in await db.list_routine_exercises(routine_id)]
    assert names == ["Жим штанги лёжа"]
    # It's a real owned exercise now, not the template row itself.
    owned = await db.find_exercise_by_name(user_id, "Жим штанги лёжа")
    assert owned is not None and owned["user_id"] == user_id


async def test_search_text_offers_both_own_matches_and_templates(fresh_db, user_id):
    db = fresh_db
    routine_id, group_id = await _make_routine(db, user_id, [])
    await db.create_exercise(user_id, "Жим гантелей лёжа", group_id)
    state = await _make_state(user_id)
    await state.update_data(rtadd_routine_id=routine_id)
    await state.set_state(RoutineFlow.adding_exercise_group)

    message = _make_message(user_id, "жим")
    await routines.rtadd_search_text(message, state)

    kb = message.answer.await_args.kwargs["reply_markup"]
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert any("Жим гантелей лёжа" in t for t in texts)
    assert any(t.startswith("📋") for t in texts)  # a catalog template is offered too
    assert await state.get_state() == RoutineFlow.adding_exercise_pick


async def test_cancel_returns_to_routine_detail_without_changes(fresh_db, user_id):
    db = fresh_db
    routine_id, _ = await _make_routine(db, user_id, ["Жим лёжа"])
    state = await _make_state(user_id)
    await state.update_data(rtadd_routine_id=routine_id)
    await state.set_state(RoutineFlow.adding_exercise_group)

    await routines.rtadd_cancel(_make_callback(user_id, "rtadd:cancel"), state)

    names = [r["display_name"] for r in await db.list_routine_exercises(routine_id)]
    assert names == ["Жим лёжа"]  # unchanged
