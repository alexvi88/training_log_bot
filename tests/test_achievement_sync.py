"""Badges must follow the workouts that earned them.

A set typed as 500кг instead of 50кг unlocks the whole weight-club ladder and a
chunk of lifetime tonnage. Deleting that workout (or correcting the set) has to
take those badges back — otherwise the mistake is permanent, with no workout
left in the history to point at it.
"""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

import achievement_sync
import config
from fsm import EditWorkoutFlow, HistoryFlow
from handlers import edit_workout, history

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


async def _logged_workout(db, user_id: int, sets, started="2026-01-05T10:00:00",
                          finished="2026-01-05T11:00:00", name="Жим лёжа"):
    """A finished workout with one exercise; returns (workout_id, block_id, set_ids)."""
    group_id = await db.create_muscle_group(user_id, "Грудь")
    workout_id = await db.create_finished_workout(user_id, started_at=started, finished_at=finished)
    ex_id = await db.create_exercise(user_id, name, group_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    set_ids = []
    for weight, reps in sets:
        round_idx = await db.next_round_index(block_id, ex_id)
        set_ids.append(await db.add_set(block_id, ex_id, round_idx, 0, weight, reps, None))
    return workout_id, block_id, set_ids


async def test_resync_awards_what_the_history_supports(fresh_db, user_id):
    db = fresh_db
    await _logged_workout(db, user_id, [(500.0, 10)] * 5)

    added, removed = await achievement_sync.resync(user_id)

    assert removed == []
    assert {"first", "club100", "club220", "ton10"} <= set(added)


async def test_deleting_the_workout_takes_its_badges_back(fresh_db, user_id):
    """The reported case: a workout logged with a typo'd weight, then deleted."""
    db = fresh_db
    workout_id, _, _ = await _logged_workout(db, user_id, [(500.0, 10)] * 5)
    await achievement_sync.resync(user_id)
    assert "club220" in await db.list_achievement_codes(user_id)

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(HistoryFlow.browsing)
    await history.hist_delete(_make_callback(user_id, f"hist:delyes:{workout_id}"), state)

    assert await db.count_workouts(user_id) == 0
    assert await db.list_achievement_codes(user_id) == set()


async def test_deleting_one_workout_keeps_badges_the_others_still_earn(fresh_db, user_id):
    db = fresh_db
    bogus_id, _, _ = await _logged_workout(db, user_id, [(500.0, 10)] * 5)
    await _logged_workout(
        db, user_id, [(120.0, 5)] * 3,
        started="2026-01-07T10:00:00", finished="2026-01-07T11:00:00", name="Присед",
    )
    await achievement_sync.resync(user_id)

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(HistoryFlow.browsing)
    await history.hist_delete(_make_callback(user_id, f"hist:delyes:{bogus_id}"), state)

    codes = await db.list_achievement_codes(user_id)
    assert "first" in codes and "club100" in codes  # 120кг still stands
    assert "club140" not in codes and "club220" not in codes
    assert "ton10" not in codes  # 1.8т left, not 25т


async def test_correcting_the_typo_by_editing_the_set_takes_badges_back(fresh_db, user_id):
    """Fixing the number in place is the other half of the same story — the user
    doesn't have to delete the whole session to lose an undeserved badge."""
    db = fresh_db
    workout_id, block_id, set_ids = await _logged_workout(db, user_id, [(500.0, 10)])
    await achievement_sync.resync(user_id)
    assert "club220" in await db.list_achievement_codes(user_id)

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(EditWorkoutFlow.viewing)
    await state.update_data(edit_workout_id=workout_id, edit_block_id=block_id)
    await db.update_set(set_ids[0], 50.0, 10, None)
    await edit_workout._on_workout_edited(workout_id)

    codes = await db.list_achievement_codes(user_id)
    assert codes == {"first"}


async def test_deleting_the_typo_set_takes_badges_back(fresh_db, user_id):
    db = fresh_db
    workout_id, block_id, set_ids = await _logged_workout(db, user_id, [(50.0, 10), (500.0, 10)])
    await achievement_sync.resync(user_id)
    assert "club220" in await db.list_achievement_codes(user_id)

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(EditWorkoutFlow.viewing)
    await state.update_data(edit_workout_id=workout_id, edit_block_id=block_id)
    await edit_workout.editw_delset(_make_callback(user_id, f"editw:delset:{set_ids[1]}"), state)

    assert "club220" not in await db.list_achievement_codes(user_id)
    assert "first" in await db.list_achievement_codes(user_id)


async def test_one_off_badges_survive_a_resync_triggered_elsewhere(fresh_db, user_id):
    """early_bird/marathon/new_year come from a single workout's clock, not from
    aggregates — a resync must re-derive them per workout instead of dropping
    every badge that isn't a lifetime total."""
    db = fresh_db
    _, block_id, set_ids = await _logged_workout(
        db, user_id, [(60.0, 10)] * 3,
        started="2026-01-01T05:30:00", finished="2026-01-01T08:00:00",
    )
    # Duration is measured between the first and last set, not started_at/finished_at.
    await db.conn().execute(
        "UPDATE sets SET created_at = ? WHERE block_id = ?", ("2026-01-01T05:30:00", block_id)
    )
    await db.conn().execute(
        "UPDATE sets SET created_at = ? WHERE id = ?", ("2026-01-01T08:00:00", set_ids[-1])
    )
    await db.conn().commit()
    await achievement_sync.resync(user_id)
    codes = await db.list_achievement_codes(user_id)
    assert {"early_bird", "new_year", "marathon"} <= codes

    later_id, _, _ = await _logged_workout(
        db, user_id, [(60.0, 10)] * 3,
        started="2026-03-02T18:00:00", finished="2026-03-02T19:00:00", name="Тяга",
    )
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(HistoryFlow.browsing)
    await history.hist_delete(_make_callback(user_id, f"hist:delyes:{later_id}"), state)

    assert {"early_bird", "new_year", "marathon"} <= await db.list_achievement_codes(user_id)


async def test_resync_survives_a_user_with_no_workouts(fresh_db, user_id):
    assert await achievement_sync.resync(user_id) == ([], [])
    assert await fresh_db.list_achievement_codes(user_id) == set()


async def test_moving_a_workout_off_january_first_drops_the_new_year_badge(fresh_db, user_id):
    db = fresh_db
    workout_id, _, _ = await _logged_workout(
        db, user_id, [(60.0, 10)],
        started="2026-01-01T10:00:00", finished="2026-01-01T11:00:00",
    )
    await achievement_sync.resync(user_id)
    assert "new_year" in await db.list_achievement_codes(user_id)

    await edit_workout._apply_edit_workout_date(workout_id, dt.date(2026, 1, 3))

    assert "new_year" not in await db.list_achievement_codes(user_id)


async def test_weight_clubs_use_kilograms_not_the_users_unit(fresh_db, user_id):
    """Club thresholds are in kg, but weights are stored in whatever unit the
    user picked — so a lb user cleared "Клуб 100" with a 100 lb (45 kg) lift."""
    db = fresh_db
    await db.update_user(user_id, unit="lb")
    gid = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим", gid)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 1, 0, 100, 5)  # 100 lb ≈ 45 kg
    await db.finish_workout(workout_id)

    await achievement_sync.resync(user_id)

    assert "club100" not in await db.list_achievement_codes(user_id)


async def test_switching_units_does_not_hand_out_every_weight_club(fresh_db, user_id):
    """Switching kg → lb multiplies every stored weight by 2.2. Measured against
    kg thresholds that unlocked all four clubs at once, and the award-only path
    never takes a badge back."""
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Ноги")
    ex_id = await db.create_exercise(user_id, "Присед", gid)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 1, 0, 100, 5)  # 100 kg
    await db.finish_workout(workout_id)
    await achievement_sync.resync(user_id)
    assert await db.list_achievement_codes(user_id) >= {"club100"}

    # The unit switch as handlers/settings performs it.
    await db.scale_user_set_weights(user_id, config.LB_PER_KG)
    await db.update_user(user_id, unit="lb")
    await achievement_sync.resync(user_id)

    codes = await db.list_achievement_codes(user_id)
    assert "club100" in codes  # still a real 100kg lift
    assert {"club140", "club180", "club220"}.isdisjoint(codes)
