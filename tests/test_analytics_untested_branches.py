"""Ветки расчётов (analytics.py), которые не исполнял ни один тест.

- `lift_change` у упражнения своим весом — вердикт, который тренер (живой и
  AI-сводка) получает о клиенте: подтягивания 8 → 12 обязаны быть «вырос», а
  не «ровно» из-за нулевого веса;
- `rank_progress` по осям тоннажа и частоты — полоска под званием обязана
  говорить про ту же ось, что фраза рядом;
- `gold_book` на подходах с нулём повторов — «Золотая книга» не должна
  объявить рекордом подход, которого не было.
"""

import pytest

import analytics


def _session(day: str, *sets: tuple[float, int], wid: int) -> analytics.SessionStats:
    started = f"{day}T10:00:00"
    return analytics.SessionStats(
        workout_id=wid, started_at=started,
        sets=[analytics.SetRow(weight=w, reps=r, workout_id=wid, started_at=started) for w, r in sets],
    )


# ---------- lift_change: своим весом ----------


def test_bodyweight_more_reps_is_up_and_compares_best_sets_by_date():
    # Нарочно не по порядку: сравнение — первая и последняя по дате.
    change = analytics.lift_change([
        _session("2026-10-15", (0.0, 12), (0.0, 9), wid=2),
        _session("2026-09-01", (0.0, 8), (0.0, 6), wid=1),
    ])
    assert change.verdict == analytics.LIFT_UP
    assert change.first == ("2026-09-01", 0.0, 8)
    assert change.last == ("2026-10-15", 0.0, 12)
    # e1RM своим весом не считается — и проценты от нуля не выдумываются.
    assert (change.first_e1rm, change.last_e1rm, change.pct) == (None, None, None)
    assert change.sessions == 2


@pytest.mark.parametrize(
    ("first", "last", "verdict"),
    [((0.0, 10), (0.0, 7), analytics.LIFT_DROP), ((0.0, 10), (0.0, 10), analytics.LIFT_FLAT)],
)
def test_bodyweight_drop_and_flat(first, last, verdict):
    change = analytics.lift_change([
        _session("2026-09-01", first, wid=1),
        _session("2026-10-01", last, wid=2),
    ])
    assert change.verdict == verdict


def test_bodyweight_single_session_has_nothing_to_compare():
    assert analytics.lift_change([_session("2026-09-01", (0.0, 10), wid=1)]) is None


def test_bodyweight_sessions_with_zero_reps_do_not_count():
    assert analytics.lift_change([
        _session("2026-09-01", (0.0, 0), wid=1),
        _session("2026-10-01", (0.0, 10), wid=2),
    ]) is None


def test_mixed_weighted_and_bodyweight_history_goes_through_e1rm():
    """Добавка на поясе в одной из сессий — уже не «только повторы»."""
    change = analytics.lift_change([
        _session("2026-09-01", (0.0, 10), wid=1),
        _session("2026-09-15", (10.0, 8), wid=2),
        _session("2026-10-01", (15.0, 8), wid=3),
    ])
    assert change is not None
    assert change.verdict == analytics.LIFT_UP
    assert change.first[1] > 0  # сессия без веса в e1RM-сравнение не попадает


# ---------- rank_progress: оси тоннажа и частоты ----------


def _rank_with_next(predicate):
    for rank in analytics.RANKS[:-1]:
        nxt = analytics.next_rank(rank)
        if predicate(rank, nxt):
            return rank, nxt
    pytest.skip("в лестнице нет пары званий с растущим порогом по этой оси")


def test_tonnage_axis_measures_from_current_threshold():
    rank, nxt = _rank_with_next(lambda r, n: n.min_tonnage_kg > r.min_tonnage_kg)
    middle = (rank.min_tonnage_kg + nxt.min_tonnage_kg) / 2
    gap = analytics.RankGap("tonnage", nxt.min_tonnage_kg - middle)
    assert analytics.rank_progress(rank, gap, 0, middle, 0) == pytest.approx(0.5)


def test_frequency_axis_measures_from_current_threshold():
    rank, nxt = _rank_with_next(lambda r, n: n.min_per_week > r.min_per_week)
    middle = (rank.min_per_week + nxt.min_per_week) / 2
    gap = analytics.RankGap("frequency", nxt.min_per_week)
    assert analytics.rank_progress(rank, gap, 0, 0, middle) == pytest.approx(0.5)


def test_progress_is_clamped_to_the_bar():
    rank, nxt = _rank_with_next(lambda r, n: n.min_tonnage_kg > r.min_tonnage_kg)
    gap = analytics.RankGap("tonnage", 1)
    # Перерыв уронил звание ниже заработанного тоннажа — полоска не за 100%.
    assert analytics.rank_progress(rank, gap, 0, nxt.min_tonnage_kg * 10, 0) == 1.0
    assert analytics.rank_progress(rank, gap, 0, -5, 0) == 0.0


def test_rank_gap_and_progress_agree_on_the_axis():
    """Сквозная проверка: ось, которую назвал rank_gap, и полоска по ней."""
    rank = analytics.RANKS[1]
    nxt = analytics.next_rank(rank)
    workouts, tonnage, per_week = nxt.min_workouts, rank.min_tonnage_kg, nxt.min_per_week
    gap = analytics.rank_gap(rank, workouts, tonnage, per_week)
    if nxt.min_tonnage_kg > rank.min_tonnage_kg:
        assert gap.axis == "tonnage"
        assert gap.value == pytest.approx(nxt.min_tonnage_kg - tonnage)
        assert analytics.rank_progress(rank, gap, workouts, tonnage, per_week) == 0.0


# ---------- gold_book: подходы с нулём повторов ----------


def test_gold_book_ignores_zero_rep_rows():
    book = analytics.gold_book([
        _session("2026-09-01", (200.0, 0), (100.0, 5), wid=1),
    ])
    # 200×0 — не подход: ни максимальный вес, ни e1RM от него не берутся.
    assert book.max_weight == 100.0
    assert (book.best_e1rm_weight, book.best_e1rm_reps) == (100.0, 5)


def test_gold_book_of_only_zero_rep_rows_is_empty():
    assert analytics.gold_book([_session("2026-09-01", (100.0, 0), wid=1)]) is None
