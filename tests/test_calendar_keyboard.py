"""keyboards.calendar_keyboard — inline month grid for picking a past date."""
import datetime as dt

import keyboards


def _all_buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def test_day_taps_emit_date_callback():
    today = dt.date(2026, 7, 23)
    kb = keyboards.calendar_keyboard("bf", 2026, 7, today=today)
    cbs = [b.callback_data for b in _all_buttons(kb)]
    assert "bf:date:2026-07-01" in cbs
    assert "bf:date:2026-07-23" in cbs  # today is selectable


def test_future_days_are_not_selectable():
    today = dt.date(2026, 7, 23)
    kb = keyboards.calendar_keyboard("bf", 2026, 7, today=today)
    cbs = [b.callback_data for b in _all_buttons(kb)]
    assert "bf:date:2026-07-24" not in cbs  # tomorrow
    assert "bf:date:2026-07-31" not in cbs


def test_next_arrow_disabled_in_current_month():
    today = dt.date(2026, 7, 23)
    kb = keyboards.calendar_keyboard("bf", 2026, 7, today=today)
    cbs = [b.callback_data for b in _all_buttons(kb)]
    # No navigation into August (the future) — the › slot is a noop.
    assert not any(c == "bf:cal:2026-8" for c in cbs)
    assert "bf:cal:2026-6" in cbs  # but you can go back to June


def test_past_month_allows_forward_nav_and_full_month():
    today = dt.date(2026, 7, 23)
    kb = keyboards.calendar_keyboard("bf", 2026, 6, today=today)
    cbs = [b.callback_data for b in _all_buttons(kb)]
    assert "bf:cal:2026-7" in cbs  # forward into July is allowed
    assert "bf:date:2026-06-30" in cbs  # all of June is in the past → selectable


def test_quick_and_cancel_present():
    today = dt.date(2026, 7, 23)
    kb = keyboards.calendar_keyboard("findate", 2026, 7, today=today)
    cbs = [b.callback_data for b in _all_buttons(kb)]
    assert "findate:date:2026-07-23" in cbs  # Сегодня
    assert "findate:date:2026-07-22" in cbs  # Вчера
    assert "findate:cancel" in cbs


def test_year_boundary_backward_nav():
    today = dt.date(2026, 7, 23)
    kb = keyboards.calendar_keyboard("bf", 2026, 1, today=today)
    cbs = [b.callback_data for b in _all_buttons(kb)]
    assert "bf:cal:2025-12" in cbs
