"""'🔁 Повторить прошлую' — start a new workout pre-loaded with the last one's plan."""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import db as dbmod
import formatting
import keyboards
from fsm import WorkoutFlow
from handlers import workout


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    ids = iter(range(600, 700))

    async def _answer(*args, **kwargs):
        return SimpleNamespace(message_id=next(ids), chat=SimpleNamespace(id=user_id))

    message.answer = AsyncMock(side_effect=_answer)
    message.delete = AsyncMock()
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


async def _state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


async def _finished_workout(db, user_id, exercise_ids):
    wid = await db.create_workout(user_id)
    for ex_id in exercise_ids:
        block_id = await db.create_block(wid, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.add_set(block_id, ex_id, 0, 0, 100.0, 8, None)
    await db.finish_workout(wid, None)
    return wid


@pytest.mark.asyncio
async def test_repeat_picks_a_specific_past_workout(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", group_id)
    row = await db.create_exercise(user_id, "Тяга", group_id)
    squat = await db.create_exercise(user_id, "Присед", group_id)
    # Two finished workouts — the user should be able to repeat the older one,
    # not just the most recent.
    older = await _finished_workout(db, user_id, [bench, row])
    await _finished_workout(db, user_id, [squat])

    state = await _state(user_id)
    # Repeat now lives inside the fresh-workout picker: start a workout first,
    # then tap "🔁 Повторить прошлую" to open the list of past workouts.
    await workout.start_workout(_make_callback(user_id, "menu:start_workout"), state)
    await workout.pick_repeat_last(_make_callback(user_id, "pick:repeat"), state)

    # The list offers a "show preview" button per past workout.
    list_cb = _make_callback(user_id, "pick:repeat")
    await workout._repeat_list_screen(list_cb, state, page=0)
    kb = list_cb.bot.send_message.await_args.kwargs["reply_markup"]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"pick:rep:show:{older}" in cbs

    # Preview the older workout, then repeat it.
    await workout.pick_repeat_show(_make_callback(user_id, f"pick:rep:show:{older}"), state)
    await workout.pick_repeat_use(_make_callback(user_id, f"pick:rep:use:{older}"), state)

    data = await state.get_data()
    # First block of the CHOSEN workout opened for logging, the rest queued.
    assert await state.get_state() == WorkoutFlow.logging_set
    assert data["open_exercises"] == [bench]
    assert data["planned_blocks"] == [{"exercise_ids": [row]}]


@pytest.mark.asyncio
async def test_repeat_list_keeps_button_labels_short_with_dash_separator(fresh_db, user_id):
    """Buttons carry only a number + date ("1 - <date>") since full exercise
    names don't fit into a button label. The text above them still lists every
    exercise (bulleted, muscle group in brackets) under a bold number+date title
    — nothing is truncated, just laid out so it's scannable."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    a = await db.create_exercise(user_id, "Приседания со штангой на плечах", group_id)
    b = await db.create_exercise(user_id, "Жим штанги лёжа широким хватом", group_id)
    c = await db.create_exercise(user_id, "Тяга штанги в наклоне", group_id)
    wid = await _finished_workout(db, user_id, [a, b, c])

    state = await _state(user_id)
    await workout.start_workout(_make_callback(user_id, "menu:start_workout"), state)

    list_cb = _make_callback(user_id, "pick:repeat")
    await workout._repeat_list_screen(list_cb, state, page=0)

    workout_row = await dbmod.get_workout(wid)
    expected_date = formatting.format_date_ru(dt.datetime.fromisoformat(workout_row["started_at"]))

    kb = list_cb.bot.send_message.await_args.kwargs["reply_markup"]
    button = next(b for row in kb.inline_keyboard for b in row if b.callback_data == f"pick:rep:show:{wid}")
    assert button.text == f"1 - {expected_date}"

    text = list_cb.bot.send_message.await_args.kwargs["text"]
    assert f"<b>1 · {expected_date}</b>" in text
    assert "• Приседания со штангой на плечах [ГРУДЬ]" in text
    assert "• Жим штанги лёжа широким хватом [ГРУДЬ]" in text
    # Nothing gets folded behind a "+N" anymore — every exercise gets its own line.
    assert "• Тяга штанги в наклоне [ГРУДЬ]" in text
    assert text.startswith("🔁 Выбери тренировку, чтобы повторить её план:")


@pytest.mark.asyncio
async def test_repeat_last_without_history_is_gentle(fresh_db, user_id):
    state = await _state(user_id)
    await state.set_state(WorkoutFlow.picking_group)
    callback = _make_callback(user_id, "pick:repeat")

    await workout.pick_repeat_last(callback, state)

    callback.answer.assert_any_await("Нет прошлой тренировки для повтора", show_alert=True)


@pytest.mark.asyncio
async def test_repeat_button_offered_in_picker_only_with_history(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", group_id)

    # No finished workout yet: the fresh-workout picker omits the repeat button.
    state = await _state(user_id)
    callback = _make_callback(user_id, "menu:start_workout")
    await workout.start_workout(callback, state)
    kb = callback.bot.send_message.await_args.kwargs["reply_markup"]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "pick:repeat" not in cbs

    # Discard that still-active workout so the next start_workout call below
    # goes through the create-and-pick path again instead of resuming it.
    active = await db.get_active_workout(user_id)
    await db.discard_workout(active["id"])

    # After a finished workout, the same screen offers it.
    await _finished_workout(db, user_id, [bench])
    state2 = await _state(user_id)
    callback2 = _make_callback(user_id, "menu:start_workout")
    await workout.start_workout(callback2, state2)
    kb2 = callback2.bot.send_message.await_args.kwargs["reply_markup"]
    cbs2 = [b.callback_data for row in kb2.inline_keyboard for b in row]
    assert "pick:repeat" in cbs2


def test_main_menu_no_longer_carries_repeat_or_hall():
    for active in (False, True):
        kb = keyboards.main_menu(has_active_workout=active)
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert "menu:repeat_last" not in cbs
        assert "menu:hall" not in cbs
