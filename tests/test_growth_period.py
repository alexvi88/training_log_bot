"""/growth умеет не только окно в днях, но и один конкретный день."""

import datetime as dt

from handlers.admin import _growth_day


def test_words_for_today_and_yesterday():
    today = dt.date.today()
    assert _growth_day("сегодня") == today.isoformat()
    assert _growth_day("today") == today.isoformat()
    assert _growth_day("Вчера") == (today - dt.timedelta(days=1)).isoformat()


def test_dates_in_every_shape_the_admin_types():
    today = dt.date.today()
    assert _growth_day("2026-02-03") == "2026-02-03"
    assert _growth_day("03.02.2026") == "2026-02-03"
    # Без года — год текущий; день, который в этом году ещё не наступил, — прошлый.
    day_before = today - dt.timedelta(days=1)
    assert _growth_day(day_before.strftime("%d.%m")) == day_before.isoformat()
    ahead = today + dt.timedelta(days=30)
    assert _growth_day(ahead.strftime("%d.%m")) == ahead.replace(year=ahead.year - 1).isoformat()


def test_nonsense_is_not_silently_taken_for_a_day():
    assert _growth_day("позавчера") is None
    assert _growth_day("неделя") is None
