"""/start warns about a long-abandoned workout and offers one-tap actions to
resolve it (finish retroactively, or delete) instead of leaving the user to
figure out how to close it themselves.
"""
import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import config
from handlers import workout

pytestmark = pytest.mark.asyncio


def _make_message(user_id: int):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester")
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=999))
    return message


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1, chat=SimpleNamespace(id=user_id)))
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


async def test_start_warns_and_offers_buttons_for_stale_workout(fresh_db, user_id):
    db = fresh_db
    stale_started = dt.datetime.now() - dt.timedelta(hours=config.STALE_WORKOUT_HOURS + 1)
    workout_id = await db.create_workout(user_id, started_at=stale_started.isoformat())

    message = _make_message(user_id)
    state = await _make_state(user_id)

    await workout.cmd_start(message, state)

    assert message.answer.await_count == 2
    warning_call = message.answer.await_args_list[1]
    assert "висит тренировка" in warning_call.args[0]
    kb = warning_call.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"stale:finish:{workout_id}" in callback_datas
    assert f"stale:delete:{workout_id}" in callback_datas


async def test_stale_workout_warning_shows_what_was_logged(fresh_db, user_id):
    """Раньше «завершить задним числом» или «удалить» решали вслепую, уже не
    помня, что вообще успели записать. Состав — без HTML-тегов буквами: у
    сообщения нет parse_mode (entities заняты под часовой пояс в дате), так
    что <b>/<i> отрендерились бы как есть."""
    db = fresh_db
    stale_started = dt.datetime.now() - dt.timedelta(hours=config.STALE_WORKOUT_HOURS + 1)
    workout_id = await db.create_workout(user_id, started_at=stale_started.isoformat())
    group = await db.create_muscle_group(user_id, "Грудь")
    ex = await db.create_exercise(user_id, "Жим лёжа", group)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex, 0)
    await db.append_set(block_id, ex, 0, 100.0, 8)

    message = _make_message(user_id)
    state = await _make_state(user_id)

    await workout.cmd_start(message, state)

    warning = message.answer.await_args_list[1].args[0]
    assert "Жим лёжа" in warning
    assert "Грудь" in warning
    assert "100" in warning and "8" in warning
    assert "<b>" not in warning and "<i>" not in warning


async def test_start_does_not_warn_for_recent_workout(fresh_db, user_id):
    db = fresh_db
    await db.create_workout(user_id)

    message = _make_message(user_id)
    state = await _make_state(user_id)

    await workout.cmd_start(message, state)

    assert message.answer.await_count == 1


async def test_stale_finish_marks_workout_finished_with_original_date(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    started = dt.datetime.now() - dt.timedelta(hours=config.STALE_WORKOUT_HOURS + 1)
    workout_id = await db.create_workout(user_id, started_at=started.isoformat())
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)
    await db.add_set(block_id, bench, 1, 0, 100, 8)

    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"stale:finish:{workout_id}")

    await workout.stale_finish_workout(callback, state)

    saved = await db.get_workout(workout_id)
    assert saved["status"] == "finished"
    assert saved["finished_at"] == started.isoformat()
    assert "Закрыл тренировку задним числом" in callback.message.answer.await_args.args[0]


async def test_stale_finish_awards_achievements_for_the_workout(fresh_db, user_id):
    """Finishing retroactively bypasses _finalize_workout, so nothing else would
    evaluate badges: the workout counts toward streaks, tonnage and weight clubs
    the moment it is finished, but the grid stayed empty until some later
    workout happened to trigger an evaluation."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    squat = await db.create_exercise(user_id, "Присед", group_id)
    started = dt.datetime.now() - dt.timedelta(hours=config.STALE_WORKOUT_HOURS + 1)
    workout_id = await db.create_workout(user_id, started_at=started.isoformat())
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, squat, 0)
    await db.add_set(block_id, squat, 1, 0, 150, 5)

    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"stale:finish:{workout_id}")

    await workout.stale_finish_workout(callback, state)

    codes = await db.list_achievement_codes(user_id)
    assert "first" in codes
    assert "club100" in codes


async def test_stale_finish_discards_empty_workout(fresh_db, user_id):
    db = fresh_db
    started = dt.datetime.now() - dt.timedelta(hours=config.STALE_WORKOUT_HOURS + 1)
    workout_id = await db.create_workout(user_id, started_at=started.isoformat())

    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"stale:finish:{workout_id}")

    await workout.stale_finish_workout(callback, state)

    assert await db.get_workout(workout_id) is None


async def test_start_workout_creates_and_enters_picker_immediately(fresh_db, user_id):
    db = fresh_db
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "menu:start_workout")

    await workout.start_workout(callback, state)

    active = await db.get_active_workout(user_id)
    assert active is not None
    data = await state.get_data()
    assert data["workout_id"] == active["id"]

    kb = callback.bot.send_message.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "rt:manage" in callback_datas
    assert "pick:cancel" in callback_datas


async def test_start_workout_resets_stale_fsm_scaffold(fresh_db, user_id):
    """Regression for a bug where finishing a stale workout retroactively and
    then starting a new one left the previous workout's open-exercise
    scaffolding (`open_exercises`/`open_blocks`) in the FSM. A set logged into
    that phantom "open" tab in the new workout was silently written into the
    old (already finished) workout's block instead."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    old_workout_id = await db.create_workout(user_id)
    old_block_id = await db.create_block(old_workout_id, "single")
    await db.add_block_exercise(old_block_id, bench, 0)
    await db.add_set(old_block_id, bench, 1, 0, 100, 8)

    state = await _make_state(user_id)
    # Simulates the leftover scaffolding _clear_state_keep_workout preserves
    # across a trip to the menu, pointing at the now-finished workout's block.
    await state.update_data(
        workout_id=old_workout_id, open_exercises=[bench], active_exercise_id=bench,
        open_blocks={bench: old_block_id}, last_by_exercise={bench: (100, 8, None)},
        last_session_sets={bench: [(100, 8, None)]}, weight_steps={bench: 2.5},
        confirmed_weights={bench: 100}, exercise_targets={bench: "3x8"},
    )
    await db.finish_workout(old_workout_id)

    callback = _make_callback(user_id, "menu:start_workout")
    await workout.start_workout(callback, state)

    data = await state.get_data()
    new_workout_id = data["workout_id"]
    assert new_workout_id != old_workout_id
    assert not data.get("open_exercises")
    assert not data.get("open_blocks")
    assert data.get("active_exercise_id") is None
    assert not data.get("last_session_sets")
    assert not data.get("weight_steps")
    assert not data.get("confirmed_weights")
    assert not data.get("exercise_targets")


async def test_stale_delete_requires_confirmation_then_deletes(fresh_db, user_id):
    db = fresh_db
    started = dt.datetime.now() - dt.timedelta(hours=config.STALE_WORKOUT_HOURS + 1)
    workout_id = await db.create_workout(user_id, started_at=started.isoformat())

    state = await _make_state(user_id)
    confirm_callback = _make_callback(user_id, f"stale:delete:{workout_id}")
    await workout.stale_delete_confirm(confirm_callback, state)

    kb = confirm_callback.message.answer.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"stale:delyes:{workout_id}" in callback_datas

    # Still there until the yes button is actually tapped.
    assert await db.get_workout(workout_id) is not None

    delete_callback = _make_callback(user_id, f"stale:delyes:{workout_id}")
    await workout.stale_delete(delete_callback, state)

    assert await db.get_workout(workout_id) is None


async def test_double_tap_on_start_opens_one_workout_not_two(fresh_db, user_id):
    """aiogram handles updates concurrently, so two taps both used to see "no
    active workout" and create one each. The loser became a permanent ghost:
    an empty active workout that "Продолжить" might open instead of the real
    one, and that resurfaces later as a stale-workout warning."""
    db = fresh_db

    first, second = await asyncio.gather(
        db.get_or_create_active_workout(user_id),
        db.get_or_create_active_workout(user_id),
    )

    assert first[0] == second[0]
    assert [first[1], second[1]].count(True) == 1  # exactly one of them created it
    cur = await db.conn().execute(
        "SELECT COUNT(*) FROM workouts WHERE user_id = ? AND status = 'active'", (user_id,)
    )
    assert (await cur.fetchone())[0] == 1


async def test_finish_workout_only_succeeds_once(fresh_db, user_id):
    """The status guard in _finalize_workout is several awaits away from the
    UPDATE, which is enough of a window for a second tap to slip past it and
    build a duplicate finish card."""
    db = fresh_db
    workout_id = await db.create_workout(user_id)

    assert await db.finish_workout(workout_id) is True
    assert await db.finish_workout(workout_id) is False
