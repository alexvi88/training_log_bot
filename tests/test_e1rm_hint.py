"""The "что такое e1RM" footnote on the progress screen.

The metric labels every card, chart and record in the bot but is never spelled
out anywhere, so a user who doesn't know the term reads the whole app in units
they can't interpret. The explanation lives on the one screen you open *to
interpret* the metric — permanently, since that's reference material — plus the
two places a user goes looking on purpose (/help and the formula setting).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import chat_bottom
import formatting
from handlers import history, workout

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def clean_chat_tracker():
    chat_bottom.reset()
    yield
    chat_bottom.reset()


def _make_callback(user_id: int, data: str):
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


async def _make_state(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


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


async def _open_progress(db, user_id: int, ex_id: int) -> str:
    callback = _make_callback(user_id, f"prog:ex:{ex_id}")
    await history.prog_show_exercise(callback, await _make_state(user_id))
    return callback.message.answer_photo.await_args.kwargs["caption"]


async def test_progress_screen_explains_the_metric(fresh_db, user_id):
    ex_id = await _seed_exercise(fresh_db, user_id, 100, 8)

    caption = await _open_progress(fresh_db, user_id, ex_id)

    assert "e1RM" in caption
    assert formatting.E1RM_HINT in caption


async def test_the_footnote_stays_on_every_visit(fresh_db, user_id):
    """It's reference material on the screen you open to read the numbers, not
    an onboarding tip that has done its job after the first look."""
    ex_id = await _seed_exercise(fresh_db, user_id, 100, 8)

    for _ in range(5):
        assert formatting.E1RM_HINT in await _open_progress(fresh_db, user_id, ex_id)


async def test_bodyweight_progress_screen_has_no_footnote(fresh_db, user_id):
    """That screen is measured in reps and never prints an e1RM — there'd be
    nothing for the line to explain."""
    ex_id = await _seed_exercise(fresh_db, user_id, 0, 12)

    caption = await _open_progress(fresh_db, user_id, ex_id)

    assert "e1RM" not in caption
    assert formatting.E1RM_HINT not in caption


async def test_switching_the_period_keeps_the_footnote(fresh_db, user_id):
    ex_id = await _seed_exercise(fresh_db, user_id, 100, 8)
    state = await _make_state(user_id)
    await history.prog_show_exercise(_make_callback(user_id, f"prog:ex:{ex_id}"), state)

    callback = _make_callback(user_id, f"prog:per:{ex_id}:20")
    await history.prog_change_period(callback, state)

    assert formatting.E1RM_HINT in callback.message.answer_photo.await_args.kwargs["caption"]


async def test_caption_with_the_footnote_still_fits_telegram(fresh_db, user_id):
    """The footnote is inside the caption's length budget, not appended after
    it: an over-long caption doesn't truncate, it fails the send outright — and
    safe_edit_photo has already deleted the previous screen by then."""
    ex_id = await _seed_exercise(fresh_db, user_id, 100, 8, n=25)

    caption = await _open_progress(fresh_db, user_id, ex_id)

    assert formatting.E1RM_HINT in caption
    assert formatting.telegram_length(caption) <= formatting.CAPTION_LIMIT


async def test_workout_card_stays_free_of_the_explanation(fresh_db, user_id):
    """The completion card is read after every session; a permanent footnote
    under it would be something to scroll past, not something to read."""
    ex_id = await _seed_exercise(fresh_db, user_id, 100, 8, n=1)
    saved = await fresh_db.get_workout((await fresh_db.list_workouts(user_id, limit=1))[0]["id"])
    user = await fresh_db.get_user(user_id)

    text = await workout._finished_workout_card_text(saved, user, note=None)

    assert "e1RM" in text
    assert formatting.E1RM_HINT not in text
    assert ex_id  # seeded exercise is what put the e1RM on the card


async def test_help_spells_out_the_term_on_the_full_screen():
    """/help is where a user goes with the question on purpose — but only its
    expanded half: the short screen answers "как записать подход" and nothing else."""
    assert "расчётный максимум в упражнении" in workout._HELP_FULL
    assert "e1RM" not in workout._HELP_SHORT


async def test_help_full_points_migrating_users_to_csv_import():
    """Онбординг намеренно не упоминает импорт (место дороже), но человек с
    историей из Hevy/Strong должен где-то узнать, что не обязан начинать с нуля."""
    assert "Импорт CSV" in workout._HELP_FULL
    assert "Hevy" in workout._HELP_FULL
