"""One-line text commands typed into the live logging screen: "-" (undo),
"=" (repeat), "!text" (note), "N: 100 8" (edit an already-logged set)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import WorkoutFlow
from handlers import workout


def _make_message(user_id: int, text: str, message_id: int = 55):
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=user_id)
    msg.message_id = message_id
    msg.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    msg.text = text
    msg.delete = AsyncMock()
    msg.reply = AsyncMock()
    msg.answer = AsyncMock()
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.set_message_reaction = AsyncMock()

    async def _send(*args, **kwargs):
        return SimpleNamespace(message_id=700, chat=SimpleNamespace(id=user_id))

    bot.send_message = AsyncMock(side_effect=_send)
    msg.bot = bot
    return msg


def _make_callback(user_id: int, data: str):
    cb = MagicMock()
    cb.data = data
    cb.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    cb.answer = AsyncMock()
    cb.message = _make_message(user_id, "")
    cb.message.edit_text = AsyncMock()
    return cb


async def _setup_logging(db, user_id: int, sets: list[tuple[float, int]] | None = None):
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for weight, reps in sets or []:
        await db.add_set(block_id, ex_id, 0, 0, weight, reps, None)

    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        workout_id=workout_id, live_chat_id=user_id, live_message_id=42,
        open_exercises=[ex_id], open_blocks={ex_id: block_id}, active_exercise_id=ex_id,
        last_by_exercise={ex_id: sets[-1]} if sets else {}, last_session_sets={},
    )
    return state, ex_id, block_id


# ---------- "-" undo ----------


@pytest.mark.asyncio
async def test_dash_undoes_the_last_set(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id, [(100.0, 8), (100.0, 7)])
    message = _make_message(user_id, "-")

    await workout.log_set_text(message, state)

    sets = await db.list_sets_for_block(block_id)
    assert [(s["weight"], s["reps"]) for s in sets] == [(100.0, 8)]
    message.delete.assert_awaited_once()  # the "-" command itself is tidied away


@pytest.mark.asyncio
async def test_dash_with_nothing_to_undo_replies_instead_of_crashing(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id, [])
    message = _make_message(user_id, "-")

    await workout.log_set_text(message, state)

    assert await db.list_sets_for_block(block_id) == []
    message.reply.assert_awaited_once()
    message.delete.assert_not_awaited()  # left in place so the user sees what they typed


# ---------- "=" repeat ----------


@pytest.mark.asyncio
async def test_equals_repeats_the_last_set(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id, [(100.0, 8)])
    message = _make_message(user_id, "=")

    await workout.log_set_text(message, state)

    sets = await db.list_sets_for_block(block_id)
    assert [(s["weight"], s["reps"]) for s in sets] == [(100.0, 8), (100.0, 8)]
    message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_equals_with_nothing_to_repeat_replies_instead(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id, [])
    message = _make_message(user_id, "=")

    await workout.log_set_text(message, state)

    assert await db.list_sets_for_block(block_id) == []
    message.reply.assert_awaited_once()


# ---------- "!text" note ----------


@pytest.mark.asyncio
async def test_bang_sets_a_note_on_the_active_exercise(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id, [(100.0, 8)])
    message = _make_message(user_id, "!болит плечо — следи за локтями")

    await workout.log_set_text(message, state)

    workout_id = (await state.get_data())["workout_id"]
    note = await db.get_workout_exercise_note(workout_id, ex_id)
    assert note == "болит плечо — следи за локтями"
    message.delete.assert_awaited_once()
    # It doesn't also get parsed as a set.
    assert len(await db.list_sets_for_block(block_id)) == 1


@pytest.mark.asyncio
async def test_bang_with_no_text_asks_for_it(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id, [(100.0, 8)])
    message = _make_message(user_id, "!   ")

    await workout.log_set_text(message, state)

    ex = await db.get_exercise(ex_id)
    assert ex["notes"] is None
    message.reply.assert_awaited_once()


# ---------- "N: 100 8" edit ----------


@pytest.mark.asyncio
async def test_indexed_edit_overwrites_that_set_only(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id, [(100.0, 8), (100.0, 7), (95.0, 8)])
    message = _make_message(user_id, "2: 105 6")

    await workout.log_set_text(message, state)

    sets = await db.list_sets_for_block(block_id)
    assert [(s["weight"], s["reps"]) for s in sets] == [(100.0, 8), (105.0, 6), (95.0, 8)]
    message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_indexed_edit_bare_reps_keeps_that_sets_own_weight(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id, [(100.0, 8), (90.0, 10)])
    message = _make_message(user_id, "2: 6")  # fix reps only, weight (90) unspecified

    await workout.log_set_text(message, state)

    sets = await db.list_sets_for_block(block_id)
    assert [(s["weight"], s["reps"]) for s in sets] == [(100.0, 8), (90.0, 6)]


@pytest.mark.asyncio
async def test_indexed_edit_out_of_range_replies_with_how_many_exist(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id, [(100.0, 8)])
    message = _make_message(user_id, "5: 100 8")

    await workout.log_set_text(message, state)

    sets = await db.list_sets_for_block(block_id)
    assert [(s["weight"], s["reps"]) for s in sets] == [(100.0, 8)]  # unchanged
    message.reply.assert_awaited_once()
    assert "5" in message.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_indexed_edit_counts_sets_across_a_reopened_exercise(fresh_db, user_id):
    """Closing an exercise and adding it again later makes a second block for
    it, but the tracker still numbers all its sets as one list. The edit has to
    use that same numbering: counting inside the open block only would edit the
    wrong set and reject numbers the user can see on screen."""
    db = fresh_db
    state, ex_id, first_block = await _setup_logging(db, user_id, [(100.0, 8), (100.0, 8)])
    data = await state.get_data()

    # Reopened later in the same workout: a fresh block, one set in it.
    second_block = await db.create_block(data["workout_id"], "single")
    await db.add_block_exercise(second_block, ex_id, 0)
    await db.append_set(second_block, ex_id, 0, 60.0, 10)
    await state.update_data(open_blocks={ex_id: second_block})

    # On screen this is set 3 of 3 — it lives in the second block.
    await workout.log_set_text(_make_message(user_id, "3: 65 10"), state)

    assert [(s["weight"], s["reps"]) for s in await db.list_sets_for_block(first_block)] == [
        (100.0, 8), (100.0, 8),
    ]
    assert [(s["weight"], s["reps"]) for s in await db.list_sets_for_block(second_block)] == [
        (65.0, 10),
    ]

    # ...and set 1 is the first block's, not the open block's only set.
    await workout.log_set_text(_make_message(user_id, "1: 110 5"), state)

    assert [(s["weight"], s["reps"]) for s in await db.list_sets_for_block(first_block)] == [
        (110.0, 5), (100.0, 8),
    ]


@pytest.mark.asyncio
async def test_indexed_edit_rejects_a_count_suffix(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id, [(100.0, 8)])
    message = _make_message(user_id, "1: 100x8x3")

    await workout.log_set_text(message, state)

    sets = await db.list_sets_for_block(block_id)
    assert [(s["weight"], s["reps"]) for s in sets] == [(100.0, 8)]  # unchanged
    message.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_indexed_edit_updates_last_by_exercise_when_editing_the_newest_set(fresh_db, user_id):
    """A follow-up bare-reps set ("8") carries the weight forward from
    last_by_exercise — editing the newest set must keep that pointer in sync,
    or the next bare-reps set would silently use the pre-edit weight."""
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id, [(100.0, 8)])
    await workout.log_set_text(_make_message(user_id, "1: 110 8"), state)

    await workout.log_set_text(_make_message(user_id, "9"), state)  # bare reps

    sets = await db.list_sets_for_block(block_id)
    assert [(s["weight"], s["reps"]) for s in sets] == [(110.0, 8), (110.0, 9)]


@pytest.mark.asyncio
async def test_decimal_weight_is_not_mistaken_for_an_edit_command(fresh_db, user_id):
    """"2.5 8" is an ordinary 2.5kg set, not "edit set 2" — see parser.parse_set_edit."""
    db = fresh_db
    # Веса рядом с 2.5 намеренно: подход втрое легче сегодняшних сначала
    # спросит «2.5кг? сегодня 100кг» (workout._suspicious_weight_warning), а
    # здесь проверяется разбор строки, а не подтверждение.
    state, ex_id, block_id = await _setup_logging(db, user_id, [(5.0, 8), (5.0, 7)])
    message = _make_message(user_id, "2.5 8")

    await workout.log_set_text(message, state)

    sets = await db.list_sets_for_block(block_id)
    assert [(s["weight"], s["reps"]) for s in sets] == [(5.0, 8), (5.0, 7), (2.5, 8)]


# ---------- "/help" and "?" ----------


@pytest.mark.asyncio
async def test_help_command_sends_the_reference(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id, [(100.0, 8)])
    message = _make_message(user_id, "/help")

    await workout.cmd_help(message, state)

    message.answer.assert_awaited_once()
    text, kwargs = message.answer.await_args.args[0], message.answer.await_args.kwargs
    assert kwargs.get("parse_mode") == "HTML"
    # The first screen stays short — the basics only, the rest is a tap away.
    for needle in ("100 8", "100 8 3", "-", "="):
        assert needle in text
    assert "@9" not in text and "2: 100 8" not in text
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "help:more"


@pytest.mark.asyncio
async def test_help_expands_to_the_full_reference_and_back(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id, [(100.0, 8)])
    callback = _make_callback(user_id, "help:more")

    await workout.help_toggle(callback, state)

    text, kwargs = (
        callback.message.edit_text.await_args.args[0],
        callback.message.edit_text.await_args.kwargs,
    )
    for needle in ("@9", "!болит плечо", "2: 100 8", "+20 8"):
        assert needle in text
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "help:less"

    callback.data = "help:less"
    await workout.help_toggle(callback, state)
    collapsed = callback.message.edit_text.await_args.args[0]
    assert "@9" not in collapsed


@pytest.mark.asyncio
async def test_question_mark_shows_help_without_touching_the_workout(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id = await _setup_logging(db, user_id, [(100.0, 8)])
    message = _make_message(user_id, "?")

    await workout.log_set_text(message, state)

    message.reply.assert_awaited_once()
    text = message.reply.await_args.args[0]
    assert "КАК ЗАПИСАТЬ ПОДХОД" in text
    assert message.reply.await_args.kwargs.get("parse_mode") == "HTML"
    # Nothing about the in-progress exercise changed — it's a pure lookup.
    assert len(await db.list_sets_for_block(block_id)) == 1
    message.delete.assert_not_awaited()
