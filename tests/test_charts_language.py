"""Кириллица, запечённая в PNG, невидима ни одному текстовому тесту: `render_*`
возвращает байты растра, а не строку. Проверить язык подписи можно двумя
способами — оба применены здесь:

1. Собственные строки charts.py (суффикс тренда, подпись коридора объёма,
   заголовок карточки, кавычки цитаты) вынесены в отдельные функции
   (`_trend_title`, `_volume_norm_label`, `_workout_card_header`,
   `_quote_marks`), которые возвращают str, а не сразу зовут `ax.text`.
   Их можно проверить напрямую — без matplotlib и без PNG.
2. Для end-to-end подстраховки (регрессия вида «кто-то вписал кириллицу
   литералом прямо в вызов ax.text») matplotlib патчится: `Axes.text`,
   `Axes.set_title`, `Axes.set_xlabel`, `Axes.set_ylabel` оборачиваются так,
   чтобы каждая переданная строка складывалась в список, а рендер шёл как
   обычно (patch не подменяет поведение, только подсматривает за
   аргументами). После рендера каждого экрана под lang="en" список
   проверяется на отсутствие кириллицы.

Русские тексты, которые приходят в charts.py как параметры (headline, badge,
title, ylabel, volume_title, lifts_title, note, tiles, ...) собирает
formatting.py — это не территория этой проверки: здесь только то, что
charts.py решает сам.
"""
import contextlib
import datetime as dt
import re

import matplotlib.axes

import charts
import i18n
from analytics import Trend

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def setup_function(_):
    i18n.reload()


@contextlib.contextmanager
def _capture_drawn_text():
    """Патчит методы matplotlib, которыми charts.py кладёт текст на холст, и
    собирает все переданные строки — без завязки на пиксели растра."""
    drawn: list[str] = []
    originals = {
        name: getattr(matplotlib.axes.Axes, name)
        for name in ("text", "set_title", "set_xlabel", "set_ylabel")
    }

    def _wrap(name, original):
        def wrapped(self, *args, **kwargs):
            # ax.text(x, y, s, ...) — строка третьим позиционным; set_title/
            # set_xlabel/set_ylabel получают её первым.
            s = args[2] if name == "text" else args[0]
            if isinstance(s, str):
                drawn.append(s)
            return original(self, *args, **kwargs)
        return wrapped

    for name, original in originals.items():
        setattr(matplotlib.axes.Axes, name, _wrap(name, original))
    try:
        yield drawn
    finally:
        for name, original in originals.items():
            setattr(matplotlib.axes.Axes, name, original)


# ---------- собственные строки charts.py, напрямую ----------


def test_trend_title_suffix_has_no_cyrillic_in_english():
    trend = Trend(slope_per_week=1.5, direction="up", intercept=0.0)
    with i18n.use_lang("en"):
        title = charts._trend_title("Bench press", trend, [100.0, 110.0], show_weekly_rate=True)
    assert not CYRILLIC.search(title)
    assert "/wk" in title


def test_trend_title_suffix_stays_russian_by_default():
    trend = Trend(slope_per_week=1.5, direction="up", intercept=0.0)
    with i18n.use_lang("ru"):
        title = charts._trend_title("Жим лёжа", trend, [100.0, 110.0], show_weekly_rate=True)
    assert "/нед" in title


def test_volume_norm_label_has_no_cyrillic_in_english():
    with i18n.use_lang("en"):
        label = charts._volume_norm_label(10, 20)
    assert not CYRILLIC.search(label)
    assert "10" in label and "20" in label


def test_workout_card_header_has_no_cyrillic_in_english():
    with i18n.use_lang("en"):
        header = charts._workout_card_header()
    assert not CYRILLIC.search(header)
    assert header == "WORKOUT"


def test_quote_marks_are_straight_in_english_and_guillemets_in_russian():
    """TONE_OF_VOICE, ## English voice: «в английской строке кавычки прямые,
    ёлочки остаются в русской»."""
    with i18n.use_lang("en"):
        assert charts._quote_marks() == ('"', '"')
    with i18n.use_lang("ru"):
        assert charts._quote_marks() == ("«", "»")


# ---------- end-to-end: подстраховка через перехват matplotlib ----------


def test_workout_card_render_has_no_cyrillic_in_english():
    with i18n.use_lang("en"), _capture_drawn_text() as drawn:
        charts.render_workout_card(
            "Push day", ["Bench press", "  100x8, 105x8"], "3 exercises done", note="★ new record"
        )
    assert drawn, "перехват не увидел ни одной строки — проверь патч"
    offenders = [s for s in drawn if CYRILLIC.search(s)]
    assert not offenders, offenders


def test_menu_dashboard_render_has_no_cyrillic_in_english():
    with i18n.use_lang("en"), _capture_drawn_text() as drawn:
        charts.render_menu_dashboard(
            headline="9 weeks straight",
            badge="HEAVYWEIGHT",
            tiles=[("WORKOUTS / 30D", "12"), ("TONNAGE / 7D", "24.5t")],
            volume_rows=[("BACK", 14, "high"), ("CHEST", 9, "in_range"), ("LEGS", 0, "low")],
            volume_title="VOLUME / 7 DAYS",
            lift_tiles=[("BENCH PRESS", "+12%", "112kg vs 100kg")],
            lifts_title="e1RM GROWTH / 8 WEEKS",
            lifts_note="vs the 8 weeks before",
        )
    assert drawn
    offenders = [s for s in drawn if CYRILLIC.search(s)]
    assert not offenders, offenders


def test_metric_over_sessions_render_has_no_cyrillic_in_english():
    points = [
        (dt.datetime(2026, 7, 1) + dt.timedelta(days=7 * i), 100.0 + i) for i in range(4)
    ]
    with i18n.use_lang("en"), _capture_drawn_text() as drawn:
        charts.render_metric_over_sessions(points, "Bench press — e1RM", "e1RM")
    assert drawn
    offenders = [s for s in drawn if CYRILLIC.search(s)]
    assert not offenders, offenders
