from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import WorkoutFlow
from handlers import workout

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str):
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=999))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id)
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _make_message(user_id: int, text: str):
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=999))
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.bot = bot
    message.text = text
    message.delete = AsyncMock()
    return message


async def _make_state(user_id: int, **extra_data) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.picking_exercise)
    await state.update_data(
        workout_id=1, live_chat_id=user_id, live_message_id=1, pending_group_id=None,
        **extra_data,
    )
    return state


async def test_pick_page_advances_to_second_page_and_keeps_remainder(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    for i in range(14):
        await db.create_exercise(user_id, f"Exercise {i:02d}", group_id)

    state = await _make_state(user_id, pick_page=0)
    callback = _make_callback(user_id, "pick:page:1")

    await workout.pick_page(callback, state)

    data = await state.get_data()
    assert data["pick_page"] == 1

    # Second page should contain the remaining 2 exercises, sent as the new live message.
    sent_text = callback.bot.send_message.await_args.kwargs["text"]
    assert sent_text.count("Exercise") == 2


async def test_pick_page_first_page_has_no_back_button(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    for i in range(14):
        await db.create_exercise(user_id, f"Exercise {i:02d}", group_id)

    state = await _make_state(user_id, pick_page=1)
    callback = _make_callback(user_id, "pick:page:0")

    await workout.pick_page(callback, state)

    kb = callback.bot.send_message.await_args.kwargs["reply_markup"]
    callback_datas = [
        button.callback_data for row in kb.inline_keyboard for button in row
    ]
    assert "pick:page:-1" not in callback_datas
    assert "pick:page:1" in callback_datas  # next-page button still present


async def test_finishing_last_exercise_suggests_what_came_next_last_time(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    triceps = await db.create_exercise(user_id, "Triceps pushdown", group_id)

    # Prior finished workout: bench, then triceps.
    prev_workout = await db.create_workout(user_id)
    b1 = await db.create_block(prev_workout, "single")
    await db.add_block_exercise(b1, bench, 0)
    b2 = await db.create_block(prev_workout, "single")
    await db.add_block_exercise(b2, triceps, 0)
    await db.finish_workout(prev_workout)

    # Current workout: bench just logged and being finished, nothing else open.
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)
    await db.add_set(block_id, bench, 1, 0, 100, 8)

    state = await _make_state(
        user_id, open_exercises=[bench], open_blocks={bench: block_id}, active_exercise_id=bench,
    )
    await state.update_data(workout_id=workout_id)
    await state.set_state(WorkoutFlow.logging_set)
    callback = _make_callback(user_id, "live:finish_exercise")

    await workout.live_finish_exercise(callback, state)

    sent_text = callback.bot.send_message.await_args.kwargs["text"]
    assert "Triceps pushdown" in sent_text
    kb = callback.bot.send_message.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"live:suggest:{triceps}" in callback_datas


async def test_pick_search_text_finds_matching_exercise_by_substring(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", group_id)
    await db.create_exercise(user_id, "Приседания", group_id)

    state = await _make_state(user_id)
    message = _make_message(user_id, "жим")

    await workout.pick_search_text(message, state)

    message.delete.assert_awaited_once()
    sent_text = message.bot.send_message.await_args.kwargs["text"]
    assert "Жим лёжа" in sent_text
    assert "Приседания" not in sent_text
    kb = message.bot.send_message.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"pick:ex:{bench}" in callback_datas
    # Search happened from "Все" (no group selected) — creating a new exercise needs a group.
    assert not any(cb == "pick:new" for cb in callback_datas)


async def test_pick_search_text_reports_no_matches(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Жим лёжа", group_id)

    state = await _make_state(user_id)
    await state.update_data(pending_group_id=group_id)
    message = _make_message(user_id, "становая")

    await workout.pick_search_text(message, state)

    sent_text = message.bot.send_message.await_args.kwargs["text"]
    assert "Ничего не нашлось" in sent_text
    kb = message.bot.send_message.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "pick:new" in callback_datas


async def test_tapping_suggestion_jumps_straight_into_logging_it(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    triceps = await db.create_exercise(user_id, "Triceps pushdown", group_id)

    workout_id = await db.create_workout(user_id)
    state = await _make_state(
        user_id, open_exercises=[], open_blocks={}, active_exercise_id=None,
    )
    await state.update_data(workout_id=workout_id)
    await state.set_state(WorkoutFlow.idle)
    callback = _make_callback(user_id, f"live:suggest:{triceps}")

    await workout.live_pick_suggested(callback, state)

    data = await state.get_data()
    assert data["active_exercise_id"] == triceps
    assert data["open_exercises"] == [triceps]
    assert await state.get_state() == WorkoutFlow.logging_set.state
