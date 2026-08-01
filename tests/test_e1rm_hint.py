"""The fading "что такое e1RM" footnote: where it appears, and how it stops.

The metric labels every card, chart and record in the bot, so the explanation
has to be reachable from all of them — but it's only new information once, and
the counter (users.e1rm_hint_seen) is what keeps a permanent footnote from
turning into permanent noise.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import chat_bottom
import config
import formatting
from fsm import WorkoutFlow
from handlers import history, workout

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def clean_chat_tracker():
    chat_bottom.reset()
    yield
    chat_bottom.reset()


def _make_bot():
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    bot.delete_message = AsyncMock()
    return bot


def _make_callback(user_id: int, bot):
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.bot = bot
    callback.answer = AsyncMock()
    return callback


def _make_progress_callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 500
    message.text = "экран"
    message.photo = None
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    message.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=2))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int, **extra) -> FSMContext:
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )
    await state.set_state(WorkoutFlow.idle)
    await state.update_data(live_chat_id=user_id, live_message_id=1, **extra)
    return state


async def _finish_workout_with(db, user_id: int, weight: float, reps: int) -> str:
    """Log one set, finish the workout, return the card text that was sent."""
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, f"Жим {weight}x{reps}", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 1, 0, weight, reps)

    bot = _make_bot()
    state = await _make_state(user_id, workout_id=workout_id)
    await workout._finalize_workout(_make_callback(user_id, bot), state, note=None)
    return bot.edit_message_text.await_args.kwargs["text"]


async def _seed_exercise(db, user_id: int, weight: float, reps: int, n: int = 3) -> int:
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, f"Жим {weight}x{reps}", group_id)
    for i in range(1, n + 1):
        workout_id = await db.create_finished_workout(
            user_id, started_at=f"2026-01-{i:02d}T10:00:00", finished_at=f"2026-01-{i:02d}T10:30:00"
        )
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.add_set(block_id, ex_id, round_index=1, order_in_round=0, weight=weight, reps=reps)
    return ex_id


# ---------- the finished-workout card ----------


async def test_first_finished_card_explains_the_metric(fresh_db, user_id):
    text = await _finish_workout_with(fresh_db, user_id, 100, 8)

    assert "e1RM" in text
    assert formatting.E1RM_HINT in text
    assert (await fresh_db.get_user(user_id))["e1rm_hint_seen"] == 1


async def test_hint_stops_after_the_cap(fresh_db, user_id):
    for _ in range(config.E1RM_HINT_MAX_SHOWS):
        assert formatting.E1RM_HINT in await _finish_workout_with(fresh_db, user_id, 100, 8)

    text = await _finish_workout_with(fresh_db, user_id, 100, 8)
    assert "e1RM" in text  # the metric still shows — only the explanation retires
    assert formatting.E1RM_HINT not in text
    assert (await fresh_db.get_user(user_id))["e1rm_hint_seen"] == config.E1RM_HINT_MAX_SHOWS


async def test_bodyweight_only_card_neither_shows_nor_spends_a_showing(fresh_db, user_id):
    """A card measured in reps never prints "e1RM", so explaining it there would
    burn one of the few showings on a word the user hasn't met yet."""
    text = await _finish_workout_with(fresh_db, user_id, 0, 12)

    assert "e1RM" not in text
    assert formatting.E1RM_HINT not in text
    assert (await fresh_db.get_user(user_id))["e1rm_hint_seen"] == 0


async def test_rerendering_a_finished_card_does_not_spend_a_showing(fresh_db, user_id):
    """Attaching a note (or reopening the workout from history) re-renders the
    same card — the user isn't meeting the metric again."""
    await _finish_workout_with(fresh_db, user_id, 100, 8)
    assert (await fresh_db.get_user(user_id))["e1rm_hint_seen"] == 1

    workouts = await fresh_db.list_workouts(user_id, limit=1)
    saved = await fresh_db.get_workout(workouts[0]["id"])
    user = await fresh_db.get_user(user_id)
    text = await workout._finished_workout_card_text(saved, user, note=None)

    assert formatting.E1RM_HINT in text
    assert (await fresh_db.get_user(user_id))["e1rm_hint_seen"] == 1


# ---------- the progress screen ----------


async def test_progress_screen_explains_the_metric_and_counts_the_showing(fresh_db, user_id):
    ex_id = await _seed_exercise(fresh_db, user_id, 100, 8)
    callback = _make_progress_callback(user_id, f"prog:ex:{ex_id}")

    await history.prog_show_exercise(callback, await _make_state(user_id))

    caption = callback.message.answer_photo.await_args.kwargs["caption"]
    assert formatting.E1RM_HINT in caption
    assert (await fresh_db.get_user(user_id))["e1rm_hint_seen"] == 1


async def test_switching_the_period_keeps_the_hint_without_spending_a_showing(fresh_db, user_id):
    ex_id = await _seed_exercise(fresh_db, user_id, 100, 8)
    state = await _make_state(user_id)
    await history.prog_show_exercise(_make_progress_callback(user_id, f"prog:ex:{ex_id}"), state)

    callback = _make_progress_callback(user_id, f"prog:per:{ex_id}:20")
    await history.prog_change_period(callback, state)

    assert (await fresh_db.get_user(user_id))["e1rm_hint_seen"] == 1


async def test_bodyweight_progress_screen_has_no_hint(fresh_db, user_id):
    ex_id = await _seed_exercise(fresh_db, user_id, 0, 12)
    callback = _make_progress_callback(user_id, f"prog:ex:{ex_id}")

    await history.prog_show_exercise(callback, await _make_state(user_id))

    caption = callback.message.answer_photo.await_args.kwargs["caption"]
    assert formatting.E1RM_HINT not in caption
    assert (await fresh_db.get_user(user_id))["e1rm_hint_seen"] == 0


async def test_progress_caption_with_the_hint_still_fits_telegram(fresh_db, user_id):
    """The footnote is inside the caption's length budget, not appended after
    it: an over-long caption doesn't truncate, it fails the send outright."""
    ex_id = await _seed_exercise(fresh_db, user_id, 100, 8, n=25)
    callback = _make_progress_callback(user_id, f"prog:ex:{ex_id}")

    await history.prog_show_exercise(callback, await _make_state(user_id))

    caption = callback.message.answer_photo.await_args.kwargs["caption"]
    assert formatting.E1RM_HINT in caption
    assert formatting.telegram_length(caption) <= formatting.CAPTION_LIMIT


# ---------- always-available copy ----------


async def test_help_and_settings_name_the_same_concept():
    """The /help reference and the formula setting are the two places a user
    goes looking on purpose — both spell out what e1RM is."""
    assert "e1RM" in workout._HELP_TEXT
    assert "расчётный разовый максимум" in workout._HELP_TEXT
