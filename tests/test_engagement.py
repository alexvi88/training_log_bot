"""Pure signal-detection logic behind the daily engagement pushes."""

import datetime as dt

import analytics
import engagement
from analytics import SessionStats, SetRow


def d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


# ---------- streak at risk ----------


def test_streak_at_risk_only_on_weekend():
    dashboard = analytics.Dashboard(total_workouts=10, this_week=0, last_30_days=4, days_since_last=3, week_streak=3)
    monday = d("2026-07-06")  # Monday
    saturday = d("2026-07-11")
    assert engagement.is_streak_at_risk(dashboard, monday) is False
    assert engagement.is_streak_at_risk(dashboard, saturday) is True


def test_streak_at_risk_requires_streak_and_empty_week():
    saturday = d("2026-07-11")
    no_streak = analytics.Dashboard(10, 0, 4, 3, week_streak=1)
    already_trained = analytics.Dashboard(10, 1, 4, 0, week_streak=3)
    assert engagement.is_streak_at_risk(no_streak, saturday) is False
    assert engagement.is_streak_at_risk(already_trained, saturday) is False


# ---------- skip milestones ----------


def test_skip_milestone_matches_only_exact_days():
    assert engagement.skip_milestone(3) == 3
    assert engagement.skip_milestone(7) == 7
    assert engagement.skip_milestone(14) == 14
    assert engagement.skip_milestone(4) is None
    assert engagement.skip_milestone(1) is None
    assert engagement.skip_milestone(21) is None
    assert engagement.skip_milestone(None) is None


# ---------- win-back ----------


def test_win_back_starts_at_21_then_every_10_days():
    assert engagement.is_win_back_day(20) is False
    assert engagement.is_win_back_day(21) is True
    assert engagement.is_win_back_day(25) is False
    assert engagement.is_win_back_day(31) is True
    assert engagement.is_win_back_day(41) is True
    assert engagement.is_win_back_day(None) is False


# ---------- usual training weekday ----------


def test_usual_weekday_needs_enough_history():
    few_tuesdays = [d("2026-06-30"), d("2026-07-07")]
    assert engagement.usual_weekday(few_tuesdays) is None


def test_usual_weekday_picks_the_mode():
    # 6 Tuesdays, 4 Fridays -> Tuesday (weekday() == 1) wins
    tuesdays = [d(f"2026-06-{day:02d}") for day in (2, 9, 16, 23, 30)]
    fridays = [d(f"2026-06-{day:02d}") for day in (5, 12, 19, 26)]
    extra_tuesday = [d("2026-07-07")]
    dates = tuesdays + fridays + extra_tuesday
    assert engagement.usual_weekday(dates) == dt.date(2026, 6, 2).weekday()


# ---------- plateau ----------


def _session(weight: float, reps_per_set: list[int]) -> SessionStats:
    return SessionStats(
        workout_id=1, started_at="2026-07-01T10:00:00",
        sets=[SetRow(weight, r) for r in reps_per_set],
    )


def test_plateau_needs_three_sessions():
    sessions = [_session(60, [12, 12]), _session(60, [12, 12])]
    assert engagement.is_plateau(sessions) is False


def test_plateau_true_when_weight_stuck_and_reps_high():
    sessions = [_session(60, [12, 13]), _session(60, [14, 12]), _session(60, [12, 12])]
    assert engagement.is_plateau(sessions) is True


def test_plateau_false_when_weight_progressed():
    sessions = [_session(60, [12, 12]), _session(62.5, [12, 12]), _session(65, [12, 12])]
    assert engagement.is_plateau(sessions) is False


def test_plateau_false_when_reps_below_threshold():
    # same weight three times, but reps are low -> genuinely still working up to it, not a plateau
    sessions = [_session(60, [8, 8]), _session(60, [9, 8]), _session(60, [8, 9])]
    assert engagement.is_plateau(sessions) is False


def test_plateau_ignores_bodyweight_zero_weight():
    sessions = [_session(0, [15, 15]), _session(0, [16, 15]), _session(0, [15, 16])]
    assert engagement.is_plateau(sessions) is False


def test_plateau_only_looks_at_the_last_three_sessions():
    # an old plateau that was already broken shouldn't retrigger
    sessions = [_session(60, [12, 12]), _session(60, [12, 12]), _session(65, [12, 12]), _session(70, [12, 12])]
    assert engagement.is_plateau(sessions) is False


# ---------- tonnage formatting ----------


def test_format_tonnage_switches_units_at_1000kg():
    assert engagement.format_tonnage(850) == "850 кг"
    assert engagement.format_tonnage(1000) == "1.0 т"
    assert engagement.format_tonnage(4200) == "4.2 т"


# ---------- quiet hours ----------


def test_quiet_hours_span_23_to_8():
    assert engagement.in_quiet_hours(dt.datetime(2026, 7, 12, 23, 30)) is True
    assert engagement.in_quiet_hours(dt.datetime(2026, 7, 12, 2, 0)) is True
    assert engagement.in_quiet_hours(dt.datetime(2026, 7, 12, 7, 59)) is True
    assert engagement.in_quiet_hours(dt.datetime(2026, 7, 12, 8, 0)) is False
    assert engagement.in_quiet_hours(dt.datetime(2026, 7, 12, 19, 0)) is False
