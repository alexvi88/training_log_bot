"""Pure-Python metrics: e1RM formulas, sessions, trend regression, PR detection."""

import datetime as dt

import pytest

import analytics
from analytics import NewRecord, SessionStats, SetRow

# ---------- e1RM formulas ----------


def test_epley_e1rm_single_rep_returns_weight():
    assert analytics.epley_e1rm(100, 1) == 100
    assert analytics.epley_e1rm(100, 0) == 100


def test_epley_e1rm_formula():
    assert analytics.epley_e1rm(100, 10) == pytest.approx(133.333, abs=1e-3)


def test_brzycki_e1rm_single_rep_returns_weight():
    assert analytics.brzycki_e1rm(100, 1) == 100


def test_brzycki_e1rm_at_or_above_37_reps_returns_weight():
    # formula divides by (37 - reps); the function special-cases reps >= 37
    # instead of blowing up or going negative.
    assert analytics.brzycki_e1rm(100, 37) == 100
    assert analytics.brzycki_e1rm(100, 50) == 100


def test_brzycki_e1rm_formula():
    assert analytics.brzycki_e1rm(100, 10) == pytest.approx(133.333, abs=1e-3)


def test_e1rm_dispatches_by_formula_name():
    assert analytics.e1rm(100, 10, "epley") == analytics.epley_e1rm(100, 10)
    assert analytics.e1rm(100, 10, "brzycki") == analytics.brzycki_e1rm(100, 10)


def test_e1rm_defaults_to_epley():
    assert analytics.e1rm(100, 10) == analytics.epley_e1rm(100, 10)


# ---------- SessionStats ----------


def test_session_stats_tonnage_and_total_reps():
    s = SessionStats(1, "2026-06-01T10:00:00", [SetRow(100, 8), SetRow(80, 10)])
    assert s.tonnage == 100 * 8 + 80 * 10
    assert s.total_reps == 18


def test_session_stats_empty_sets():
    s = SessionStats(1, "2026-06-01T10:00:00", [])
    assert s.tonnage == 0
    assert s.total_reps == 0
    assert s.top_set is None
    assert s.top_e1rm == 0.0
    assert s.max_reps_in_set == 0
    assert s.is_bodyweight_mode is False  # nothing logged yet, not "bodyweight mode"


def test_session_stats_is_bodyweight_mode_when_all_weights_zero():
    s = SessionStats(1, "2026-06-01T10:00:00", [SetRow(0, 12), SetRow(0, 10)])
    assert s.is_bodyweight_mode is True


def test_session_stats_not_bodyweight_when_any_weight_set():
    s = SessionStats(1, "2026-06-01T10:00:00", [SetRow(0, 12), SetRow(20, 10)])
    assert s.is_bodyweight_mode is False


def test_session_stats_top_set_picks_highest_e1rm_for_weighted():
    s = SessionStats(1, "2026-06-01T10:00:00", [SetRow(100, 5), SetRow(60, 12)])
    # 100x5 -> e1rm 116.67, 60x12 -> e1rm 84 — the heavier set wins.
    assert s.top_set == SetRow(100, 5)
    assert s.top_e1rm == pytest.approx(analytics.epley_e1rm(100, 5))


def test_session_stats_top_set_picks_highest_reps_for_bodyweight():
    s = SessionStats(1, "2026-06-01T10:00:00", [SetRow(0, 8), SetRow(0, 15)])
    assert s.top_set == SetRow(0, 15)


def test_session_stats_max_reps_in_set():
    s = SessionStats(1, "2026-06-01T10:00:00", [SetRow(100, 5), SetRow(60, 12)])
    assert s.max_reps_in_set == 12


# ---------- group_sets_by_session ----------


def test_group_sets_by_session_groups_and_sorts():
    rows = [
        SetRow(100, 8, workout_id=2, started_at="2026-06-08T10:00:00"),
        SetRow(80, 10, workout_id=1, started_at="2026-06-01T10:00:00"),
        SetRow(105, 6, workout_id=2, started_at="2026-06-08T10:00:00"),
    ]
    sessions = analytics.group_sets_by_session(rows)
    assert [s.workout_id for s in sessions] == [1, 2]
    assert len(sessions[0].sets) == 1
    assert len(sessions[1].sets) == 2


# ---------- linear_trend ----------


def test_linear_trend_needs_at_least_two_points():
    assert analytics.linear_trend([]) is None
    assert analytics.linear_trend([(dt.datetime(2026, 6, 1), 100.0)]) is None


def test_linear_trend_detects_upward_slope():
    points = [(dt.datetime(2026, 6, 1), 100.0), (dt.datetime(2026, 6, 8), 107.0)]
    trend = analytics.linear_trend(points)
    assert trend.direction == "up"
    assert trend.slope_per_week == pytest.approx(7.0)
    assert trend.intercept == pytest.approx(100.0)


def test_linear_trend_detects_downward_slope():
    points = [(dt.datetime(2026, 6, 1), 110.0), (dt.datetime(2026, 6, 8), 100.0)]
    trend = analytics.linear_trend(points)
    assert trend.direction == "down"
    assert trend.slope_per_week == pytest.approx(-10.0)


def test_linear_trend_flat_when_unchanged():
    points = [(dt.datetime(2026, 6, 1), 100.0), (dt.datetime(2026, 6, 8), 100.0)]
    trend = analytics.linear_trend(points)
    assert trend.direction == "flat"
    assert trend.slope_per_week == pytest.approx(0.0)


def test_linear_trend_same_calendar_day_avoids_division_by_zero():
    # Two sessions logged hours apart on the same day must not blow up the
    # slope (x-values would otherwise be near-identical, not exactly equal).
    points = [
        (dt.datetime(2026, 6, 1, 9, 0), 100.0),
        (dt.datetime(2026, 6, 1, 18, 0), 200.0),
    ]
    trend = analytics.linear_trend(points)
    assert trend.direction == "flat"
    assert trend.slope_per_week == 0.0
    assert trend.intercept == pytest.approx(150.0)


# ---------- compute_personal_records ----------


def test_compute_personal_records_empty():
    pr = analytics.compute_personal_records([])
    assert pr.max_weight == 0.0
    assert pr.max_e1rm == 0.0
    assert pr.max_session_tonnage == 0.0
    assert pr.max_reps_at_weight == {}


def test_compute_personal_records_tracks_best_across_sessions():
    sessions = [
        SessionStats(1, "2026-06-01T10:00:00", [SetRow(100, 5), SetRow(80, 10)]),
        SessionStats(2, "2026-06-08T10:00:00", [SetRow(110, 3), SetRow(80, 12)]),
    ]
    pr = analytics.compute_personal_records(sessions)
    assert pr.max_weight == 110
    assert pr.max_reps_at_weight == {100: 5, 80: 12, 110: 3}
    assert pr.max_session_tonnage == max(sessions[0].tonnage, sessions[1].tonnage)
    # best e1rm should come from whichever set has the highest computed e1rm
    best = max(
        (s for sess in sessions for s in sess.sets),
        key=lambda s: analytics.epley_e1rm(s.weight, s.reps),
    )
    assert pr.best_e1rm_weight == best.weight
    assert pr.best_e1rm_reps == best.reps


# ---------- detect_new_records ----------


def test_detect_new_records_first_ever_session_is_a_record():
    new_session = SessionStats(1, "2026-06-01T10:00:00", [SetRow(100, 8)])
    records = analytics.detect_new_records([], new_session)
    kinds = {r.kind for r in records}
    assert kinds == {"e1rm", "reps_at_weight"}


def test_detect_new_records_no_pr_when_below_history():
    history = [SessionStats(1, "2026-06-01T10:00:00", [SetRow(120, 8)])]
    new_session = SessionStats(2, "2026-06-08T10:00:00", [SetRow(120, 5)])
    records = analytics.detect_new_records(history, new_session)
    assert records == []


def test_detect_new_records_new_weight_bucket_is_a_reps_pr_even_if_lighter():
    # max_reps_at_weight is tracked per distinct weight, so a lighter weight
    # never seen before still counts as a fresh reps record at that weight —
    # it just won't beat the heavier e1rm PR.
    history = [SessionStats(1, "2026-06-01T10:00:00", [SetRow(120, 8)])]
    new_session = SessionStats(2, "2026-06-08T10:00:00", [SetRow(100, 8)])
    records = analytics.detect_new_records(history, new_session)
    assert records == [NewRecord(kind="reps_at_weight", value=8, extra=100)]


def test_detect_new_records_e1rm_pr_detected():
    history = [SessionStats(1, "2026-06-01T10:00:00", [SetRow(100, 8)])]
    new_session = SessionStats(2, "2026-06-08T10:00:00", [SetRow(110, 8)])
    records = analytics.detect_new_records(history, new_session)
    e1rm_records = [r for r in records if r.kind == "e1rm"]
    assert len(e1rm_records) == 1
    assert e1rm_records[0].value == pytest.approx(analytics.epley_e1rm(110, 8))


def test_detect_new_records_reps_at_weight_pr_detected():
    history = [SessionStats(1, "2026-06-01T10:00:00", [SetRow(100, 5)])]
    new_session = SessionStats(2, "2026-06-08T10:00:00", [SetRow(100, 8)])
    records = analytics.detect_new_records(history, new_session)
    reps_records = [r for r in records if r.kind == "reps_at_weight"]
    assert reps_records == [NewRecord(kind="reps_at_weight", value=8, extra=100)]


def test_detect_new_records_drops_dominated_reps_record_by_weight():
    # 100x8 dominates 80x8 (same reps, heavier weight) within the same session.
    new_session = SessionStats(1, "2026-06-01T10:00:00", [SetRow(100, 8), SetRow(80, 8)])
    records = analytics.detect_new_records([], new_session)
    reps_records = [r for r in records if r.kind == "reps_at_weight"]
    assert reps_records == [NewRecord(kind="reps_at_weight", value=8, extra=100)]


def test_detect_new_records_drops_dominated_reps_record_by_reps():
    # 100x8 dominates 100x6 (same weight, more reps) within the same session.
    new_session = SessionStats(1, "2026-06-01T10:00:00", [SetRow(100, 6), SetRow(100, 8)])
    records = analytics.detect_new_records([], new_session)
    reps_records = [r for r in records if r.kind == "reps_at_weight"]
    assert reps_records == [NewRecord(kind="reps_at_weight", value=8, extra=100)]


def test_detect_new_records_dominated_by_a_weight_already_matched_in_history():
    # 210x3 was already a PR from a past session, so it isn't itself "new" today,
    # but it's still a set performed in this session and should suppress the
    # lighter 205x3 (new weight bucket, but strictly worse than 210x3 done today).
    history = [SessionStats(1, "2026-06-01T10:00:00", [SetRow(210, 3)])]
    new_session = SessionStats(
        2, "2026-06-08T10:00:00", [SetRow(190, 3), SetRow(205, 3), SetRow(210, 3)]
    )
    records = analytics.detect_new_records(history, new_session)
    reps_records = [r for r in records if r.kind == "reps_at_weight"]
    assert reps_records == []


# ---------- compare_to_previous_session ----------


def test_compare_to_previous_session_needs_two_sessions():
    s = SessionStats(1, "2026-06-01T10:00:00", [SetRow(100, 8)])
    assert analytics.compare_to_previous_session([s]) is None
    assert analytics.compare_to_previous_session([]) is None


def test_compare_to_previous_session_computes_deltas():
    prev = SessionStats(1, "2026-06-01T10:00:00", [SetRow(100, 8)])
    curr = SessionStats(2, "2026-06-08T10:00:00", [SetRow(110, 8)])
    delta = analytics.compare_to_previous_session([prev, curr])
    assert delta.prev_started_at == "2026-06-01T10:00:00"
    assert delta.e1rm_delta == pytest.approx(curr.top_e1rm - prev.top_e1rm)
    assert delta.tonnage_delta == pytest.approx(curr.tonnage - prev.tonnage)
