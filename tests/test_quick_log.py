"""Typing a whole past session as one line.

A first-time user has an empty diary and a training history behind them —
walking the picker to enter the first record is the slowest possible start.
This flow trades precision for speed: one line in, a finished workout out.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import timeutil
from fsm import WorkoutFlow
from handlers import workout
from parser import ParseError, parse_quick_workout

pytestmark = pytest.mark.asyncio


def _make_message(user_id: int, text: str):
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=user_id)
    msg.message_id = 5
    msg.from_user = SimpleNamespace(id=user_id, username="tester")
    msg.text = text
    msg.reply = AsyncMock()
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=6))
    msg.bot = MagicMock()
    return msg


async def _state(user_id: int) -> FSMContext:
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )
    await state.set_state(WorkoutFlow.quick_log)
    return state


# ---------- parsing ----------


async def test_parses_several_exercises_with_their_sets():
    entries = parse_quick_workout("жим 80x8x3, присед 100 5")
    assert [e.name for e in entries] == ["жим", "присед"]
    assert [(s.weight, s.reps) for s in entries[0].sets] == [(80.0, 8)] * 3
    assert [(s.weight, s.reps) for s in entries[1].sets] == [(100.0, 5)]


async def test_a_nameless_chunk_continues_the_previous_exercise():
    """"жим 80x8, 75x8" is one exercise with two sets, not a nameless second."""
    entries = parse_quick_workout("жим 80x8, 75x8")
    assert len(entries) == 1
    assert [(s.weight, s.reps) for s in entries[0].sets] == [(80.0, 8), (75.0, 8)]


async def test_a_name_containing_digits_is_not_split_on_them():
    entries = parse_quick_workout("жим 45 градусов 60x10")
    assert entries[0].name == "жим 45 градусов"
    assert [(s.weight, s.reps) for s in entries[0].sets] == [(60.0, 10)]


async def test_bodyweight_sets_need_no_weight():
    entries = parse_quick_workout("подтягивания 12")
    assert [(s.weight, s.reps) for s in entries[0].sets] == [(0.0, 12)]


async def test_a_chunk_with_no_numbers_is_rejected():
    with pytest.raises(ParseError):
        parse_quick_workout("сегодня было тяжело")


async def test_leading_numbers_without_an_exercise_are_rejected():
    with pytest.raises(ParseError):
        parse_quick_workout("100x5, жим 80x8")


# ---------- the flow ----------


async def test_saves_a_finished_workout_with_every_set(fresh_db, user_id):
    db = fresh_db
    state = await _state(user_id)

    await workout.quick_log_entered(_make_message(user_id, "жим 80x8x3, присед 100x5"), state)

    workouts = await db.list_workouts(user_id, status="finished")
    assert len(workouts) == 1
    blocks = await db.list_blocks_for_workout(workouts[0]["id"])
    assert len(blocks) == 2
    all_sets = [s for b in blocks for s in await db.list_sets_for_block(b["id"])]
    assert [(s["weight"], s["reps"]) for s in all_sets] == [(80.0, 8)] * 3 + [(100.0, 5)]
    assert await state.get_state() is None


async def test_reuses_an_existing_exercise_instead_of_duplicating_it(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Грудь")
    existing = await db.create_exercise(user_id, "Жим лёжа", gid)

    await workout.quick_log_entered(_make_message(user_id, "жим лёжа 80x8"), await _state(user_id))

    assert await db.count_user_exercises(user_id) == 1
    workouts = await db.list_workouts(user_id, status="finished")
    blocks = await db.list_blocks_for_workout(workouts[0]["id"])
    sets = await db.list_sets_for_block(blocks[0]["id"])
    assert sets[0]["exercise_id"] == existing


async def test_creates_unknown_exercises_rather_than_stopping_to_ask(fresh_db, user_id):
    db = fresh_db

    await workout.quick_log_entered(
        _make_message(user_id, "какая-то моя тяга 60x10"), await _state(user_id)
    )

    names = [e["display_name"] for e in await db.list_user_exercises(user_id)]
    assert "какая-то моя тяга" in names


async def test_dates_the_workout_today_in_the_users_timezone(fresh_db, user_id):
    db = fresh_db
    await db.update_user(user_id, tz_offset=5)

    await workout.quick_log_entered(_make_message(user_id, "жим 80x8"), await _state(user_id))

    saved = (await db.list_workouts(user_id, status="finished"))[0]
    expected = timeutil.user_today(await db.get_user(user_id))
    assert saved["started_at"].startswith(expected.isoformat())


async def test_awards_achievements_for_what_was_just_logged(fresh_db, user_id):
    db = fresh_db

    await workout.quick_log_entered(_make_message(user_id, "присед 150x5"), await _state(user_id))

    codes = await db.list_achievement_codes(user_id)
    assert "first" in codes
    assert "club100" in codes


async def test_bad_input_explains_itself_and_keeps_the_flow_open(fresh_db, user_id):
    db = fresh_db
    state = await _state(user_id)
    message = _make_message(user_id, "сегодня было тяжело")

    await workout.quick_log_entered(message, state)

    message.reply.assert_awaited_once()
    assert await db.count_workouts(user_id) == 0
    assert await state.get_state() == WorkoutFlow.quick_log  # can just retype


async def test_entry_is_offered_only_while_the_diary_is_empty(fresh_db, user_id):
    kb = await workout._main_menu_kb(user_id, None)
    assert "menu:quicklog" in [b.callback_data for row in kb.inline_keyboard for b in row]

    await workout.quick_log_entered(_make_message(user_id, "жим 80x8"), await _state(user_id))

    kb = await workout._main_menu_kb(user_id, None)
    assert "menu:quicklog" not in [b.callback_data for row in kb.inline_keyboard for b in row]
