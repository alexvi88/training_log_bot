"""Ряд цифр повторов над трекером бота (`live:reps:N`, handlers.workout.live_reps_button).

Самая частая запись подхода в боте после текста — и до сих пор ни одного
теста: обработчик пишет в дневник вес, который сам вывел из истории. Ошибка
тут — тихая неправда в дневнике (не тот вес, перенесённая чужая RPE, вес на
подтягиваниях), и человек её не заметит, потому что цифру не набирал.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import WorkoutFlow
from handlers import workout

pytestmark = pytest.mark.asyncio


def _callback(user_id: int, data: str):
    async def _answer(*args, **kwargs):
        return SimpleNamespace(message_id=700, chat=SimpleNamespace(id=user_id))

    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(side_effect=_answer)
    bot.edit_message_text = AsyncMock()
    bot.send_media_group = AsyncMock(return_value=[])
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.answer = AsyncMock(side_effect=_answer)
    message.bot = bot
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _logging_state(db, user_id, *, today_sets=(), last_session=None, reps_only=False):
    gid = await db.create_muscle_group(user_id, "Спина")
    ex = await db.create_exercise(user_id, "Мои подтягивания" if reps_only else "Тяга штанги", gid)
    if reps_only:
        await db.conn().execute("UPDATE exercises SET bodyweight_load = 'full' WHERE id = ?", (ex,))
        await db.conn().commit()
    workout_id = await db.create_workout(user_id)
    block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block, ex, 0)
    for i, (weight, reps, rpe) in enumerate(today_sets):
        await db.add_set(block, ex, i, 0, weight, reps, rpe)
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        workout_id=workout_id, live_chat_id=user_id, live_message_id=42,
        open_exercises=[ex], open_blocks={ex: block}, active_exercise_id=ex,
        last_by_exercise={}, last_session_sets={ex: last_session} if last_session else {},
    )
    return state, ex, block


def _logged(rows):
    return [(r["weight"], r["reps"], r["rpe"]) for r in rows]


async def test_takes_weight_of_todays_last_set_and_drops_its_rpe(fresh_db, user_id):
    db = fresh_db
    state, ex, block = await _logging_state(db, user_id, today_sets=[(80.0, 8, 8.0), (75.0, 8, 9.0)])

    await workout.live_reps_button(_callback(user_id, "live:reps:6"), state)

    # Вес последнего подхода, новые повторы, RPE не переносится (другие
    # повторы — другая тяжесть, оценку за человека не выдумываем).
    assert _logged(await db.list_sets_for_block(block))[-1] == (75.0, 6, None)
    assert tuple((await state.get_data())["last_by_exercise"][ex]) == (75.0, 6)


async def test_first_set_uses_first_set_of_last_session_not_the_backoff(fresh_db, user_id):
    db = fresh_db
    state, _, block = await _logging_state(
        db, user_id, last_session=[(100.0, 5, None), (90.0, 8, None), (85.0, 8, None)],
    )

    await workout.live_reps_button(_callback(user_id, "live:reps:5"), state)

    assert _logged(await db.list_sets_for_block(block)) == [(100.0, 5, None)]


async def test_reps_only_exercise_logs_zero_weight_even_with_weighted_history(fresh_db, user_id):
    db = fresh_db
    state, _, block = await _logging_state(
        db, user_id, reps_only=True, last_session=[(10.0, 8, None)],
    )

    await workout.live_reps_button(_callback(user_id, "live:reps:9"), state)

    assert _logged(await db.list_sets_for_block(block)) == [(0.0, 9, None)]


async def test_nothing_to_base_on_writes_nothing_and_hints(fresh_db, user_id):
    db = fresh_db
    state, _, block = await _logging_state(db, user_id)
    callback = _callback(user_id, "live:reps:8")

    await workout.live_reps_button(callback, state)

    assert await db.list_sets_for_block(block) == []
    hint = callback.answer.await_args.args[0]
    assert hint  # подсказка «запиши первый подход», а не молчание


@pytest.mark.parametrize("data", ["live:reps:0", "live:reps:-3", "live:reps:abc", "live:reps:"])
async def test_bad_reps_payload_writes_nothing(fresh_db, user_id, data):
    db = fresh_db
    state, _, block = await _logging_state(db, user_id, today_sets=[(60.0, 10, None)])

    await workout.live_reps_button(_callback(user_id, data), state)

    assert _logged(await db.list_sets_for_block(block)) == [(60.0, 10, None)]


async def test_closed_workout_is_not_written_to(fresh_db, user_id):
    """Тренировку закрыли с другого клиента, а кнопка в чате осталась."""
    db = fresh_db
    state, _, block = await _logging_state(db, user_id, today_sets=[(60.0, 10, None)])
    await db.finish_workout((await state.get_data())["workout_id"])

    await workout.live_reps_button(_callback(user_id, "live:reps:8"), state)

    assert _logged(await db.list_sets_for_block(block)) == [(60.0, 10, None)]
