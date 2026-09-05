"""История по месяцам: календарь на history_list, marks по db, тап по дню.

30 тренировок назад — 4 тапа по листалке (8/страница), 1 тап по календарю. Эти
тесты — по db.list_finished_workouts_by_day_in_month, keyboards.calendar_keyboard
(marked/show_quick_dates/back_text/back_cb) и handlers.history's hist:cal:*."""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery

import i18n
import keyboards
from handlers import history


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.text = "some previous screen"
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.edit_text = AsyncMock(return_value=message)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    message.delete = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _log_workout(db, user_id: int, exercise_id: int, started_at: str) -> int:
    workout_id = await db.create_workout(user_id, started_at=started_at)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, exercise_id, 0)
    await db.add_set(block_id, exercise_id, 1, 0, 60.0, 8)
    await db.finish_workout(workout_id, finished_at=started_at)
    return workout_id


# ---------- keyboards.calendar_keyboard: marks, quick row, back override ----------


def _all_buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def test_marked_day_gets_a_bullet_unmarked_does_not():
    """show_quick_dates=False so the Today/Yesterday row can't shadow a grid
    cell with the same callback_data (both would otherwise target the same
    date, and a dict keyed by callback_data would only keep one label)."""
    today = dt.date(2026, 9, 5)
    kb = keyboards.calendar_keyboard(
        "hist:cal", 2026, 9, today=today, marked={"2026-09-03"}, show_quick_dates=False
    )
    labels = {b.callback_data: b.text for b in _all_buttons(kb)}
    assert labels["hist:cal:date:2026-09-03"] == "🏋️3"
    assert labels["hist:cal:date:2026-09-04"] == "4"


def test_marked_today_shows_the_icon_instead_of_the_today_dots():
    today = dt.date(2026, 9, 5)
    kb = keyboards.calendar_keyboard(
        "hist:cal", 2026, 9, today=today, marked={"2026-09-05"}, show_quick_dates=False
    )
    labels = {b.callback_data: b.text for b in _all_buttons(kb)}
    assert labels["hist:cal:date:2026-09-05"] == "🏋️5"


def test_show_quick_dates_false_hides_today_yesterday_row():
    today = dt.date(2026, 9, 5)
    kb = keyboards.calendar_keyboard("hist:cal", 2026, 9, today=today, show_quick_dates=False)
    cbs = [b.callback_data for b in _all_buttons(kb)]
    assert "hist:cal:date:2026-09-05" in cbs  # the grid cell itself still works
    # But no *second* button targets today/yesterday via the quick row — the
    # calendar grid already carries a callback for every selectable day, so
    # absence of the quick row is checked by button count, not by data value.
    texts = [b.text for b in _all_buttons(kb)]
    assert i18n.t("btn.today_plain") not in texts
    assert i18n.t("btn.yesterday") not in texts


def test_back_text_and_cb_override_the_default_cancel():
    kb = keyboards.calendar_keyboard(
        "hist:cal", 2026, 9, today=dt.date(2026, 9, 5),
        back_text=i18n.t("btn.to_list"), back_cb="hist:back",
    )
    cbs = [b.callback_data for b in _all_buttons(kb)]
    texts = [b.text for b in _all_buttons(kb)]
    assert "hist:back" in cbs
    assert "hist:cal:cancel" not in cbs
    assert i18n.t("btn.to_list") in texts


def test_backfill_calendar_unaffected_by_new_params():
    """Defaults keep bf's own screen exactly as before — marked=None, quick
    row shown, cancel targets {prefix}:cancel."""
    kb = keyboards.calendar_keyboard("bf", 2026, 7, today=dt.date(2026, 7, 23))
    cbs = [b.callback_data for b in _all_buttons(kb)]
    assert "bf:date:2026-07-23" in cbs
    assert "bf:cancel" in cbs


# ---------- history_list_keyboard: the "📅 По месяцам" entry point ----------


def test_history_list_has_by_month_button_when_not_empty():
    kb = keyboards.history_list_keyboard([{"id": 1, "label": "Sep 5"}], page=0, has_next=False)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "hist:cal" in cbs


def test_empty_history_swaps_calendar_for_start_workout():
    kb = keyboards.history_list_keyboard([], page=0, has_next=False, is_empty=True)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "hist:cal" not in cbs
    assert "menu:start_workout" in cbs
    assert i18n.t("history.start_workout_button") in texts
    # Backfill entry stays reachable either way.
    assert "menu:backfill_workout" in cbs


# ---------- db.list_finished_workouts_by_day_in_month ----------


async def test_db_groups_workout_ids_by_local_day(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    squat = await db.create_exercise(user_id, "Присед", group_id)
    w1 = await _log_workout(db, user_id, squat, "2026-09-03T10:00:00")
    w2 = await _log_workout(db, user_id, squat, "2026-09-03T18:00:00")
    w3 = await _log_workout(db, user_id, squat, "2026-09-10T10:00:00")
    # Another month — must not leak into September's result.
    await _log_workout(db, user_id, squat, "2026-08-31T10:00:00")

    by_day = await db.list_finished_workouts_by_day_in_month(user_id, 2026, 9)

    assert set(by_day["2026-09-03"]) == {w1, w2}
    assert by_day["2026-09-10"] == [w3]
    assert "2026-08-31" not in by_day


# ---------- handlers.history: tapping a calendar day ----------


async def test_tap_marked_day_with_one_workout_opens_the_card(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    row = await db.create_exercise(user_id, "Тяга", group_id)
    await _log_workout(db, user_id, row, "2026-09-03T10:00:00")

    callback = _make_callback(user_id, "hist:cal:date:2026-09-03")
    await history.hist_calendar_day(callback, state=AsyncMock())

    text = callback.message.answer.await_args.args[0]
    assert "Тяга" in text
    callback.answer.assert_awaited()
    # Single-workout day opens straight to the card, not a toast.
    assert callback.answer.await_args.args == () or callback.answer.await_args.args == (None,)


async def test_tap_marked_day_with_two_workouts_shows_a_day_list(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    row = await db.create_exercise(user_id, "Тяга", group_id)
    w1 = await _log_workout(db, user_id, row, "2026-09-03T10:00:00")
    w2 = await _log_workout(db, user_id, row, "2026-09-03T18:00:00")

    callback = _make_callback(user_id, "hist:cal:date:2026-09-03")
    await history.hist_calendar_day(callback, state=AsyncMock())

    text = callback.message.answer.await_args.args[0]
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    cbs = [b.callback_data for row_ in kb.inline_keyboard for b in row_]
    assert f"hist:item:{w1}" in cbs
    assert f"hist:item:{w2}" in cbs
    assert "10:00" in text or "10:00" in "".join(
        b.text for row_ in kb.inline_keyboard for b in row_
    )


async def test_tap_unmarked_day_shows_a_toast_not_a_screen(fresh_db, user_id):
    callback = _make_callback(user_id, "hist:cal:date:2026-09-03")
    await history.hist_calendar_day(callback, state=AsyncMock())

    callback.answer.assert_awaited_once_with(i18n.t("history.calendar_day_empty"))
    callback.message.answer.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()


# ---------- handlers.history: month navigation ----------


async def test_calendar_open_shows_current_month_and_marks(fresh_db, user_id, monkeypatch):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    squat = await db.create_exercise(user_id, "Присед", group_id)
    await _log_workout(db, user_id, squat, "2026-09-03T10:00:00")
    monkeypatch.setattr(
        history.timeutil, "user_today", lambda user: dt.date(2026, 9, 5)
    )

    callback = _make_callback(user_id, "hist:cal")
    state = AsyncMock()
    await history.hist_calendar_open(callback, state)

    kb = callback.message.answer.await_args.kwargs.get("reply_markup") or (
        callback.message.edit_text.await_args.kwargs.get("reply_markup")
    )
    cbs = [b.callback_data for row_ in kb.inline_keyboard for b in row_]
    labels = {b.callback_data: b.text for row_ in kb.inline_keyboard for b in row_}
    assert labels["hist:cal:date:2026-09-03"] == "🏋️3"
    assert "hist:back" in cbs  # "⬅️ К списку" replaces the generic cancel


async def test_calendar_nav_moves_month_and_keeps_marks(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    squat = await db.create_exercise(user_id, "Присед", group_id)
    await _log_workout(db, user_id, squat, "2026-08-15T10:00:00")

    callback = _make_callback(user_id, "hist:cal:cal:2026-8")
    await history.hist_calendar_nav(callback, state=AsyncMock())

    kb = callback.message.answer.await_args.kwargs.get("reply_markup") or (
        callback.message.edit_text.await_args.kwargs.get("reply_markup")
    )
    labels = {b.callback_data: b.text for row_ in kb.inline_keyboard for b in row_}
    assert labels["hist:cal:date:2026-08-15"] == "🏋️15"
