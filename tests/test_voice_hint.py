"""Одноразовая подсказка про голосовой ввод (задание №9): на третьем подходе,
набранном текстом за всю жизнь пользователя, тренер один раз упоминает голос —
он спрятан в /help, куда новичок сам не заходит. Дальше — тишина."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import WorkoutFlow
from handlers import workout

_HINT = "Кстати, можно голосом"


def _make_message(user_id: int, text: str, message_id: int):
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=user_id)
    msg.message_id = message_id
    msg.from_user = SimpleNamespace(id=user_id, username="tester")
    msg.text = text
    msg.delete = AsyncMock()
    msg.reply = AsyncMock()

    sent_texts: list[str] = []

    async def _send(chat_id, text, *args, **kwargs):
        sent_texts.append(text)
        return SimpleNamespace(message_id=900, chat=SimpleNamespace(id=chat_id))

    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.set_message_reaction = AsyncMock()
    bot.send_message = AsyncMock(side_effect=_send)
    bot.sent_texts = sent_texts
    msg.bot = bot
    return msg


async def _setup_logging(db, user_id: int, *, is_backfill: bool = False):
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)

    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.logging_set)
    data = dict(
        workout_id=workout_id, live_chat_id=user_id, live_message_id=42,
        open_exercises=[ex_id], open_blocks={ex_id: block_id}, active_exercise_id=ex_id,
        last_by_exercise={}, last_session_sets={},
    )
    if is_backfill:
        data["is_backfill"] = True
    await state.update_data(**data)
    return state


def _hint_sent(bot) -> bool:
    return any(_HINT in text for text in bot.sent_texts)


@pytest.mark.asyncio
async def test_no_hint_on_the_very_first_typed_set(fresh_db, user_id):
    state = await _setup_logging(fresh_db, user_id)
    message = _make_message(user_id, "50 10", message_id=1)

    await workout.log_set_text(message, state)

    assert not _hint_sent(message.bot)


@pytest.mark.asyncio
async def test_hint_appears_on_the_third_typed_set(fresh_db, user_id):
    state = await _setup_logging(fresh_db, user_id)

    for i in range(1, 3):
        message = _make_message(user_id, "50 10", message_id=i)
        await workout.log_set_text(message, state)
        assert not _hint_sent(message.bot)

    third = _make_message(user_id, "50 10", message_id=3)
    await workout.log_set_text(third, state)

    assert _hint_sent(third.bot)


@pytest.mark.asyncio
async def test_hint_does_not_repeat_on_the_fourth_set(fresh_db, user_id):
    state = await _setup_logging(fresh_db, user_id)
    for i in range(1, 4):
        message = _make_message(user_id, "50 10", message_id=i)
        await workout.log_set_text(message, state)

    fourth = _make_message(user_id, "50 10", message_id=4)
    await workout.log_set_text(fourth, state)

    assert not _hint_sent(fourth.bot)


@pytest.mark.asyncio
async def test_one_line_with_several_sets_crosses_the_threshold_in_one_go(fresh_db, user_id):
    """"50 10, 50 9, 50 8" — три подхода одной строкой, а не три сообщения:
    порог должен сработать и на таком скачке через него, а не только на
    ровном третьем сообщении."""
    state = await _setup_logging(fresh_db, user_id)
    message = _make_message(user_id, "50 10, 50 9, 50 8", message_id=1)

    await workout.log_set_text(message, state)

    assert _hint_sent(message.bot)


@pytest.mark.asyncio
async def test_no_hint_during_backfill(fresh_db, user_id):
    """Задним числом — не живая тренировка (задание №9): подход тут не первый
    в жизни, а прошлый, и подсказка не к месту."""
    state = await _setup_logging(fresh_db, user_id, is_backfill=True)

    for i in range(1, 4):
        message = _make_message(user_id, "50 10", message_id=i)
        await workout.log_set_text(message, state)
        assert not _hint_sent(message.bot)


# ---------- db.register_manual_sets_and_check_hint напрямую ----------


async def test_register_manual_sets_crosses_threshold_exactly_once(fresh_db, user_id):
    db = fresh_db
    assert await db.register_manual_sets_and_check_hint(user_id, 1) is False
    assert await db.register_manual_sets_and_check_hint(user_id, 1) is False
    assert await db.register_manual_sets_and_check_hint(user_id, 1) is True
    # Порог уже пройден и подсказка отмечена показанной — дальше молчим.
    assert await db.register_manual_sets_and_check_hint(user_id, 1) is False


async def test_register_manual_sets_jump_over_threshold_still_fires(fresh_db, user_id):
    db = fresh_db
    assert await db.register_manual_sets_and_check_hint(user_id, 5) is True
    assert await db.register_manual_sets_and_check_hint(user_id, 1) is False


async def test_mark_voice_hint_shown_suppresses_future_crossings(fresh_db, user_id):
    db = fresh_db
    await db.mark_voice_hint_shown(user_id)
    assert await db.register_manual_sets_and_check_hint(user_id, 10) is False


@pytest.mark.asyncio
async def test_voice_user_does_not_get_the_pitch_for_voice(fresh_db, user_id):
    """Уже пользуется голосом (см. _finalize_voice_sets) — предлагать ему то же
    самое второй раз незачем, даже если потом наберёт три подхода текстом."""
    db = fresh_db
    await db.mark_voice_hint_shown(user_id)
    state = await _setup_logging(db, user_id)

    for i in range(1, 4):
        message = _make_message(user_id, "50 10", message_id=i)
        await workout.log_set_text(message, state)
        assert not _hint_sent(message.bot)
