"""«Сила за N недель» — analytics.strength_index и `lifts.strength` в
GET /v1/dashboard (сквозные тесты — в test_api_v1_dashboard.py).

Прежнее число на доске «Прогресса» было средним по плиткам роста, а плитки —
только выросшие движения: просевшие и застывшие в него не попадали, и итог
врал вверх. Здесь — все движения окна, вес по подходам с потолком, медианы
краёв окна вместо одного удачного дня.
"""

import pytest

import analytics


def test_lift_change_compares_medians_of_the_window_edges():
    # Один удачный день в конце (150) не делает рост +50%: медиана трёх последних — 110.
    assert analytics.lift_change_pct([100, 100, 100, 105, 110, 150]) == pytest.approx(10.0)
    # Четыре тренировки — по две с краю, края не перекрываются.
    assert analytics.lift_change_pct([100, 100, 120, 120]) == pytest.approx(20.0)
    assert analytics.lift_change_pct([100]) is None
    assert analytics.lift_change_pct([0, 0, 0, 0]) is None


def test_dropping_lifts_pull_the_index_down():
    up = ([100, 100, 100, 110, 110, 110], 20)
    down = ([100, 100, 100, 90, 90, 90], 20)
    index = analytics.strength_index([up, down])
    assert index.growth_pct == 0.0
    assert (index.lifts, index.up, index.flat, index.down) == (2, 1, 0, 1)


def test_one_lift_cannot_carry_the_index():
    # Жим — 80 подходов из 100, остальные четыре по 5. Без потолка итог был бы
    # +16% (0.8 × 20); с потолком доля жима 25%, после нормировки — 0.25/0.45.
    bench = ([100, 100, 100, 120, 120, 120], 80)
    rest = [([100, 100, 100, 100, 100, 100], 5)] * 4
    index = analytics.strength_index([bench, *rest])
    assert index.growth_pct == pytest.approx(11.1)
    assert (index.up, index.flat, index.down) == (1, 4, 0)


def test_no_index_without_two_weighted_sessions():
    assert analytics.strength_index([]) is None
    assert analytics.strength_index([([100], 3), ([0, 0, 0, 0], 12)]) is None
