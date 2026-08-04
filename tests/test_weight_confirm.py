"""The "555кг? Записываем?" gate in front of a set whose weight looks like a
typo — nothing reaches the DB until the question is answered."""
import asyncio
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import WorkoutFlow
from handlers import workout
from tests.test_workout_repeat_note import _make_callback, _make_message


async def _setup(db, user_id: int, last_session=((66.0, 8, None),)):
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Seated row", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        workout_id=workout_id, live_chat_id=user_id, live_message_id=42,
        open_exercises=[ex_id], open_blocks={ex_id: block_id}, active_exercise_id=ex_id,
        last_by_exercise={}, last_session_sets={ex_id: [list(s) for s in last_session]},
    )
    return state, ex_id, block_id


@pytest.mark.asyncio
async def test_suspicious_weight_is_not_logged_until_confirmed(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup(db, user_id)

    await workout.log_set_text(_make_message(user_id, "555 5"), state)

    assert await db.list_sets_for_block(block_id) == []
    pending = (await state.get_data())["pending_weight_confirm"]
    assert pending["sets"] == [[555.0, 5, None]]
    assert pending["exercise_id"] == ex_id


@pytest.mark.asyncio
async def test_confirmation_question_names_both_weights(fresh_db, user_id):
    db = fresh_db
    state, _ex_id, _block_id = await _setup(db, user_id)
    message = _make_message(user_id, "555 5")

    await workout.log_set_text(message, state)

    asked = message.reply.await_args.args[0]
    assert "555кг?" in asked and "66кг" in asked
    kb = message.reply.await_args.kwargs["reply_markup"]
    assert [b.callback_data for row in kb.inline_keyboard for b in row] == [
        "live:wconf:yes", "live:wconf:no",
    ]


@pytest.mark.asyncio
async def test_yes_writes_the_set(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup(db, user_id)
    await workout.log_set_text(_make_message(user_id, "555 5"), state)

    await workout.live_weight_confirm(_make_callback(user_id, "live:wconf:yes"), state)

    sets = await db.list_sets_for_block(block_id)
    assert [(s["weight"], s["reps"]) for s in sets] == [(555.0, 5)]
    data = await state.get_data()
    assert data["pending_weight_confirm"] is None
    # ...and the tracker stops repeating the warning for a weight already confirmed.
    assert data["confirmed_weights"][ex_id] == 555.0


@pytest.mark.asyncio
async def test_double_tap_on_yes_does_not_duplicate_the_set(fresh_db, user_id):
    """Two fast taps on "✅ Да, записать" both read `data` before either
    finishes popping pending_weight_confirm — each then works from its own
    snapshot, so popping alone doesn't stop the second one from writing the
    same sets again.

    The window is opened explicitly here: MemoryStorage never suspends, so the
    first call would otherwise run to completion before the second starts. In
    production the real storage and the Telegram call inside the pop both
    suspend right there, which is exactly what the sleep(0) stands in for.
    """
    db = fresh_db
    state, _ex_id, block_id = await _setup(db, user_id)
    await workout.log_set_text(_make_message(user_id, "555 5"), state)

    original_get_data = state.get_data

    async def yielding_get_data():
        data = await original_get_data()
        await asyncio.sleep(0)
        return data

    state.get_data = yielding_get_data
    workout._confirming.discard(user_id)  # isolate from other tests' leftovers
    await asyncio.gather(
        workout.live_weight_confirm(_make_callback(user_id, "live:wconf:yes"), state),
        workout.live_weight_confirm(_make_callback(user_id, "live:wconf:yes"), state),
    )

    sets = await db.list_sets_for_block(block_id)
    assert [(s["weight"], s["reps"]) for s in sets] == [(555.0, 5)]


@pytest.mark.asyncio
async def test_no_discards_the_set_and_its_message(fresh_db, user_id):
    db = fresh_db
    state, _ex_id, block_id = await _setup(db, user_id)
    await workout.log_set_text(_make_message(user_id, "555 5"), state)
    callback = _make_callback(user_id, "live:wconf:no")

    await workout.live_weight_confirm(callback, state)

    assert await db.list_sets_for_block(block_id) == []
    assert (await state.get_data())["pending_weight_confirm"] is None
    deleted = [c.kwargs["message_id"] for c in callback.bot.delete_message.await_args_list]
    assert 55 in deleted  # the typed "555 5" itself, not just the question


@pytest.mark.asyncio
async def test_plausible_weight_is_logged_without_a_question(fresh_db, user_id):
    db = fresh_db
    state, _ex_id, block_id = await _setup(db, user_id)
    message = _make_message(user_id, "70 8")

    await workout.log_set_text(message, state)

    assert [(s["weight"], s["reps"]) for s in await db.list_sets_for_block(block_id)] == [(70.0, 8)]
    assert (await state.get_data()).get("pending_weight_confirm") is None
    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_typing_a_new_set_supersedes_the_pending_question(fresh_db, user_id):
    """Retyping instead of tapping "исправить" is the natural correction — the
    stale question must not stay tappable, or the typo could still land later."""
    db = fresh_db
    state, _ex_id, block_id = await _setup(db, user_id)
    await workout.log_set_text(_make_message(user_id, "555 5"), state)

    await workout.log_set_text(_make_message(user_id, "55 5", message_id=56), state)

    assert [(s["weight"], s["reps"]) for s in await db.list_sets_for_block(block_id)] == [(55.0, 5)]
    assert (await state.get_data())["pending_weight_confirm"] is None


@pytest.mark.asyncio
async def test_stale_confirmation_callback_is_a_no_op(fresh_db, user_id):
    db = fresh_db
    state, _ex_id, block_id = await _setup(db, user_id)
    callback = _make_callback(user_id, "live:wconf:yes")

    await workout.live_weight_confirm(callback, state)

    assert await db.list_sets_for_block(block_id) == []
    callback.answer.assert_awaited()


@pytest.mark.asyncio
async def test_bare_reps_are_checked_against_the_carried_weight(fresh_db, user_id):
    """"5" alone reuses the previous set's weight — the check has to see that
    resolved weight, not the parser's placeholder 0."""
    db = fresh_db
    state, ex_id, block_id = await _setup(db, user_id)
    await state.update_data(last_by_exercise={ex_id: (555.0, 5)})

    await workout.log_set_text(_make_message(user_id, "5"), state)

    assert await db.list_sets_for_block(block_id) == []
    assert (await state.get_data())["pending_weight_confirm"]["sets"] == [[555.0, 5, None]]


@pytest.mark.asyncio
async def test_no_history_means_no_question(fresh_db, user_id):
    db = fresh_db
    state, _ex_id, block_id = await _setup(db, user_id, last_session=())

    await workout.log_set_text(_make_message(user_id, "555 5"), state)

    assert [(s["weight"], s["reps"]) for s in await db.list_sets_for_block(block_id)] == [(555.0, 5)]
    assert (await state.get_data()).get("pending_weight_confirm") is None


def test_confirmed_weight_silences_the_tracker_warning():
    last_session = [(66.0, 8, None)]
    today = [(555.0, 5)]
    assert "⚠️" in workout._logging_hint(last_session, True, today_sets=today)
    assert "⚠️" not in workout._logging_hint(
        last_session, True, today_sets=today, confirmed_weight=555.0
    )
    # A *different* suspicious weight after the confirmed one still gets flagged.
    assert "⚠️" in workout._logging_hint(
        last_session, True, today_sets=[(555.0, 5), (1.0, 5)], confirmed_weight=555.0
    )


def test_weight_confirm_prompt_flags_any_set_on_a_multi_set_line():
    from parser import ParsedSet

    data = {"last_session_sets": {7: [(66.0, 8, None)]}}
    resolved = [ParsedSet(weight=66.0, reps=8), ParsedSet(weight=660.0, reps=8)]
    assert "660кг?" in workout._weight_confirm_prompt(data, 7, resolved)


@pytest.mark.asyncio
async def test_voice_asks_before_logging(fresh_db, user_id, monkeypatch):
    import ai_trainer

    db = fresh_db
    state, _ex_id, block_id = await _setup(db, user_id)
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)

    async def _fake_transcribe(buf, uid):
        return "пятьсот пятьдесят пять на пять"

    monkeypatch.setattr(ai_trainer, "transcribe_voice", _fake_transcribe)
    message = _make_message(user_id, text=None)
    message.voice = SimpleNamespace(file_id="v1", duration=2, file_size=1000)

    async def _download(_voice):
        return SimpleNamespace(name="")

    message.bot.download = _download

    await workout.log_set_voice(message, state)

    assert await db.list_sets_for_block(block_id) == []
    pending = (await state.get_data())["pending_weight_confirm"]
    assert pending["source"] == "voice"
    assert pending["sets"] == [[555.0, 5, None]]


# ---------- повторы ----------


@pytest.mark.asyncio
async def test_a_hundred_and_fifty_reps_under_load_is_questioned(fresh_db, user_id):
    """Повторы не проверялись вовсе: `parser.MAX_REPS` — 500, то есть почти
    ничего. А промахнуться легко именно голосом: «сто пятьдесят» без повторов
    слышится как одно число, вес берётся с прошлого подхода — и в базу уходит
    100×150. e1RM оттуда — 600 кг, вечный рекорд упражнения, тоннаж и Зал славы.
    """
    db = fresh_db
    state, ex_id, block_id = await _setup(db, user_id, last_session=((100.0, 8, None),))

    await workout.log_set_text(_make_message(user_id, "100 150"), state)

    assert await db.list_sets_for_block(block_id) == []
    assert (await state.get_data()).get("pending_weight_confirm") is not None
    assert "повторов" in workout._suspicious_reps_warning(100.0, 150)


@pytest.mark.asyncio
async def test_many_reps_at_bodyweight_pass_without_a_question(fresh_db, user_id):
    """50 отжиманий — обычный подход, а не промах: веса там нет, и придираться
    к числу повторов не к чему."""
    db = fresh_db
    state, ex_id, block_id = await _setup(db, user_id, last_session=((0.0, 30, None),))

    await workout.log_set_text(_make_message(user_id, "0 50"), state)

    assert len(await db.list_sets_for_block(block_id)) == 1


@pytest.mark.asyncio
async def test_a_normal_high_rep_set_is_not_questioned(fresh_db, user_id):
    """Двадцать повторов с весом — это работа на выносливость, а не опечатка.
    Порог стоит там, где силовой работы уже не бывает."""
    db = fresh_db
    state, ex_id, block_id = await _setup(db, user_id, last_session=((40.0, 20, None),))

    await workout.log_set_text(_make_message(user_id, "40 20"), state)

    assert len(await db.list_sets_for_block(block_id)) == 1
