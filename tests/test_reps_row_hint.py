"""Первые три показа ряда «тот же вес, другие повторы» — короткая подпись для
новичка прямо в тексте экрана над рядом (см. handlers.workout._maybe_reps_row_hint,
_REPS_ROW_HINT_TEXT). Дальше молчим — постоянная строка с весом (reps_row_line)
объясняет достаточно тем, кто уже видел ряд несколько раз."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import WorkoutFlow
from handlers import workout

pytestmark = pytest.mark.asyncio

_HINT = "Цифры внизу"


def _make_bot():
    bot = MagicMock()
    bot.delete_message = AsyncMock()

    async def _send(*args, **kwargs):
        _make_bot.sent_text = kwargs.get("text", "")
        return SimpleNamespace(message_id=900, chat=SimpleNamespace(id=1))

    bot.send_message = AsyncMock(side_effect=_send)
    return bot


async def _setup(db, user_id: int):
    """Активная тренировка с одним подходом — ряд «тот же вес» уже есть,
    от чего оттолкнуться (см. workout._reps_row_basis)."""
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 0, 0, 25.0, 10, None)

    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        workout_id=workout_id, live_chat_id=1, live_message_id=1,
        open_exercises=[ex_id], open_blocks={ex_id: block_id}, active_exercise_id=ex_id,
        last_by_exercise={}, last_session_sets={},
    )
    return state


async def test_hint_shown_on_first_appearance_of_the_row(fresh_db, user_id):
    db = fresh_db
    state = await _setup(db, user_id)
    user = await db.get_user(user_id)
    bot = _make_bot()

    await workout._render_logging_screen(bot, state, user)

    assert _HINT in _make_bot.sent_text


async def test_hint_shown_exactly_three_times_then_stops(fresh_db, user_id):
    db = fresh_db
    user = await db.get_user(user_id)
    bot = _make_bot()

    shown = []
    for _ in range(5):
        state = await _setup(db, user_id)
        await workout._render_logging_screen(bot, state, user)
        shown.append(_HINT in _make_bot.sent_text)

    assert shown == [True, True, True, False, False]


async def test_hint_absent_when_the_row_itself_is_absent(fresh_db, user_id):
    """Без веса, от которого оттолкнуться (самый первый подход в жизни
    упражнения без истории), ряда кнопок нет вовсе — значит, и подписывать
    нечего. Показ не должен списываться со счётчика впустую."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)

    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        workout_id=workout_id, live_chat_id=1, live_message_id=1,
        open_exercises=[ex_id], open_blocks={ex_id: block_id}, active_exercise_id=ex_id,
        last_by_exercise={}, last_session_sets={},
    )
    user = await db.get_user(user_id)
    bot = _make_bot()

    await workout._render_logging_screen(bot, state, user)
    assert _HINT not in _make_bot.sent_text

    # Счётчик не тронут — первый настоящий показ ряда всё ещё получит подпись.
    state2 = await _setup(db, user_id)
    await workout._render_logging_screen(bot, state2, user)
    assert _HINT in _make_bot.sent_text


# ---------- _maybe_reps_row_hint напрямую ----------


async def test_claim_fires_exactly_three_times_per_account(fresh_db, user_id):
    text = workout._REPS_ROW_HINT_TEXT
    seen = [await workout._maybe_reps_row_hint(user_id) for _ in range(5)]
    assert seen == [text, text, text, "", ""]


async def test_hint_ack_survives_the_nightly_prune(fresh_db, user_id):
    """prune_old_limit_acks (admin_tasks.py) чистит ai_limit_ack по date < cutoff
    раз в сутки — счётчик показов живёт там же (has_limit_ack/record_limit_ack),
    но с фиктивной «датой»-сентинелом, которую настоящий cutoff не достаёт."""
    text = workout._REPS_ROW_HINT_TEXT
    await workout._maybe_reps_row_hint(user_id)

    deleted = await fresh_db.prune_old_limit_acks(keep_days=7)

    assert deleted == 0
    # Счётчик всё ещё помнит один показ — второй тоже покажет подпись, а не
    # четвёртый по счёту после мнимого сброса.
    assert await workout._maybe_reps_row_hint(user_id) == text
    assert await workout._maybe_reps_row_hint(user_id) == text
    assert await workout._maybe_reps_row_hint(user_id) == ""
