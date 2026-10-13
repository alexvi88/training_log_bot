"""Голосовой ввод не рекламируется нигде (владелец: «давай нигде не говорить
про голосовой ввод»): ни строкой в инструкции подхода, ни разовой подсказкой
на третьем подходе, ни в справке /help, ни во вступлении AI-тренера. Сам
голос работает как раньше — кто пришлёт голосовое, тому запишу."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import WorkoutFlow
from handlers import workout

_VOICE_WORDS = ("голос", "🎙", "🎤", "voice", "сто на восемь", "hundred for eight")


def _make_message(user_id: int, text: str, message_id: int):
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=user_id)
    msg.message_id = message_id
    msg.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
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


def _mentions_voice(text: str) -> bool:
    low = text.lower()
    return any(word in low for word in _VOICE_WORDS)


@pytest.mark.asyncio
async def test_typed_sets_never_pitch_voice(fresh_db, user_id):
    state = await _setup_logging(fresh_db, user_id)
    for i in range(1, 6):
        message = _make_message(user_id, "50 10", message_id=i)
        await workout.log_set_text(message, state)
        assert not any(_mentions_voice(t) for t in message.bot.sent_texts)


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("key", [
    "workout.instruction_base", "workout.instruction_reps_only",
    "workout.help_full", "workout.help_short", "ai.screen.intro",
])
def test_screens_do_not_mention_voice(lang, key):
    catalog = json.loads((Path(__file__).resolve().parent.parent / "locales" / f"{lang}.json").read_text("utf-8"))
    assert not _mentions_voice(catalog[key]), catalog[key]
