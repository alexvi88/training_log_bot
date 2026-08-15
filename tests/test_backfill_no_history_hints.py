"""Занесение задним числом не показывает ни весов «в прошлый раз», ни цели.

История в базе к этому моменту почти всегда СВЕЖЕЕ заносимой даты: занося
тренировку за 3 августа, человек видел веса с 6-го и посчитанную от них цель.
См. workout._render_logging_screen и _weight_confirm_prompt.
"""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import WorkoutFlow
from handlers import workout
from parser import ParsedSet


def _make_bot():
    bot = MagicMock()
    bot.delete_message = AsyncMock()

    async def _send(*args, **kwargs):
        _make_bot.sent_text = kwargs.get("text", "")
        return SimpleNamespace(message_id=900, chat=SimpleNamespace(id=1))

    bot.send_message = AsyncMock(side_effect=_send)
    return bot


async def _setup(db, user_id: int, *, is_backfill: bool):
    """Прошлая тренировка 6 августа, заносимая — 3 августа: история «из будущего»."""
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)

    later = dt.date.today()
    prev_id = await db.create_workout(user_id, started_at=f"{later.isoformat()}T12:00:00")
    prev_block = await db.create_block(prev_id, "single")
    await db.add_block_exercise(prev_block, ex_id, 0)
    for i in range(3):
        await db.add_set(prev_block, ex_id, i, 0, 100.0, 8, None)
    await db.finish_workout(prev_id, finished_at=f"{later.isoformat()}T13:00:00")

    earlier = later - dt.timedelta(days=3)
    workout_id = await db.create_workout(
        user_id, started_at=f"{earlier.isoformat()}T12:00:00",
        status="backfill" if is_backfill else "active",
    )
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)

    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        workout_id=workout_id, live_chat_id=1, live_message_id=1,
        open_exercises=[ex_id], open_blocks={ex_id: block_id}, active_exercise_id=ex_id,
        last_by_exercise={}, last_session_sets={ex_id: [(100.0, 8, None)] * 3},
        exercise_targets={ex_id: "3×8"}, is_backfill=is_backfill,
    )
    return state, ex_id


@pytest.mark.asyncio
async def test_live_workout_still_shows_history_and_target(fresh_db, user_id):
    db = fresh_db
    state, _ex_id = await _setup(db, user_id, is_backfill=False)
    bot = _make_bot()

    await workout._render_logging_screen(bot, state, await db.get_user(user_id))

    assert "В прошлый раз" in _make_bot.sent_text
    assert "📋 План" in _make_bot.sent_text
    assert "🎯 Цель" in _make_bot.sent_text


@pytest.mark.asyncio
async def test_backfill_hides_history_and_target(fresh_db, user_id):
    db = fresh_db
    state, _ex_id = await _setup(db, user_id, is_backfill=True)
    bot = _make_bot()

    await workout._render_logging_screen(bot, state, await db.get_user(user_id))

    assert "В прошлый раз" not in _make_bot.sent_text
    assert "📋 План" not in _make_bot.sent_text
    assert "Цель" not in _make_bot.sent_text


@pytest.mark.asyncio
async def test_backfill_does_not_question_a_weight_against_a_later_session():
    """Вопрос «555кг? в прошлый раз 100кг» сверяется с той же историей из
    будущего — у бэкфилла его нет. Проверка повторов историей не пользуется и
    остаётся на месте."""
    data = {"last_session_sets": {1: [(100.0, 8, None)]}, "is_backfill": True}
    heavy = [ParsedSet(weight=500.0, reps=5)]

    assert workout._weight_confirm_prompt(data, 1, heavy) is None
    assert workout._weight_confirm_prompt({**data, "is_backfill": False}, 1, heavy) is not None

    absurd_reps = [ParsedSet(weight=100.0, reps=200)]
    assert workout._weight_confirm_prompt(data, 1, absurd_reps) is not None
