"""Regression tests for _finalize_workout: double-tap idempotency and correct
record/comparison detection when a workout is backfilled to a date that isn't
chronologically last.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

from fsm import WorkoutFlow
from handlers import workout

pytestmark = pytest.mark.asyncio


def _make_bot():
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.delete_message = AsyncMock()
    return bot


def _make_callback(user_id: int, bot):
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.bot = bot
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int, **extra_data) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.idle)
    await state.update_data(live_chat_id=user_id, live_message_id=1, **extra_data)
    return state


async def _log_bench_set(db, workout_id: int, exercise_id: int, weight: float, reps: int):
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, exercise_id, 0)
    await db.add_set(block_id, exercise_id, 1, 0, weight, reps)


async def test_double_tap_finalize_is_idempotent(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    workout_id = await db.create_workout(user_id)
    await _log_bench_set(db, workout_id, bench, 100, 5)

    bot = _make_bot()
    state1 = await _make_state(user_id, workout_id=workout_id)
    state2 = await _make_state(user_id, workout_id=workout_id)
    callback1 = _make_callback(user_id, bot)
    callback2 = _make_callback(user_id, bot)

    # Simulate a fast double-tap: both handlers already read the same FSM
    # data before either finishes, so both call _finalize_workout for the
    # same workout_id.
    await workout._finalize_workout(callback1, state1, note=None)
    await workout._finalize_workout(callback2, state2, note=None)

    saved = await db.get_workout(workout_id)
    assert saved["status"] == "finished"
    # The second call must be a no-op: only one edit of the live message, and no
    # menu message (the card no longer auto-sends one — see live:back_to_menu).
    assert bot.edit_message_text.await_count == 1
    assert bot.send_message.await_count + bot.send_photo.await_count == 0
    callback2.answer.assert_awaited_once()


async def test_finalize_drops_empty_blocks(fresh_db, user_id):
    """An exercise added mid-workout but never given a set shouldn't linger as
    a "подходов нет" placeholder once the workout is finished."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    squat = await db.create_exercise(user_id, "Squat", group_id)
    empty_ex = await db.create_exercise(user_id, "Abandoned exercise", group_id)
    workout_id = await db.create_workout(user_id)
    await _log_bench_set(db, workout_id, squat, 100, 5)
    empty_block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(empty_block, empty_ex, 1)

    bot = _make_bot()
    state = await _make_state(user_id, workout_id=workout_id)
    callback = _make_callback(user_id, bot)

    await workout._finalize_workout(callback, state, note=None)

    full_text = bot.edit_message_text.await_args.kwargs["text"]
    assert "Abandoned exercise" not in full_text
    assert "подходов нет" not in full_text
    blocks = await db.list_blocks_for_workout(workout_id)
    assert len(blocks) == 1


async def test_backfill_does_not_compare_against_later_workout(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)

    # An older real session (2026-01-01) and a later, much stronger real
    # session (2026-01-10) already exist.
    w1 = await db.create_workout(user_id, started_at="2026-01-01T12:00:00")
    await _log_bench_set(db, w1, bench, 100, 5)  # e1RM ~116.7
    await db.finish_workout(w1, finished_at="2026-01-01T12:00:00")

    w3 = await db.create_workout(user_id, started_at="2026-01-10T12:00:00")
    await _log_bench_set(db, w3, bench, 200, 5)  # e1RM ~233.3
    await db.finish_workout(w3, finished_at="2026-01-10T12:00:00")

    # Now backfill a workout dated in between the two, at a weight/rep combo
    # already matched by both existing sessions (not a genuine PR either way).
    w2 = await db.create_workout(user_id, started_at="2026-01-05T12:00:00", status="backfill")
    await _log_bench_set(db, w2, bench, 100, 5)  # matches w1 exactly, weaker than w3

    bot = _make_bot()
    state = await _make_state(
        user_id, workout_id=w2, is_backfill=True, bf_date="2026-01-05",
    )
    callback = _make_callback(user_id, bot)

    await workout._finalize_workout(callback, state, note=None)

    saved = await db.get_workout(w2)
    assert saved["status"] == "finished"
    full_text = bot.edit_message_text.await_args.kwargs["text"]
    # Must not be falsely flagged as a record or improvement — its true
    # predecessor (w1, 2026-01-01) was stronger, even though a later
    # unrelated workout (w3) is even stronger still.
    assert "рекорд" not in full_text.lower()
    assert "vs прошлой" not in full_text


async def test_e1rm_comparison_uses_alltime_max_not_previous_session(fresh_db, user_id):
    """A session that beats the last one but not the true all-time best e1RM
    must not be reported as growth."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)

    w1 = await db.create_workout(user_id, started_at="2026-01-01T12:00:00")
    await _log_bench_set(db, w1, bench, 100, 5)  # e1RM ~116.7 — the true best
    await db.finish_workout(w1, finished_at="2026-01-01T12:00:00")

    w2 = await db.create_workout(user_id, started_at="2026-01-05T12:00:00")
    await _log_bench_set(db, w2, bench, 100, 3)  # e1RM = 110 — weaker than w1
    await db.finish_workout(w2, finished_at="2026-01-05T12:00:00")

    # w3 beats w2 (the immediately previous session) but not w1 (the all-time max).
    w3 = await db.create_workout(user_id, started_at="2026-01-10T12:00:00")
    await _log_bench_set(db, w3, bench, 100, 4)  # e1RM ~113.3

    bot = _make_bot()
    state = await _make_state(user_id, workout_id=w3)
    callback = _make_callback(user_id, bot)

    await workout._finalize_workout(callback, state, note=None)

    full_text = bot.edit_message_text.await_args.kwargs["text"]
    assert "vs предыдущего рекорда" not in full_text


async def test_ai_comment_generated_in_background_after_finalize(fresh_db, user_id, monkeypatch):
    """Finishing a workout must not block on the AI-trainer comment: the summary
    is sent first, and the comment is appended via a second edit once ready."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    workout_id = await db.create_workout(user_id)
    await _log_bench_set(db, workout_id, bench, 100, 5)
    await db.update_user(user_id, ai_comments_enabled=1)

    monkeypatch.setattr(workout.ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(
        workout.ai_trainer, "comment_on_workout", AsyncMock(return_value="Отличная работа!")
    )

    bot = _make_bot()
    state = await _make_state(user_id, workout_id=workout_id)
    callback = _make_callback(user_id, bot)

    await workout._finalize_workout(callback, state, note=None)

    first_text = bot.edit_message_text.await_args_list[0].kwargs["text"]
    assert "Отличная работа!" not in first_text
    # Регрессия: без плейсхолдера карточка без комментария неотличима от той, у
    # кого AI-комментарии выключены вовсе — человек не понимает, ждать ли ответ.
    assert "Разбираю тренировку" in first_text

    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    await asyncio.gather(*pending)

    last_text = bot.edit_message_text.await_args_list[-1].kwargs["text"]
    assert "Отличная работа!" in last_text
    assert "Разбираю тренировку" not in last_text
    saved = await db.get_workout(workout_id)
    assert saved["ai_comment"] == "Отличная работа!"


async def test_ai_comment_placeholder_is_cleared_on_failure(fresh_db, user_id, monkeypatch):
    """Регрессия: на упавшем разборе правился только reply_markup, а плейсхолдер
    «Разбираю тренировку...» так и оставался в тексте карточки навсегда — рядом
    с кнопкой «Разобрать», которая противоречит тексту над ней."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    workout_id = await db.create_workout(user_id)
    await _log_bench_set(db, workout_id, bench, 100, 5)
    await db.update_user(user_id, ai_comments_enabled=1)

    monkeypatch.setattr(workout.ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(
        workout.ai_trainer, "comment_on_workout", AsyncMock(side_effect=RuntimeError("llm down"))
    )

    bot = _make_bot()
    state = await _make_state(user_id, workout_id=workout_id)
    callback = _make_callback(user_id, bot)

    await workout._finalize_workout(callback, state, note=None)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    await asyncio.gather(*pending)

    last_text = bot.edit_message_text.await_args_list[-1].kwargs["text"]
    assert "Разбираю тренировку" not in last_text


async def test_background_tasks_are_kept_referenced_until_they_finish(fresh_db, user_id):
    """The event loop holds only weak references to running tasks, so a
    fire-and-forget create_task() can be collected mid-flight — the record
    message would never be tidied away and the AI comment would silently never
    arrive."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow():
        started.set()
        await release.wait()

    task = workout._spawn(slow())
    await started.wait()

    assert task in workout._background_tasks

    release.set()
    await task
    assert task not in workout._background_tasks
