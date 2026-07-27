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
async def test_record_set_reacts_and_keeps_message_briefly(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id)
    await _finished_baseline(db, user_id, ex_id, 100.0, 5)
    message = _make_message(user_id, "150 5")  # clear e1RM record

    await workout.log_set_text(message, state)

    message.bot.set_message_reaction.assert_awaited_once()
    react = message.bot.set_message_reaction.await_args.kwargs["reaction"]
    assert react[0].emoji == "🔥"
    message.delete.assert_not_awaited()  # not tidied away immediately, unlike a normal set


@pytest.mark.asyncio
async def test_record_set_message_is_deleted_after_delay(fresh_db, user_id, monkeypatch):
    """The 🔥 reaction message isn't left in the chat forever — it's cleaned up
    after a delay like everything else, just not instantly (so it can be noticed)."""
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id)
    await _finished_baseline(db, user_id, ex_id, 100.0, 5)
    message = _make_message(user_id, "150 5")  # clear e1RM record

    monkeypatch.setattr(workout, "_RECORD_MESSAGE_LIFETIME_SECONDS", 0)
    scheduled = []
    monkeypatch.setattr(workout.asyncio, "create_task", lambda coro: scheduled.append(coro))

    await workout.log_set_text(message, state)

    # log_set_text also re-renders the live tracker (delete-and-resend), which
    # uses this same bot.delete_message mock for an unrelated message — count
    # calls rather than asserting zero.
    assert len(scheduled) == 1
    calls_before = message.bot.delete_message.await_count

    await scheduled[0]  # let the delayed-delete task run to completion

    assert message.bot.delete_message.await_count == calls_before + 1
    message.bot.delete_message.assert_awaited_with(
        chat_id=message.chat.id, message_id=message.message_id
    )


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


def test_logging_keyboard_omits_repeat_but_keeps_note():
    # The 🔁 Повторить button was removed from the live logging screen; 📝 note
    # took over the slot freed by dropping the redundant "ℹ️ Упражнение" card.
    for has_sets in (True, False):
        kb = keyboards.logging_keyboard([(1, "Bench")], active_id=1, has_sets=has_sets)
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert "live:repeat" not in cbs
        assert "live:note:1" in cbs
    kb = keyboards.logging_keyboard([(1, "Bench")], active_id=1, has_sets=True)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "live:undo" in cbs


def test_logging_keyboard_note_button_is_labelled():
    # A bare "📝" reads as "draft"/"edit" next to "➕ Суперсет" — it needs the word.
    kb = keyboards.logging_keyboard([(1, "Bench")], active_id=1, has_sets=True)
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "📝 Заметка" in texts


def test_logging_keyboard_packs_superset_tabs_two_per_row():
    open_items = [(1, "Bench press"), (2, "Overhead press - machine"), (3, "Row")]
    kb = keyboards.logging_keyboard(open_items, active_id=1, has_sets=True)
    tab_rows = [
        row for row in kb.inline_keyboard
        if any(b.callback_data.startswith("live:switch:") for b in row)
    ]
    assert len(tab_rows) == 2  # 3 tabs -> two per row, then the odd one alone
    assert len(tab_rows[0]) == 2
    assert len(tab_rows[1]) == 1
    # The long name is shortened — it's already fully visible in the tracker text.
    long_tab_text = next(
        b.text for row in tab_rows for b in row if b.callback_data == "live:switch:2"
    )
    assert len(long_tab_text) < len("Overhead press - machine")


def test_suspicious_weight_warning_flags_likely_typo():
    last_session = [(140.0, 6, None), (130.0, 8, None)]
    warning = workout._suspicious_weight_warning(last_session, today_sets=[(1.0, 1)])
    assert warning is not None
    assert "1кг?" in warning
    assert "140кг" in warning


def test_suspicious_weight_warning_silent_for_real_backoff_set():
    last_session = [(140.0, 6, None)]
    # 70kg is a plausible deliberate backoff set, not a typo.
    assert workout._suspicious_weight_warning(last_session, today_sets=[(70.0, 8)]) is None


def test_suspicious_weight_warning_exempt_for_bodyweight():
    last_session = [(0.0, 12, None)]
    assert workout._suspicious_weight_warning(last_session, today_sets=[(0.0, 3)]) is None


def test_suspicious_weight_warning_none_without_history():
    assert workout._suspicious_weight_warning(None, today_sets=[(1.0, 1)]) is None
    assert workout._suspicious_weight_warning([(140.0, 6, None)], today_sets=None) is None


def test_suspicious_weight_warning_flags_an_extra_digit_too():
    """The check used to be one-directional — only a suspiciously *low* weight
    was flagged, so "1400" typed for "140" (parser.MAX_WEIGHT's 1500 ceiling
    doesn't catch this one, since 1400 is still a "plausible" absolute weight)
    passed through silently."""
    last_session = [(140.0, 6, None)]
    warning = workout._suspicious_weight_warning(last_session, today_sets=[(1400.0, 6)])
    assert warning is not None
    assert "1400кг?" in warning
    assert "140кг" in warning


def test_suspicious_weight_warning_silent_for_plausible_progression():
    """A real jump in working weight — not a typo — shouldn't get flagged just
    because it's well above last session's."""
    last_session = [(140.0, 6, None)]
    assert workout._suspicious_weight_warning(last_session, today_sets=[(200.0, 5)]) is None
