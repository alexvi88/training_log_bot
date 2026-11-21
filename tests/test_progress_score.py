"""«Оценка» 0–10 (progress_score.py) — каждое правило на данных руками.

Подходы — `SetRow` без базы: день отсчитывается назад от D. Одна строка —
один подход; тренировка — день (у теста одна тренировка в день).
"""

from __future__ import annotations

import datetime as dt

import pytest

import db  # noqa: F401 — seed_data импортируется только после db (круговой импорт)
import progress_score as ps

D = dt.date(2026, 11, 21)


def ago(n: int) -> dt.date:
    return D - dt.timedelta(days=n)


def row(n_ago: int, ex: int = 1, e1rm: float = 100.0, group: str | None = "Грудь",
        identity: str | None = None, name: str = "Жим", weighted: bool = True) -> ps.SetRow:
    day = ago(n_ago)
    return ps.SetRow(day.toordinal(), day, ex, name, identity, group, e1rm, weighted)


def days_of(rows, *extra: int) -> list[dt.date]:
    return sorted({r.day for r in rows} | {ago(n) for n in extra})


def first_weeks(rows) -> ps.FirstWeeks:
    return ps.first_weeks_from_rows(rows, dt.date.min)


def score(rows, d: dt.date = D, *extra: int) -> ps.DayScore:
    return ps.score_day(rows, days_of(rows, *extra), first_weeks(rows), d)


def steady(days: int = 120, every: int = 3, ex: int = 99, group: str = "Спина", sets: int = 6) -> list[ps.SetRow]:
    """Фон: давний регулярный атлет — тренировка каждые `every` дней."""
    return [row(n, ex=ex, group=group, name="Тяга") for n in range(days, -1, -every) for _ in range(sets)]


# --- баллы упражнения ------------------------------------------------------------


@pytest.mark.parametrize("change, points", [
    (0.0, 0.5), (1.0, 0.5), (-1.0, 0.5),           # шум
    (5.0, 1.0), (2.5, 0.75), (-2.5, 0.25), (-5.0, 0.0),
    (12.0, 1.0), (-30.0, 0.0),                       # потолок и пол
])
def test_lift_points_continuous_mapping(change, points):
    assert ps.lift_points(change) == pytest.approx(points)


def test_lift_points_just_past_noise_band():
    assert ps.lift_points(1.5) == pytest.approx(0.65)


# --- прогрессия ----------------------------------------------------------------


def test_baseline_is_best_of_days_41_to_28():
    # D-50 — раньше окна базы, D-20 — между базой и «текущим» (последние 14 дней).
    rows = [row(50, e1rm=200), row(41, e1rm=90), row(35, e1rm=100), row(28, e1rm=95),
            row(20, e1rm=300), row(5, e1rm=105)]
    points, lifts = ps.progression(rows, first_weeks(rows), D)
    lift = lifts[0]
    assert lift.baseline == 100 and lift.current == 105
    assert lift.change_pct == pytest.approx(5.0)
    assert lift.points == pytest.approx(1.0) and points == pytest.approx(4.0)


def test_baseline_falls_back_to_first_14_days():
    rows = [row(70, e1rm=100), row(60, e1rm=102), row(55, e1rm=120), row(3, e1rm=104.04)]
    _, lifts = ps.progression(rows, first_weeks(rows), D)
    # Первые 14 дней — D-70…D-57: лучший 102, а не 120 с D-55.
    assert lifts[0].baseline == 102
    assert lifts[0].change_pct == pytest.approx(2.0)


def test_exercise_younger_than_28_days_is_not_rated():
    rows = [row(20, e1rm=100), row(2, e1rm=110)]
    points, lifts = ps.progression(rows, first_weeks(rows), D)
    assert points is None
    assert len(lifts) == 1 and not lifts[0].rated and lifts[0].baseline is None


def test_exercise_without_recent_session_is_not_listed():
    rows = [row(35, e1rm=100), row(20, e1rm=100)]
    points, lifts = ps.progression(rows, first_weeks(rows), D)
    assert points is None and lifts == []


def test_compound_weighs_twice_isolation():
    rows = [
        row(35, ex=1, e1rm=100, identity="Жим штанги лёжа"), row(3, ex=1, e1rm=105, identity="Жим штанги лёжа"),
        row(35, ex=2, e1rm=20, name="Разводка"), row(3, ex=2, e1rm=19, name="Разводка"),
    ]
    points, lifts = ps.progression(rows, first_weeks(rows), D)
    assert [lift.weight for lift in lifts] == [1.0, 2.0]  # слабое (разводка) первым
    assert points == pytest.approx(4 * (2 * 1.0 + 1 * 0.0) / 3)


def test_light_technique_session_does_not_lower_progression():
    base = [row(35, e1rm=100), row(10, e1rm=105)]
    light = base + [row(2, e1rm=60)]
    assert ps.progression(base, first_weeks(base), D)[0] == ps.progression(light, first_weeks(light), D)[0]


def test_bodyweight_and_timed_moves_are_skipped():
    assert not ps.is_weighted("Подтягивания", "full")
    assert not ps.is_weighted("Планка", "none")
    assert not ps.is_weighted("Скручивания", "none")
    assert ps.is_weighted("Жим штанги лёжа", "none")
    rows = [row(35, weighted=False), row(3, e1rm=200, weighted=False)]
    assert ps.progression(rows, first_weeks(rows), D) == (None, [])


# --- регулярность --------------------------------------------------------------


def test_regularity_caps_at_two_workouts_per_block():
    wd = sorted(ago(n) for n in (1, 3, 5, 8, 22, 24, 25))  # блоки: 3, 1, 0, 3
    weeks = ps.week_blocks(wd, ago(100), D)
    assert [w.workouts for w in weeks] == [3, 1, 0, 3]
    assert ps.regularity(weeks) == pytest.approx(3 * (1 + 0.5 + 0 + 1) / 4)


def test_regularity_counts_only_blocks_from_first_workout():
    wd = [ago(10), ago(5), ago(2)]
    weeks = ps.week_blocks(wd, wd[0], D)
    assert [w.counted for w in weeks] == [True, True, False, False]
    assert ps.regularity(weeks) == pytest.approx(3 * (1 + 0.5) / 2)


# --- объём и баланс --------------------------------------------------------------


def _blocks(rows, start=None):
    return ps.week_blocks(days_of(rows), start or ago(100), D)


def _sets(n_ago: int, count: int, group: str = "Грудь", e1rm: float = 100.0):
    return [row(n_ago, group=group, e1rm=e1rm) for _ in range(count)]


def test_volume_more_sets_never_lower_the_score():
    six = [r for n in (2, 9, 16, 23) for r in _sets(n, 6)]
    twenty = [r for n in (2, 9, 16, 23) for r in _sets(n, 20)]
    vol6, *_ = ps.volume_and_balance(six, _blocks(six), D)
    vol20, *_ = ps.volume_and_balance(twenty, _blocks(twenty), D)
    assert vol6 == vol20 == pytest.approx(2.0)


def test_volume_is_mean_of_block_shares_over_groups():
    rows = (_sets(2, 6) + _sets(9, 3) + _sets(16, 6) + _sets(23, 0)
            + _sets(2, 3, "Ноги") + _sets(9, 6, "Ноги") + _sets(16, 6, "Ноги") + _sets(23, 6, "Ноги"))
    vol, _bal, weeks, groups = ps.volume_and_balance(rows, _blocks(rows), D)
    # Грудь: 1, .5, 1, 0 → .625; Ноги: .5, 1, 1, 1 → .875
    assert vol == pytest.approx(2 * (0.625 + 0.875) / 2)
    chest = next(g for g in groups if g.group == "Грудь")
    assert (chest.sets_7d, chest.sets_28d) == (6, 15)


def test_newcomer_volume_counts_only_existing_blocks():
    rows = _sets(9, 6) + _sets(2, 6)
    vol, _bal, weeks, _groups = ps.volume_and_balance(rows, _blocks(rows, start=ago(9)), D)
    assert [w.counted for w in weeks] == [True, True, False, False]
    assert vol == pytest.approx(2.0)


def test_deload_block_is_left_out_of_volume():
    rows = _sets(2, 3, e1rm=80) + _sets(9, 10) + _sets(16, 10) + _sets(23, 10)
    vol, _bal, weeks, _groups = ps.volume_and_balance(rows, _blocks(rows), D)
    assert [w.deload for w in weeks] == [True, False, False, False]
    assert vol == pytest.approx(2.0)  # без разгрузки — три полных блока


def test_light_but_heavy_week_is_not_a_deload():
    # Подходов мало, но верхний e1RM не упал — это не разгрузка.
    rows = _sets(2, 3, e1rm=120) + _sets(9, 10) + _sets(16, 10) + _sets(23, 10)
    _vol, _bal, weeks, _groups = ps.volume_and_balance(rows, _blocks(rows), D)
    assert not any(w.deload for w in weeks)


def test_missed_block_is_not_a_deload():
    rows = _sets(9, 10) + _sets(16, 10) + _sets(23, 10)
    vol, _bal, weeks, _groups = ps.volume_and_balance(rows, _blocks(rows), D)
    assert not weeks[0].deload
    assert vol == pytest.approx(2 * 0.75)


def test_balance_counts_groups_untouched_two_weeks():
    rows = _sets(2, 6) + _sets(20, 6, "Ноги") + _sets(10, 6, "Спина") + _sets(40, 3, "Плечи")
    _vol, bal, _weeks, groups = ps.volume_and_balance(rows, _blocks(rows), D)
    # Ноги и плечи — ни подхода за 14 дней; грудь и спина — в работе.
    assert bal == pytest.approx(1 - 2 / 4)
    assert {g.group for g in groups if g.neglected} == {"Ноги", "Плечи"}
    # Плечи тренировались только за 6 недель — в объём не входят.
    assert next(g for g in groups if g.group == "Плечи").points is None


def test_balance_without_groups_is_full():
    rows = _sets(2, 6, group=None)
    vol, bal, _weeks, groups = ps.volume_and_balance(rows, _blocks(rows), D)
    assert vol is None and bal == 1.0 and groups == []


# --- статусы и итог --------------------------------------------------------------


def test_no_workouts_is_empty():
    s = score([])
    assert s.status == "empty" and s.score is None
    assert all(p.points is None for p in s.parts)


def test_one_workout_is_calibrating():
    s = score(_sets(3, 6))
    assert s.status == "calibrating" and s.score is None
    assert s.ready_on == ago(3) + dt.timedelta(days=28)
    assert s.part("regularity").points is not None and s.part("progression").points is None


def test_two_workouts_in_first_week_count_only_current_block():
    s = score(_sets(5, 6) + _sets(2, 6))
    assert s.status == "calibrating"
    assert [w.counted for w in s.weeks] == [True, False, False, False]
    assert s.part("regularity").points == pytest.approx(3.0)
    assert s.part("volume").points == pytest.approx(2.0)


def test_newcomer_after_28_days_is_partial_and_scaled():
    rows = [r for n in range(30, -1, -4) for r in _sets(n, 6)]
    s = score(rows)
    assert s.status == "partial"
    assert s.part("progression").points is None
    assert s.progression_from == ago(30) + dt.timedelta(days=42)
    have = [p for p in s.parts if p.points is not None]
    assert s.score == round(sum(p.points for p in have) / 6 * 10, 1)


def test_full_history_is_ok():
    rows = steady() + [row(35, ex=1, e1rm=100), row(3, ex=1, e1rm=105)]
    s = score(rows)
    assert s.status == "ok" and all(p.points is not None for p in s.parts)
    assert s.score == round(sum(p.points for p in s.parts), 1)


def test_long_history_without_rateable_lift_is_partial():
    rows = [r for r in steady() if r.day >= ago(120)]
    rows = [ps.SetRow(r.workout_id, r.day, r.exercise_id, r.name, r.identity, r.group, r.e1rm, False) for r in rows]
    s = score(rows)
    assert s.status == "partial" and s.part("progression").points is None


def test_pause_after_two_weeks_without_workouts():
    rows = [r for r in steady() if r.day <= ago(18)]
    rep = ps.report(rows, days_of(rows), first_weeks(rows), D)
    s = rep.today
    assert s.status == "paused" and s.score is None
    assert s.pause_days == 18
    assert s.last_score is not None and s.last_score_date == ago(18)
    assert s.lever.kind == "paused"
    # Старые точки ряда остаются, дни паузы — пустые.
    by_day = {h.day: h.score for h in rep.history}
    assert by_day[ago(27)] is not None and by_day[ago(0)] is None


def test_return_after_break_calibrates_for_14_days():
    before = [r for r in steady() if r.day >= ago(120) and r.day <= ago(60)]
    after = _sets(5, 6) + _sets(2, 6)
    s = score(before + after)
    assert s.phase.returned and s.phase.start_day == ago(5)
    assert s.status == "calibrating" and s.ready_on == ago(5) + dt.timedelta(days=14)
    assert s.calibration_days == 14
    assert s.lever.kind == "comeback"


def test_return_regularity_ignores_weeks_before_the_return():
    before = [r for r in steady() if r.day >= ago(120) and r.day <= ago(50)]
    after = [r for n in (20, 17, 13, 10, 6, 3) for r in _sets(n, 6)]
    s = score(before + after)
    assert s.status == "partial"
    assert [w.counted for w in s.weeks] == [True, True, True, False]
    assert s.part("regularity").points == pytest.approx(3.0)


def test_return_baseline_is_post_return_only():
    # До перерыва жим был 150, после возвращения — 100 → 104: это рост, а не падение.
    before = [row(n, ex=1, e1rm=150) for n in range(120, 62, -3)]  # до D-63, перерыв 23 дня
    after = [row(n, ex=1, e1rm=100 + (40 - n) / 10) for n in range(40, -1, -3)]
    rows = before + after
    s = score(rows)
    lift = next(lift for lift in s.lifts if lift.exercise_id == 1)
    assert lift.rated and lift.baseline < 110
    assert lift.change_pct > 0
    # Ещё нет 28 дней после возвращения — прогрессии нет, дата её появления есть.
    early = [r for r in rows if r.day <= ago(20)]
    s_early = score(early, ago(20))
    assert s_early.part("progression").points is None
    assert s_early.progression_from == ago(40) + dt.timedelta(days=28)


# --- ряд, дельта и рычаг ---------------------------------------------------------


@pytest.mark.parametrize("period, points", [(28, 28), (90, 13), (365, 53)])
def test_history_length(period, points):
    rows = steady()
    rep = ps.report(rows, days_of(rows), first_weeks(rows), D, period_days=period)
    assert len(rep.history) == points
    assert rep.history[-1].day == D and rep.history[0].day < rep.history[-1].day
    if period > 28:
        assert rep.history[-1].day - rep.history[-2].day == dt.timedelta(days=7)


def test_delta_7d_and_best():
    rows = steady()
    rep = ps.report(rows, days_of(rows), first_weeks(rows), D)
    assert rep.delta_7d == round(rep.today.score - rep.week_ago.score, 1)
    assert rep.week_ago.day == ago(7)
    assert rep.best.score == max(h.score for h in rep.history if h.score is not None)


def test_delta_is_null_without_score_a_week_ago():
    rows = [r for n in range(30, -1, -4) for r in _sets(n, 6)]
    rep = ps.report(rows, days_of(rows), first_weeks(rows), D)
    assert rep.week_ago.status == "calibrating" and rep.delta_7d is None


def test_lever_names_the_short_group():
    # Прогрессия полная (жим +6%), регулярность и спина — тоже; грудь
    # появилась две недели назад и на этой неделе — 2 подхода.
    chest = [row(9, ex=3, group="Грудь", name="Разводка") for _ in range(6)]
    chest += [row(2, ex=3, group="Грудь", name="Разводка") for _ in range(2)]
    rows = [
        ps.SetRow(r.workout_id, r.day, r.exercise_id, r.name, r.identity, r.group, r.e1rm, False)
        for r in steady(group="Спина") + chest
    ] + [row(35, ex=1, e1rm=100, group=None), row(3, ex=1, e1rm=106, group=None)]
    s = score(rows)
    assert s.part("progression").points == pytest.approx(4.0)
    assert s.lever.kind == "volume_more"
    assert s.lever.params == {"group": "Грудь", "sets": 2, "need": 4}


def test_calibrating_lever_says_keep_with_two_workouts_this_week():
    s = score(_sets(5, 6) + _sets(2, 6))
    assert s.lever.kind == "calibrating_keep"
    s1 = score(_sets(2, 6))
    assert s1.lever.kind == "calibrating" and s1.lever.params["date"] == ago(2) + dt.timedelta(days=28)
