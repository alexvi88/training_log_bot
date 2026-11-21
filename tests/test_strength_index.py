"""«Сила за N недель» — analytics.strength_index и `lifts.strength` в
GET /v1/dashboard (сквозные тесты — в test_api_v1_dashboard.py).

Прежнее число на доске «Прогресса» было средним по плиткам роста, а плитки —
только выросшие движения: просевшие и застывшие в него не попадали, и итог
врал вверх. Здесь — все движения окна, вес по подходам с потолком, лучшие
краёв окна (по две недели), чтобы лёгкая тренировка на технику не читалась
спадом.
"""

import datetime as dt

import pytest

import analytics

D0 = dt.date(2026, 9, 1)


def _weekly(values, start=D0, step=7):
    """Ряд (день, e1RM): тренировка раз в `step` дней."""
    return [(start + dt.timedelta(days=i * step), v) for i, v in enumerate(values)]


def test_lift_change_compares_best_of_the_first_and_last_two_weeks():
    # 8 недель жима, последняя тренировка — лёгкая на технику (110×3 → 121):
    # она не спад, итог — лучшая последних двух недель против лучшей первых.
    bench = _weekly([139, 143, 139, 142, 148, 145, 148, 121])
    assert analytics.lift_change_pct(bench) == pytest.approx((148 / 143 - 1) * 100)
    # Лёгкая тренировка в начале окна тоже не раздувает рост.
    assert analytics.lift_change_pct(_weekly([90, 140, 140, 140, 140, 140, 140, 145])) == pytest.approx(
        (145 / 140 - 1) * 100
    )


def test_short_history_splits_sessions_in_half():
    # Всё за три недели — края по 14 дней перекрылись бы: половина против половины.
    assert analytics.lift_change_pct(_weekly([100, 100, 120, 120], step=5)) == pytest.approx(20.0)
    assert analytics.lift_change_pct(_weekly([100])) is None
    assert analytics.lift_change_pct(_weekly([0, 0, 0, 0])) is None


def test_dropping_lifts_pull_the_index_down():
    up = (_weekly([100, 100, 100, 110, 110, 110]), 20)
    down = (_weekly([100, 100, 100, 90, 90, 90]), 20)
    index = analytics.strength_index([up, down])
    assert index.growth_pct == 0.0
    assert (index.lifts, index.up, index.flat, index.down) == (2, 1, 0, 1)


def test_one_lift_cannot_carry_the_index():
    # Жим — 80 подходов из 100, остальные четыре по 5. Без потолка итог был бы
    # +16% (0.8 × 20); с потолком доля жима 25%, после нормировки — 0.25/0.45.
    bench = (_weekly([100, 100, 100, 120, 120, 120]), 80)
    rest = [(_weekly([100, 100, 100, 100, 100, 100]), 5)] * 4
    index = analytics.strength_index([bench, *rest])
    assert index.growth_pct == pytest.approx(11.1)
    assert (index.up, index.flat, index.down) == (1, 4, 0)


def test_no_index_without_two_weighted_sessions():
    assert analytics.strength_index([]) is None
    assert analytics.strength_index([(_weekly([100]), 3), (_weekly([0, 0, 0, 0]), 12)]) is None
