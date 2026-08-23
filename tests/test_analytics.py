"""Pure-Python metrics: e1RM formulas, sessions, trend regression, PR detection."""

import datetime as dt

import pytest

import analytics
from analytics import SessionStats, SetRow

# ---------- e1RM formulas ----------


def test_epley_e1rm_single_rep_returns_weight():
    assert analytics.epley_e1rm(100, 1) == 100
    assert analytics.epley_e1rm(100, 0) == 100


def test_epley_e1rm_formula():
    assert analytics.epley_e1rm(100, 10) == pytest.approx(133.333, abs=1e-3)


def test_brzycki_e1rm_single_rep_returns_weight():
    assert analytics.brzycki_e1rm(100, 1) == 100


def test_brzycki_hands_over_to_epley_past_its_range():
    """The old `reps >= 37` guard sat just past the formula's blow-up, so the
    worst values sailed through: 36 reps returned 36x the weight, then 37
    dropped back to 1x. Above BRZYCKI_MAX_REPS it defers to Epley instead."""
    for reps in (11, 20, 36, 37, 50):
        assert analytics.brzycki_e1rm(100, reps) == analytics.epley_e1rm(100, reps)


def test_brzycki_never_explodes_near_its_old_asymptote():
    # 20kg x 36 used to come out as 720kg and become a permanent e1RM record.
    assert analytics.brzycki_e1rm(20, 36) == pytest.approx(44.0)
    assert analytics.brzycki_e1rm(20, 35) < analytics.brzycki_e1rm(20, 36)


def test_brzycki_is_monotonic_in_reps():
    """No cliff and no dip anywhere — more reps at the same weight can only mean
    an equal or higher estimated max."""
    values = [analytics.brzycki_e1rm(100, r) for r in range(1, 200)]
    # strict=False on purpose: the offset slice is one shorter than `values`.
    assert all(b >= a for a, b in zip(values, values[1:], strict=False))


def test_brzycki_and_epley_agree_exactly_at_the_handover():
    # 1 + 10/30 == 36/27 == 4/3, which is why the handover is seamless.
    assert analytics.brzycki_e1rm(100, analytics.BRZYCKI_MAX_REPS) == pytest.approx(
        analytics.epley_e1rm(100, analytics.BRZYCKI_MAX_REPS)
    )


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


# ---------- suggest_progression ----------


def test_suggest_progression_add_reps_below_top_of_range():
    s = analytics.suggest_progression([(100, 8), (100, 8)])
    assert s.action == "add_reps"
    assert (s.target_weight, s.target_reps) == (100, 9)


def test_suggest_progression_add_weight_when_top_of_range_reached():
    s = analytics.suggest_progression([(100, 12), (100, 12)])
    assert s.action == "add_weight"
    assert s.target_weight == pytest.approx(102.5)
    # Not REP_RANGE_MIN: 102.5x5 would be a weaker session than 100x12.
    assert s.target_reps == 11


def test_suggest_progression_weight_bump_never_lowers_the_bar():
    # The reported bug: 127x10 came back as "129.5 x 5", an e1RM of ~151 against
    # the ~169 already lifted — a goal that asks for less than last time.
    s = analytics.suggest_progression([(127.0, 12)])
    assert s.target_weight == pytest.approx(129.5)
    assert analytics.e1rm(s.target_weight, s.target_reps) >= analytics.e1rm(127.0, 12) * 0.99


def test_suggest_progression_reps_capped_at_top_of_range():
    # 18 reps is far past the range; a single weight step can't hold that e1RM
    # inside the range, so the target caps out rather than chasing 17 reps.
    s = analytics.suggest_progression([(127.0, 18)])
    assert s.target_reps == analytics.REP_RANGE_MAX


def test_suggest_progression_big_jump_can_restart_at_bottom_of_range():
    # A 20% jump (light dumbbells stepping 10 -> 12) outruns the reps it costs.
    s = analytics.suggest_progression([(10.0, 12)], inferred_step=2.0)
    assert (s.target_weight, s.target_reps) == (12.0, analytics.REP_RANGE_MIN)


def test_suggest_progression_uses_heaviest_set_reps():
    # Heaviest set is 100 for 8 reps; lighter warmup-ish sets ignored for the target.
    s = analytics.suggest_progression([(80, 12), (100, 8)])
    assert (s.action, s.target_weight, s.target_reps) == ("add_reps", 100, 9)


def test_suggest_progression_bodyweight_chases_one_more_rep():
    s = analytics.suggest_progression([(0, 12), (0, 10)])
    assert s.is_bodyweight is True
    assert s.target_reps == 13


def test_suggest_progression_none_when_no_sets():
    assert analytics.suggest_progression([]) is None


def test_suggest_progression_heavy_lift_jumps_by_five():
    s = analytics.suggest_progression([(200, 12)])
    assert s.target_weight == pytest.approx(205)


def test_suggest_progression_uses_inferred_step_when_finer_than_default():
    s = analytics.suggest_progression([(50, 12)], inferred_step=2.0)
    assert s.target_weight == pytest.approx(52.0)


def test_suggest_progression_ignores_inferred_step_coarser_than_default():
    # A 20kg gap is a backoff set, not the rack's increment.
    s = analytics.suggest_progression([(100, 12)], inferred_step=20.0)
    assert s.target_weight == pytest.approx(102.5)


# ---------- infer_weight_step / weight_step_for ----------


def test_infer_weight_step_reads_gap_between_two_heaviest():
    assert analytics.infer_weight_step([20, 22, 24, 24, 26]) == pytest.approx(2.0)


def test_infer_weight_step_ignores_bodyweight_and_repeats():
    assert analytics.infer_weight_step([0, 0, 40, 40]) is None
    assert analytics.infer_weight_step([100]) is None
    assert analytics.infer_weight_step([]) is None


def test_infer_weight_step_survives_float_noise():
    assert analytics.infer_weight_step([100.0, 102.5]) == pytest.approx(2.5)


def test_weight_step_for_defaults_per_unit():
    assert analytics.weight_step_for(100, "kg") == pytest.approx(2.5)
    assert analytics.weight_step_for(100, "lb") == pytest.approx(5.0)


def test_weight_step_for_heavy_thresholds_per_unit():
    assert analytics.weight_step_for(199.9, "kg") == pytest.approx(2.5)
    assert analytics.weight_step_for(200, "kg") == pytest.approx(5.0)
    assert analytics.weight_step_for(449, "lb") == pytest.approx(5.0)
    assert analytics.weight_step_for(450, "lb") == pytest.approx(10.0)


def test_weight_step_for_micro_plates_beat_the_heavy_rule():
    # Someone micro-loading a 210kg pull means it: don't force a 5kg jump on them.
    assert analytics.weight_step_for(210, "kg", inferred_step=1.25) == pytest.approx(1.25)


# ---------- is_workout_milestone ----------


@pytest.mark.parametrize("n", [1, 10, 25, 50, 75, 100, 200, 300, 1000])
def test_is_workout_milestone_true(n):
    assert analytics.is_workout_milestone(n) is True


@pytest.mark.parametrize("n", [0, 2, 9, 11, 26, 99, 101, 150, 250])
def test_is_workout_milestone_false(n):
    assert analytics.is_workout_milestone(n) is False


# ---------- is_seasoned ----------


def test_is_seasoned_true_at_exactly_the_threshold():
    today = dt.date(2026, 6, 15)
    dates = [today, today - dt.timedelta(days=5), today - dt.timedelta(days=10)]
    assert analytics.is_seasoned(dates, today) is True


def test_is_seasoned_false_one_short_of_threshold():
    today = dt.date(2026, 6, 15)
    dates = [today, today - dt.timedelta(days=5)]
    assert analytics.is_seasoned(dates, today) is False


def test_is_seasoned_window_edge_is_inclusive():
    today = dt.date(2026, 6, 15)
    cutoff = today - dt.timedelta(days=analytics.RECENT_TRAINING_WINDOW_DAYS - 1)
    dates = [today, today - dt.timedelta(days=3), cutoff]
    assert analytics.is_seasoned(dates, today) is True


def test_is_seasoned_ignores_workouts_just_outside_the_window():
    today = dt.date(2026, 6, 15)
    too_old = today - dt.timedelta(days=analytics.RECENT_TRAINING_WINDOW_DAYS)
    dates = [today, today - dt.timedelta(days=3), too_old]
    assert analytics.is_seasoned(dates, today) is False


def test_is_seasoned_empty_history_is_false():
    assert analytics.is_seasoned([], dt.date(2026, 6, 15)) is False


# ---------- застой: три случая, которые снаружи выглядят одинаково ----------

_TODAY = dt.date(2026, 8, 23)


def _weekly_sessions(sets_per_session: list[list[SetRow]]):
    """Сессии раз в неделю, последняя — вчера. Одна на неделю: это ритм, при
    котором «вес стоит четыре недели» и «сессий в окне четыре» — одно и то же."""
    n = len(sets_per_session)
    out = []
    for i, sets in enumerate(sets_per_session):
        day = _TODAY - dt.timedelta(days=1 + 7 * (n - 1 - i))
        out.append(SessionStats(i + 1, f"{day.isoformat()}T10:00:00", sets))
    return out


def test_flat_weight_and_flat_reps_is_a_dead_end():
    """Настоящий тупик: и вес, и повторы стоят пять недель."""
    sessions = _weekly_sessions([[SetRow(100, 8)] for _ in range(5)])

    verdict = analytics.classify_stall(sessions, _TODAY)

    assert verdict.kind == "dead_end"
    assert verdict.weeks_weight_flat >= analytics.STALL_WEEKS_FLAT
    assert (verdict.reps_at_top_first, verdict.reps_at_top_last) == (8, 8)
    assert verdict.ready_to_add_weight is False


def test_flat_weight_with_rising_reps_is_double_progression_not_a_stall():
    """Вес стоит, а повторы ползут — схема РАБОТАЕТ, и звать это застоем нельзя:
    следующим шагом вес прибавится сам. Именно эти два случая модель и путала."""
    sessions = _weekly_sessions([[SetRow(100, r)] for r in (6, 7, 8, 9, 10)])

    verdict = analytics.classify_stall(sessions, _TODAY)

    assert verdict.kind == "double_progression"
    assert (verdict.reps_at_top_first, verdict.reps_at_top_last) == (6, 10)


def test_reps_at_the_top_of_the_range_flag_the_weight_bump():
    """Добрал 12 — по методике это сигнал прибавить вес, и правило в промпте
    было, а применить его проактивно было нечем."""
    sessions = _weekly_sessions([[SetRow(100, r)] for r in (9, 10, 11, 12)])

    verdict = analytics.classify_stall(sessions, _TODAY)

    assert verdict.ready_to_add_weight is True


def test_growing_e1rm_is_not_a_stall_even_with_a_flat_last_session():
    sessions = _weekly_sessions([[SetRow(w, 5)] for w in (100, 105, 110, 115, 115)])

    verdict = analytics.classify_stall(sessions, _TODAY)

    assert verdict.kind == "growing"
    assert verdict.e1rm_slope_per_week > 0


def test_falling_e1rm_reads_as_regressing():
    sessions = _weekly_sessions([[SetRow(w, 5)] for w in (120, 115, 110, 100)])

    verdict = analytics.classify_stall(sessions, _TODAY)

    assert verdict.kind == "regressing"


def test_too_few_sessions_in_the_window_is_not_a_verdict():
    """Три точки складываются в любой наклон: «встало» на них означает только
    «мало данных», и в выдаче такому упражнению не место."""
    sessions = _weekly_sessions([[SetRow(100, 8)] for _ in range(3)])

    assert analytics.classify_stall(sessions, _TODAY) is None


def test_an_abandoned_exercise_is_not_stalled_it_is_abandoned():
    """Окно считается от сегодня, а не от последней сессии: брошенное полгода
    назад упражнение не «встало» — его просто не делают."""
    sessions = [
        SessionStats(i, f"{(dt.date(2026, 1, 5) + dt.timedelta(days=7 * i)).isoformat()}T10:00:00",
                     [SetRow(100, 8)])
        for i in range(6)
    ]

    assert analytics.classify_stall(sessions, _TODAY) is None


def test_rpe_of_the_top_set_comes_back_averaged():
    """Стоит на RPE 6-7 — недогруз, на 9-10 — усталость или техника: без этого
    разреза оба лечатся одинаково и неверно."""
    sessions = _weekly_sessions([[SetRow(100, 8, rpe=rpe)] for rpe in (6, 6.5, 7, 6.5)])

    verdict = analytics.classify_stall(sessions, _TODAY)

    assert verdict.avg_top_rpe == pytest.approx(6.5, abs=0.05)


def test_bodyweight_exercise_is_judged_by_reps_alone():
    """У отжиманий рабочего веса нет вовсе, и «вес не двигался» про них не
    говорит ничего: вся прогрессия там в повторах."""
    sessions = _weekly_sessions([[SetRow(0, r)] for r in (20, 22, 25, 28)])

    verdict = analytics.classify_stall(sessions, _TODAY)

    assert verdict.kind == "double_progression"
    assert verdict.weeks_weight_flat is None
