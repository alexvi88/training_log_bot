"""«Оценка» 0–10 (progress_score.py) — каждое правило на данных руками.

Подходы — `SetRow` без базы: день отсчитывается назад от D. Одна строка —
один подход; тренировка — день (у теста одна тренировка в день).

D — суббота: текущая календарная неделя — [D-5, D] (понедельник D-5),
законченные — [D-12, D-6], [D-19, D-13], [D-26, D-20], [D-33, D-27].
"""

from __future__ import annotations

import datetime as dt

import pytest

import db  # noqa: F401 — seed_data импортируется только после db (круговой импорт)
import progress_score as ps

D = dt.date(2026, 11, 21)
assert D.weekday() == 5  # суббота — разметка недель в тестах ниже от неё


def ago(n: int) -> dt.date:
    return D - dt.timedelta(days=n)


def row(n_ago: int, ex: int = 1, e1rm: float = 100.0, group: str | None = "Грудь",
        identity: str | None = None, name: str = "Жим", metric: str | None = ps.METRIC_E1RM,
        reps: int = 5) -> ps.SetRow:
    day = ago(n_ago)
    return ps.SetRow(day.toordinal(), day, ex, name, identity, group, e1rm, metric, reps)


def unrated(rows) -> list[ps.SetRow]:
    """Те же подходы, но упражнения не оцениваются (на время)."""
    return [ps.SetRow(r.workout_id, r.day, r.exercise_id, r.name, r.identity, r.group, r.e1rm, None, r.reps)
            for r in rows]


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
    (-4.0, 0.0), (-3.0, 0.0), (-2.0, 0.1), (-1.0, 0.2), (0.0, 0.3), (2.5, 0.65), (5.0, 1.0), (8.0, 1.0),
])
def test_lift_points_piecewise_scale(change, points):
    assert ps.lift_points(change) == pytest.approx(points)


def test_lift_points_has_no_noise_band():
    # Полосы шума в баллах нет: +0.5% — уже больше, чем «без изменений».
    assert ps.lift_points(0.5) == pytest.approx(0.37)
    assert ps.lift_points(-0.5) == pytest.approx(0.25)


# --- прогрессия ----------------------------------------------------------------


def test_baseline_is_best_of_days_41_to_28():
    # D-50 — раньше окна базы, D-20 — между базой и «текущим» (последние 14 дней).
    rows = [row(50, e1rm=200), row(41, e1rm=90), row(35, e1rm=100), row(28, e1rm=95),
            row(20, e1rm=300), row(5, e1rm=105)]
    points, lifts = ps.progression(rows, first_weeks(rows), D)
    lift = lifts[0]
    assert lift.baseline == 100 and lift.current == 105
    assert lift.change_pct == pytest.approx(5.0)
    assert lift.points == pytest.approx(1.0) and points == pytest.approx(5.0)


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
    assert points == pytest.approx(5 * (2 * 1.0 + 1 * 0.0) / 3)


def test_light_technique_session_does_not_lower_progression():
    base = [row(35, e1rm=100), row(10, e1rm=105)]
    light = base + [row(2, e1rm=60)]
    assert ps.progression(base, first_weeks(base), D)[0] == ps.progression(light, first_weeks(light), D)[0]


def test_metric_by_kind():
    assert ps.metric_of("Подтягивания", "full") == ps.METRIC_REPS
    assert ps.metric_of("Скручивания", "none") == ps.METRIC_REPS
    assert ps.metric_of("Отжимания", "full") == ps.METRIC_REPS  # своё «только повторы»
    assert ps.metric_of("Планка", "none") is None
    assert ps.metric_of("Жим штанги лёжа", "none") == ps.METRIC_E1RM
    assert not ps.is_weighted("Подтягивания", "full")


def test_timed_moves_are_skipped():
    rows = unrated([row(35), row(3, e1rm=200)])
    assert ps.progression(rows, first_weeks(rows), D) == (None, [])


def _pullups(n_ago: int, reps: int) -> ps.SetRow:
    return row(n_ago, ex=7, e1rm=0.0, group="Спина", identity="Подтягивания", name="Подтягивания",
               metric=ps.METRIC_REPS, reps=reps)


def test_bodyweight_lift_is_rated_by_best_reps():
    rows = [_pullups(35, 8), _pullups(35, 10), _pullups(3, 11), _pullups(3, 9)]
    points, lifts = ps.progression(rows, first_weeks(rows), D)
    lift = lifts[0]
    assert lift.by_reps and (lift.baseline, lift.current) == (10, 11)
    assert lift.change_pct == pytest.approx(10.0)
    assert lift.points == 1.0 and points == pytest.approx(5.0)
    assert lift.weight == 2.0  # подтягивания — базовое движение


def test_bodyweight_lift_uses_the_same_scale_and_coarse_flat_band():
    rows = [_pullups(35, 21), _pullups(3, 20)]  # −4.8%: ноль баллов…
    _points, lifts = ps.progression(rows, first_weeks(rows), D)
    assert lifts[0].points == 0.0
    # …но для слов тренера повтор из двадцати — ещё «стоит».
    assert abs(lifts[0].change_pct) <= lifts[0].noise_pct


def test_bodyweight_first_weeks_fallback_by_reps():
    rows = [_pullups(60, 6), _pullups(55, 7), _pullups(3, 7)]
    _, lifts = ps.progression(rows, first_weeks(rows), D)
    assert lifts[0].baseline == 7 and lifts[0].points == pytest.approx(0.3)


# --- календарные недели и регулярность ---------------------------------------------


def test_blocks_are_calendar_weeks_current_first():
    b = ps.blocks(D)
    assert b[0] == (ago(5), ago(-1))       # понедельник–воскресенье текущей
    assert b[1:] == [(ago(12), ago(6)), (ago(19), ago(13)), (ago(26), ago(20)), (ago(33), ago(27))]
    assert all(lo.weekday() == 0 and hi.weekday() == 6 for lo, hi in b)


def test_regularity_caps_at_two_workouts_per_week():
    wd = sorted(ago(n) for n in (8, 9, 10, 15, 27, 28, 29))  # недели: 0 | 3, 1, 0, 3
    weeks = ps.week_blocks(wd, ago(100), D)
    assert [w.workouts for w in weeks] == [0, 3, 1, 0, 3]
    assert ps.regularity(weeks[1:]) == pytest.approx(3 * (1 + 0.5 + 0 + 1) / 4)


def test_current_week_counts_workouts_only_up_to_d():
    wd = [ago(4), ago(-1)]  # воскресенье этой недели ещё впереди
    assert ps.week_blocks(wd, ago(100), D)[0].workouts == 1


def test_regularity_counts_only_weeks_from_first_workout():
    wd = [ago(10), ago(5), ago(2)]
    weeks = ps.week_blocks(wd, wd[0], D)
    assert [w.counted for w in weeks] == [True, True, False, False, False]
    assert ps.regularity(weeks[:4]) == pytest.approx(3 * (1 + 0.5) / 2)


def _twice_a_week(first: int = 120, last: int = 0, *, sets: int = 6, skip_week: dt.date | None = None):
    """Две тренировки в календарную неделю: понедельник и через неделю то четверг,
    то пятница — интервалы 3, 4, 4, 3: скользящее 7-дневное окно ловит то одну,
    то три тренировки, календарная неделя — всегда две."""
    out = []
    for n in range(first, last - 1, -1):
        day = ago(n)
        second = 3 if ps.week_monday(day).toordinal() // 7 % 2 else 4
        if day.weekday() in (0, second) and not (skip_week and ps.week_monday(day) == skip_week):
            out += [row(n, group="Грудь") for _ in range(sets)]
    return out


def test_steady_two_a_week_score_does_not_jitter_day_to_day():
    """Ровные две тренировки в неделю с интервалами 3 и 4 дня: 14 дней подряд —
    одна и та же оценка (скользящие 7-дневные блоки давали 10 → 9.4 → 10)."""
    rows = _twice_a_week()
    wd, fw = days_of(rows), first_weeks(rows)
    scores = {ps.score_day([r for r in rows if r.day <= ago(n)], wd, fw, ago(n)).score for n in range(14)}
    assert scores == {6.5}  # прогрессия «стоит» (0.3 × 5), регулярность и объём полные


def test_current_week_counts_only_when_it_does_not_lower_the_score():
    rows = _twice_a_week()
    # В понедельник утром (D-5 — понедельник, тренировки ещё нет) — четыре законченные недели.
    monday = [r for r in rows if r.day < ago(5)]
    s = ps.score_day(monday, days_of(monday), first_weeks(monday), ago(5))
    assert s.this_week.workouts == 0
    assert not s.weeks[0].current and s.part("regularity").points == pytest.approx(3.0)
    # Две тренировки на этой неделе: не хуже — текущая в счёте (ничья — с ней).
    s = score(rows)
    assert s.weeks[0].current and s.weeks[0].workouts == 2


def test_current_week_replaces_a_missed_old_week():
    # Пропущена самая старая из законченных недель (D-33…D-27).
    rows = _twice_a_week(skip_week=ago(33))
    s = score(rows)
    assert s.weeks[0].current
    assert [w.start for w in s.weeks] == [ago(5), ago(12), ago(19), ago(26)]
    assert s.part("regularity").points == pytest.approx(3.0)
    # В понедельник той же недели текущая пуста — пропуск ещё в счёте.
    monday = [r for r in rows if r.day < ago(5)]
    s_mon = ps.score_day(monday, days_of(monday), first_weeks(monday), ago(5))
    assert s_mon.part("regularity").points == pytest.approx(3 * 0.75)


# --- объём ------------------------------------------------------------------------


def _done_weeks(rows, start=None):
    """Четыре законченные недели (без текущей)."""
    return ps.week_blocks(days_of(rows), start or ago(100), D)[1:]


def _sets(n_ago: int, count: int, group: str = "Грудь", e1rm: float = 100.0):
    return [row(n_ago, group=group, e1rm=e1rm) for _ in range(count)]


def test_volume_more_sets_never_lower_the_score():
    six = [r for n in (8, 15, 22, 29) for r in _sets(n, 6)]
    twenty = [r for n in (8, 15, 22, 29) for r in _sets(n, 20)]
    vol6, *_ = ps.volume(six, _done_weeks(six), D)
    vol20, *_ = ps.volume(twenty, _done_weeks(twenty), D)
    assert vol6 == vol20 == pytest.approx(2.0)


def test_volume_is_mean_of_week_shares_over_groups():
    rows = (_sets(8, 6) + _sets(15, 3) + _sets(22, 6)
            + _sets(8, 3, "Ноги") + _sets(15, 6, "Ноги") + _sets(22, 6, "Ноги") + _sets(29, 6, "Ноги"))
    vol, weeks, groups = ps.volume(rows, _done_weeks(rows), D)
    # Грудь: 1, .5, 1, 0 → .625; Ноги: .5, 1, 1, 1 → .875
    assert vol == pytest.approx(2 * (0.625 + 0.875) / 2)
    chest = next(g for g in groups if g.group == "Грудь")
    assert (chest.sets_7d, chest.sets_28d) == (0, 15)  # на текущей неделе подходов нет


def test_dropped_group_counts_in_volume_as_zero():
    # Ноги были пять недель назад и брошены: в объёме они — ноль, а не выпадают.
    rows = [r for n in (8, 15, 22, 29) for r in _sets(n, 6)] + _sets(35, 6, "Ноги")
    vol, _weeks, groups = ps.volume(rows, _done_weeks(rows), D)
    assert vol == pytest.approx(2 * (1 + 0) / 2)
    assert {g.group: g.neglected for g in groups} == {"Грудь": False, "Ноги": True}


def test_group_older_than_six_weeks_leaves_volume():
    rows = [r for n in (8, 15, 22, 29) for r in _sets(n, 6)] + _sets(45, 6, "Ноги")
    vol, _weeks, groups = ps.volume(rows, _done_weeks(rows), D)
    assert vol == pytest.approx(2.0) and [g.group for g in groups] == ["Грудь"]


def test_newcomer_volume_counts_only_existing_weeks():
    rows = _sets(9, 6) + _sets(2, 6)
    weeks = ps.week_blocks(days_of(rows), ago(9), D)[:4]
    vol, weeks, _groups = ps.volume(rows, weeks, D)
    assert [w.counted for w in weeks] == [True, True, False, False]
    assert vol == pytest.approx(2.0)


def test_deload_week_is_left_out_of_volume():
    rows = _sets(8, 3, e1rm=80) + _sets(15, 10) + _sets(22, 10) + _sets(29, 10)
    vol, weeks, _groups = ps.volume(rows, _done_weeks(rows), D)
    assert [w.deload for w in weeks] == [True, False, False, False]
    assert vol == pytest.approx(2.0)  # без разгрузки — три полные недели


def test_light_but_heavy_week_is_not_a_deload():
    # Подходов мало, но верхний e1RM не упал — это не разгрузка.
    rows = _sets(8, 3, e1rm=120) + _sets(15, 10) + _sets(22, 10) + _sets(29, 10)
    _vol, weeks, _groups = ps.volume(rows, _done_weeks(rows), D)
    assert not any(w.deload for w in weeks)


def test_missed_week_is_not_a_deload():
    rows = _sets(15, 10) + _sets(22, 10) + _sets(29, 10)
    vol, weeks, _groups = ps.volume(rows, _done_weeks(rows), D)
    assert not weeks[0].deload
    assert vol == pytest.approx(2 * 0.75)


def test_volume_without_groups_is_none():
    rows = _sets(2, 6, group=None)
    vol, _weeks, groups = ps.volume(rows, _done_weeks(rows), D)
    assert vol is None and groups == []


# --- статусы и итог --------------------------------------------------------------


def test_parts_are_progression_regularity_volume():
    assert ps.PART_KEYS == ("progression", "regularity", "volume")
    assert ps.POINTS == {"progression": 5.0, "regularity": 3.0, "volume": 2.0}


def test_no_workouts_is_empty():
    s = score([])
    assert s.status == "empty" and s.score is None
    assert all(p.points is None for p in s.parts)


def test_one_workout_is_calibrating():
    s = score(_sets(3, 6))
    assert s.status == "calibrating" and s.score is None
    assert s.ready_on == ago(3) + dt.timedelta(days=28)
    assert s.part("regularity").points is not None and s.part("progression").points is None


def test_two_workouts_in_first_week_count_only_current_week():
    s = score(_sets(5, 6) + _sets(2, 6))
    assert s.status == "calibrating"
    assert [w.counted for w in s.weeks] == [True, False, False, False]
    assert s.part("regularity").points == pytest.approx(3.0)
    assert s.part("volume").points == pytest.approx(2.0)


def test_perfect_newcomer_is_not_inflated_to_ten():
    """Прогрессии ещё нет — она идёт в итог баллом «без изменений» (1.5 из 5):
    аккуратный новичок на пятой неделе видит 6.5, а не 10."""
    rows = _twice_a_week(first=33)
    s = score(rows)
    assert s.status == "partial" and s.part("progression").points is None
    assert s.part("regularity").points == pytest.approx(3.0) and s.part("volume").points == pytest.approx(2.0)
    assert s.score == 6.5
    assert s.progression_from == ago(33) + dt.timedelta(days=42)


def test_newcomer_partial_score_counts_neutral_progression():
    rows = [r for n in range(30, -1, -4) for r in _sets(n, 6)]
    s = score(rows)
    assert s.status == "partial"
    assert s.part("progression").points is None
    reg, vol = s.part("regularity").points, s.part("volume").points
    assert s.score == round((1.5 + reg + vol) / 10 * 10, 1)


def test_full_history_is_ok():
    rows = steady() + [row(35, ex=1, e1rm=100), row(3, ex=1, e1rm=105)]
    s = score(rows)
    assert s.status == "ok" and all(p.points is not None for p in s.parts)
    assert s.score == round(sum(p.points for p in s.parts), 1)


def test_long_history_without_rateable_lift_is_partial():
    s = score(unrated(steady()))
    assert s.status == "partial" and s.part("progression").points is None
    assert s.progression_from is None
    assert s.score == round(1.5 + s.part("regularity").points + s.part("volume").points, 1)


def test_pullups_only_athlete_gets_real_progression():
    rows = [r for n in range(60, -1, -1) if ago(n).weekday() in (0, 3)
            for r in [_pullups(n, 8 + (60 - n) // 14)] * 4]
    s = score(rows)
    assert s.status == "ok"
    lift = s.lifts[0]
    assert lift.rated and lift.by_reps and lift.change_pct > 5
    assert s.part("progression").points == pytest.approx(5.0)


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
    assert by_day[ago(21)] is not None and by_day[ago(0)] is None


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
    after = [r for n in (19, 15, 12, 8, 4, 1) for r in _sets(n, 6)]
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


@pytest.mark.parametrize("period, points", [(28, 5), (90, 13), (365, 53)])
def test_history_is_weekly_on_every_period(period, points):
    rows = steady()
    rep = ps.report(rows, days_of(rows), first_weeks(rows), D, period_days=period)
    assert len(rep.history) == points
    assert rep.history[-1].day == D and rep.history[0].day == D - dt.timedelta(days=7 * (points - 1))
    assert all((b.day - a.day).days == 7 for a, b in zip(rep.history, rep.history[1:], strict=False))


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


def _strong_bench():
    return [row(35, ex=1, e1rm=100, group=None), row(3, ex=1, e1rm=106, group=None)]


def test_lever_names_the_short_group():
    # Прогрессия полная (жим +6%), регулярность и спина — тоже; грудь
    # появилась две недели назад и на этой неделе — 2 подхода.
    chest = [row(9, ex=3, group="Грудь", name="Разводка") for _ in range(6)]
    chest += [row(2, ex=3, group="Грудь", name="Разводка") for _ in range(2)]
    s = score(unrated(steady(group="Спина") + chest) + _strong_bench())
    assert s.part("progression").points == pytest.approx(5.0)
    assert s.lever.kind == "volume_more"
    assert s.lever.params == {"group": "Грудь", "sets": 2, "need": 4}


def test_lever_names_a_dropped_group():
    legs = [row(30, ex=4, group="Ноги", name="Присед") for _ in range(6)]
    s = score(unrated(steady(group="Спина") + legs) + _strong_bench())
    assert next(g for g in s.groups if g.group == "Ноги").neglected
    assert s.lever == ps.Lever("balance", {"group": "Ноги"})


def test_no_lever_when_everything_is_on_track():
    s = score(unrated(steady(group="Спина")) + _strong_bench())
    assert s.status == "ok" and s.score == 10.0
    assert s.lever is None


def test_reps_lever_for_flat_bodyweight_lift():
    rows = [r for n in range(60, -1, -1) if ago(n).weekday() in (0, 3) for r in [_pullups(n, 10)] * 6]
    s = score(rows)
    assert s.lever == ps.Lever("lift_flat_reps", {"exercise": "Подтягивания"})


def test_calibrating_lever_says_keep_with_two_workouts_this_week():
    s = score(_sets(5, 6) + _sets(2, 6))
    assert s.lever.kind == "calibrating_keep"
    s1 = score(_sets(2, 6))
    assert s1.lever.kind == "calibrating" and s1.lever.params["date"] == ago(2) + dt.timedelta(days=28)
