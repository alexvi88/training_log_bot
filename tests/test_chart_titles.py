"""Заголовок графика со стрелкой: тот же баг двойного знака, что и в тексте."""

import charts
from analytics import Trend


def _trend(slope: float, direction: str) -> Trend:
    return Trend(slope_per_week=slope, intercept=0.0, direction=direction)


def test_weekly_rate_title_prints_the_number_without_a_second_sign():
    title = charts._trend_title("Вес тела", _trend(-0.42, "down"), [80.0, 78.0], True)
    assert title == "Вес тела  ↓ 0.42/нед"
    assert "-" not in title


def test_total_change_title_prints_the_number_without_a_second_sign():
    """На проде было «↓ -38.0» — стрелка и знак дублировали друг друга."""
    assert charts._trend_title("Жим", _trend(-1.0, "down"), [120.0, 82.0], False) == "Жим  ↓ 38"


def test_total_change_title_drops_the_trailing_zero():
    assert charts._trend_title("Жим", _trend(1.0, "up"), [80.0, 82.5], False) == "Жим  ↑ 2.5"


def test_flat_trend_gets_the_neutral_arrow_and_a_bare_zero():
    assert charts._trend_title("Жим", _trend(0.0, "flat"), [80.0, 80.0], False) == "Жим  → 0"
