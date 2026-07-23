"""The one-tap "🔁 Повторить" set copier and the per-exercise 📝 note flow on
the live logging screen."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import ai_trainer
import keyboards
from fsm import WorkoutFlow
from handlers import workout


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    next_answer_id = iter(range(600, 700))

    async def _answer(*args, **kwargs):
        return SimpleNamespace(message_id=next(next_answer_id), chat=SimpleNamespace(id=user_id))

    message.answer = AsyncMock(side_effect=_answer)
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(side_effect=_answer)
    message.bot = bot
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _make_message(user_id: int, text: str, message_id: int = 55):
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=user_id)
    msg.message_id = message_id
    msg.from_user = SimpleNamespace(id=user_id, username="tester")
    msg.text = text
    msg.delete = AsyncMock()
    msg.reply = AsyncMock()
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.set_message_reaction = AsyncMock()

    async def _send(*args, **kwargs):
        return SimpleNamespace(message_id=700, chat=SimpleNamespace(id=user_id))

    bot.send_message = AsyncMock(side_effect=_send)
    msg.bot = bot
    return msg


async def _setup_logging(db, user_id: int):
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 0, 0, 100.0, 8, None)

    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        workout_id=workout_id, live_chat_id=user_id, live_message_id=42,
        open_exercises=[ex_id], open_blocks={ex_id: block_id}, active_exercise_id=ex_id,
        last_by_exercise={ex_id: (100.0, 8)}, last_session_sets={},
    )
    return state, ex_id, block_id


@pytest.mark.asyncio
async def test_repeat_copies_last_set(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id)
    callback = _make_callback(user_id, "live:repeat")

    await workout.live_repeat_set(callback, state)

    sets = await db.list_sets_for_block(block_id)
    assert len(sets) == 2
    assert (sets[-1]["weight"], sets[-1]["reps"]) == (100.0, 8)


@pytest.mark.asyncio
async def test_repeat_with_no_sets_is_a_noop(fresh_db, user_id):
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
        workout_id=workout_id, live_chat_id=user_id, live_message_id=42,
        open_exercises=[ex_id], open_blocks={ex_id: block_id}, active_exercise_id=ex_id,
    )
    callback = _make_callback(user_id, "live:repeat")

    await workout.live_repeat_set(callback, state)

    assert await db.list_sets_for_block(block_id) == []
    callback.answer.assert_awaited_with("Нет подхода для повтора")


@pytest.mark.asyncio
async def test_note_entered_saves_to_exercise(fresh_db, user_id):
    db = fresh_db
    state, ex_id, _ = await _setup_logging(db, user_id)
    await state.set_state(WorkoutFlow.logging_exercise_note)
    message = _make_message(user_id, "болит плечо — следи за локтями")

    await workout.live_note_entered(message, state)

    ex = await db.get_exercise(ex_id)
    assert ex["notes"] == "болит плечо — следи за локтями"
    assert await state.get_state() == WorkoutFlow.logging_set


async def _finished_baseline(db, user_id, ex_id, weight, reps):
    """A prior finished workout with one set, so later PR detection has history."""
    wid = await db.create_workout(user_id)
    block_id = await db.create_block(wid, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 0, 0, weight, reps, None)
    await db.finish_workout(wid, None, finished_at="2020-01-01T12:00:05")
    # Backdate so it sorts before the active workout.
    await db.update_workout_date(wid, "2020-01-01T12:00:00", "2020-01-01T12:00:05")


@pytest.mark.asyncio
async def test_record_set_reacts_and_keeps_message(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id)
    await _finished_baseline(db, user_id, ex_id, 100.0, 5)
    message = _make_message(user_id, "150 5")  # clear e1RM record

    await workout.log_set_text(message, state)

    message.bot.set_message_reaction.assert_awaited_once()
    react = message.bot.set_message_reaction.await_args.kwargs["reaction"]
    assert react[0].emoji == "🔥"
    message.delete.assert_not_awaited()  # trophy message stays in the chat


@pytest.mark.asyncio
async def test_ordinary_set_is_deleted_without_reaction(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id)
    await _finished_baseline(db, user_id, ex_id, 200.0, 5)  # high baseline
    message = _make_message(user_id, "60 5")  # nowhere near a record

    await workout.log_set_text(message, state)

    message.bot.set_message_reaction.assert_not_awaited()
    message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_voice_logs_a_set(fresh_db, user_id, monkeypatch):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id)

    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)

    async def _fake_transcribe(buf, uid):
        return "сто на восемь"

    monkeypatch.setattr(ai_trainer, "transcribe_voice", _fake_transcribe)

    message = _make_message(user_id, text=None)
    message.voice = SimpleNamespace(file_id="v1", duration=2, file_size=1000)
    message.bot.download = AsyncMock(return_value=SimpleNamespace(name=""))

    await workout.log_set_voice(message, state)

    sets = await db.list_sets_for_block(block_id)
    assert (sets[-1]["weight"], sets[-1]["reps"]) == (100.0, 8)
    assert "Записал" in message.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_voice_unparseable_asks_to_retry(fresh_db, user_id, monkeypatch):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id)
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)

    async def _fake_transcribe(buf, uid):
        return "давай запиши что-нибудь"

    monkeypatch.setattr(ai_trainer, "transcribe_voice", _fake_transcribe)

    message = _make_message(user_id, text=None)
    message.voice = SimpleNamespace(file_id="v1", duration=2, file_size=1000)
    message.bot.download = AsyncMock(return_value=SimpleNamespace(name=""))

    await workout.log_set_voice(message, state)

    assert len(await db.list_sets_for_block(block_id)) == 1  # nothing new logged
    assert "Не понял" in message.reply.await_args.args[0]


def test_logging_keyboard_has_repeat_and_note_when_sets_present():
    kb = keyboards.logging_keyboard([(1, "Bench")], active_id=1, has_sets=True)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "live:repeat" in cbs
    assert "live:note:1" in cbs


def test_logging_keyboard_repeat_absent_without_sets():
    kb = keyboards.logging_keyboard([(1, "Bench")], active_id=1, has_sets=False)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "live:repeat" not in cbs
    assert "live:note:1" in cbs  # note is still reachable from the card row
