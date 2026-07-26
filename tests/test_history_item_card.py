"""The history detail screen (hist:item) must show the same tonnage-equivalent
and PR-highlight content as the just-finished completion card — previously it
only rendered the bare sets, making a past workout look poorer than the one
you just logged."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

from handlers import history

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.text = "some previous screen"
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.edit_text = AsyncMock(return_value=message)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    message.delete = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


async def test_history_item_includes_tonnage_equivalent(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    squat = await db.create_exercise(user_id, "Присед", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, squat, 0)
    # 200kg x 10 = 2000kg tonnage, comfortably above the "это как N ..." threshold.
    await db.add_set(block_id, squat, 1, 0, 200.0, 10)
    await db.finish_workout(workout_id)

    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"hist:item:{workout_id}")

    assert await history.show_history_item(callback, workout_id)

    text = callback.message.answer.await_args.args[0]
    assert "Суммарно за тренировку" in text
    assert "Это как" in text


async def test_history_item_includes_pr_highlight_vs_prior_session(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", group_id)

    w1 = await db.create_workout(user_id, started_at="2026-01-01T12:00:00")
    b1 = await db.create_block(w1, "single")
    await db.add_block_exercise(b1, bench, 0)
    await db.add_set(b1, bench, 1, 0, 80.0, 5)
    await db.finish_workout(w1, finished_at="2026-01-01T12:00:00")

    w2 = await db.create_workout(user_id, started_at="2026-01-08T12:00:00")
    b2 = await db.create_block(w2, "single")
    await db.add_block_exercise(b2, bench, 0)
    await db.add_set(b2, bench, 1, 0, 90.0, 5)
    await db.finish_workout(w2, finished_at="2026-01-08T12:00:00")

    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"hist:item:{w2}")

    assert await history.show_history_item(callback, w2)

    text = callback.message.answer.await_args.args[0]
    assert "vs предыдущего рекорда" in text
