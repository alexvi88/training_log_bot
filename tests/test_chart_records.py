"""Огоньки рекордов на графике прогресса (charts.record_flags) — порт
`ProgressRecords` iOS-приложения: ошибка «≥ вместо >» рассыпала бы отметки по
каждой ровной полке, а отметка на первой точке звала бы рекордом начало графика."""
import datetime as dt

import charts


def test_first_point_is_never_a_record():
    assert charts.record_flags([100.0]) == [False]
    assert charts.record_flags([]) == []


def test_only_strictly_higher_than_everything_before():
    values = [100.0, 105.0, 105.0, 103.0, 110.0, 108.0, 111.0]
    assert charts.record_flags(values) == [False, True, False, False, True, False, True]


def test_dip_then_recovery_below_peak_is_not_a_record():
    assert charts.record_flags([120.0, 90.0, 100.0, 115.0]) == [False, False, False, False]


def test_chart_renders_with_record_marks():
    base = dt.datetime(2026, 1, 1)
    points = [(base + dt.timedelta(days=7 * i), v) for i, v in enumerate([100.0, 104.0, 102.0, 108.0])]
    png = charts.render_metric_over_sessions(
        points, "Жим — e1RM", "e1RM", show_weekly_rate=False, mark_records=True
    )
    assert png.startswith(b"\x89PNG")
