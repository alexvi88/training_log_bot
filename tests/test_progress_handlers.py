"""Drives the progress-chart handlers (§7) end to end against a real DB.

Covers the photo-spam fix: viewing an exercise's chart must clear whatever
screen was on screen before it, and switching the period on an already-shown
chart must edit that same message instead of sending a new one.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from handlers import history

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    message.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=2))
    message.edit_media = AsyncMock()
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


async def _seed_exercise_with_sessions(db, user_id: int, n_sessions: int) -> int:
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    for i in range(1, n_sessions + 1):
        workout_id = await db.create_finished_workout(
            user_id, started_at=f"2026-01-{i:02d}T10:00:00", finished_at=f"2026-01-{i:02d}T10:30:00"
        )
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.add_set(block_id, ex_id, round_index=1, order_in_round=0, weight=100.0, reps=8)
    return ex_id


async def test_prog_show_exercise_deletes_previous_screen_before_sending_chart(fresh_db, user_id):
    """The exercise-list message must be cleared, not left behind, when a chart is sent."""
    ex_id = await _seed_exercise_with_sessions(fresh_db, user_id, 3)
    state = await _make_state(user_id)

    callback = _make_callback(user_id, f"prog:ex:{ex_id}")
    await history.prog_show_exercise(callback, state)

    callback.message.delete.assert_awaited_once()
    callback.message.answer_photo.assert_awaited_once()
    callback.answer.assert_awaited_once()


async def test_prog_show_exercise_no_sessions_still_clears_previous_screen(fresh_db, user_id):
    group_id = await fresh_db.create_muscle_group(user_id, "Спина")
    ex_id = await fresh_db.create_exercise(user_id, "Тяга", group_id)
    state = await _make_state(user_id)

    callback = _make_callback(user_id, f"prog:ex:{ex_id}")
    await history.prog_show_exercise(callback, state)

    callback.message.delete.assert_awaited_once()
    callback.message.answer.assert_awaited_once()
    callback.message.answer_photo.assert_not_awaited()


async def test_prog_change_period_edits_chart_in_place(fresh_db, user_id):
    """Switching the shown period must reuse the existing chart message (no spam)."""
    ex_id = await _seed_exercise_with_sessions(fresh_db, user_id, 3)
    state = await _make_state(user_id)

    callback = _make_callback(user_id, f"prog:per:{ex_id}:20")
    await history.prog_change_period(callback, state)

    callback.message.edit_media.assert_awaited_once()
    callback.message.delete.assert_not_awaited()
    callback.message.answer_photo.assert_not_awaited()


async def test_prog_change_period_all_shows_every_session(fresh_db, user_id):
    ex_id = await _seed_exercise_with_sessions(fresh_db, user_id, 10)
    user = await fresh_db.get_user(user_id)

    text, png, kb = await history._render_progress_view(ex_id, user, 9999)

    assert "01.01.2026" in text
    assert "10.01.2026" in text
    assert png is not None
