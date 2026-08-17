"""Dashboard stats (analytics.compute_dashboard) + its text/card formatters."""

import datetime as dt

import pytest

import analytics
import charts
import formatting
from formatting import ExerciseBlockView


def d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


@pytest.fixture(autouse=True)
def _clear_heatmap_cache():
    from handlers.workout import _heatmap_cache

    _heatmap_cache.clear()
    yield
    _heatmap_cache.clear()


def test_dashboard_empty():
    dash = analytics.compute_dashboard([], d("2026-06-26"))
    assert dash == analytics.Dashboard(0, 0, 0, None, 0)


def test_dashboard_counts_and_last_workout():
    today = d("2026-06-26")  # Friday
    dates = [d("2026-06-26"), d("2026-06-24"), d("2026-05-01"), d("2026-03-01")]
    dash = analytics.compute_dashboard(dates, today)
    assert dash.total_workouts == 4
    assert dash.days_since_last == 0
    # current week is Mon 2026-06-22 .. Sun: two workouts fall in it
    assert dash.this_week == 2
    # last 30 days: 06-26, 06-24, and not 05-01 (56 days) -> 2
    assert dash.last_30_days == 2


def test_dashboard_same_day_counts_twice():
    today = d("2026-06-26")
    dash = analytics.compute_dashboard([d("2026-06-26"), d("2026-06-26")], today)
    assert dash.total_workouts == 2
    assert dash.this_week == 2


def test_week_streak_consecutive():
    today = d("2026-06-26")  # week of Mon 06-22
    dates = [d("2026-06-24"), d("2026-06-17"), d("2026-06-10"), d("2026-06-03")]
    dash = analytics.compute_dashboard(dates, today)
    assert dash.week_streak == 4


def test_week_streak_grace_for_empty_current_week():
    # Nothing yet this week, but last week had a workout — streak stays alive.
    today = d("2026-06-26")
    dates = [d("2026-06-19"), d("2026-06-12")]
    dash = analytics.compute_dashboard(dates, today)
    assert dash.this_week == 0
    assert dash.week_streak == 2


def test_week_streak_breaks_after_two_empty_weeks():
    today = d("2026-06-26")
    # Most recent workout was 2 weeks ago -> streak reset.
    dates = [d("2026-06-08")]
    dash = analytics.compute_dashboard(dates, today)
    assert dash.week_streak == 0


def test_plural_ru():
    forms = ("неделя", "недели", "недель")
    assert formatting.plural_ru(1, forms) == "неделя"
    assert formatting.plural_ru(2, forms) == "недели"
    assert formatting.plural_ru(5, forms) == "недель"
    assert formatting.plural_ru(11, forms) == "недель"
    assert formatting.plural_ru(21, forms) == "неделя"


def test_build_workout_card_text():
    started = dt.datetime(2026, 6, 26, 18, 0)
    blocks = [
        ExerciseBlockView(
            group_name="грудь",
            exercise_name="Жим лёжа",
            sets=[(100.0, 8), (100.0, 8), (60.0, 12)],
        ),
        ExerciseBlockView(
            group_name="спина",
            exercise_name="Тяга",
            sets=[(80.0, 10), (80.0, 10)],
        ),
    ]
    title, body, footer, note = formatting.build_workout_card(
        started, blocks, note="Спал хорошо", unit="kg"
    )
    assert title.startswith("26.06.2026")
    assert any("Жим лёжа [ГРУДЬ]" in line for line in body)
    assert any("Тяга [СПИНА]" in line for line in body)
    assert note == "Спал хорошо"
    assert footer.startswith("2 упражнения")
    assert "5 подходов" in footer


def test_render_workout_card_returns_png():
    png = charts.render_workout_card(
        "26.06.2026 (пт)", ["Жим лёжа [ГРУДЬ]", "  100×8, 100×8"], "1 упражнение · 2 рабочих сета · 1600 кг",
        note="Хорошая тренировка",
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_list_finished_workout_dates(user_id, fresh_db):
    db = fresh_db
    await db.create_finished_workout(user_id, "2026-06-20T10:00:00", "2026-06-20T11:00:00")
    await db.create_finished_workout(user_id, "2026-06-26T10:00:00", "2026-06-26T11:00:00")
    # An active (unfinished) workout must not appear.
    await db.create_workout(user_id)
    dates = await db.list_finished_workout_dates(user_id)
    assert dates == ["2026-06-20", "2026-06-26"]


@pytest.mark.asyncio
async def test_menu_view_plain_text_for_new_user(user_id, fresh_db):
    from handlers.workout import _menu_view

    text, png = await _menu_view(user_id)
    assert "АТЛЕТ" in text
    assert png is None


@pytest.mark.asyncio
async def test_menu_view_shows_onboarding_for_new_user(user_id, fresh_db):
    from handlers.workout import _menu_view, _onboarding

    text, _ = await _menu_view(user_id)
    assert text == _onboarding()


@pytest.mark.asyncio
async def test_menu_view_includes_heatmap_once_history_exists(user_id, fresh_db):
    db = fresh_db
    started = dt.datetime.now() - dt.timedelta(days=3)
    await db.create_finished_workout(
        user_id, started.isoformat(), (started + dt.timedelta(hours=1)).isoformat()
    )
    from handlers.workout import _menu_view

    text, png = await _menu_view(user_id)
    assert "АТЛЕТ" in text
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_menu_view_heatmap_cached_across_calls(user_id, fresh_db, monkeypatch):
    db = fresh_db
    started = dt.datetime.now() - dt.timedelta(days=3)
    await db.create_finished_workout(
        user_id, started.isoformat(), (started + dt.timedelta(hours=1)).isoformat()
    )
    from handlers.workout import _menu_view

    calls = 0
    # Меню рисует сводку целиком (render_menu_dashboard), а не одну тепловую
    # карту: сама карта осталась отдельной функцией и своими тестами ниже.
    real_render = charts.render_menu_dashboard

    def _counting_render(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_render(*args, **kwargs)

    monkeypatch.setattr(charts, "render_menu_dashboard", _counting_render)

    _, png1 = await _menu_view(user_id)
    _, png2 = await _menu_view(user_id)
    assert calls == 1  # second call is served from cache, no re-render
    assert png1 == png2

    # A new finished workout invalidates the cache.
    started2 = dt.datetime.now() - dt.timedelta(days=1)
    await db.create_finished_workout(
        user_id, started2.isoformat(), (started2 + dt.timedelta(hours=1)).isoformat()
    )
    _, png3 = await _menu_view(user_id)
    assert calls == 2
    assert png3 != png1




async def test_charts_render_correctly_from_concurrent_threads():
    """Renders run in worker threads (asyncio.to_thread). pyplot keeps one global
    figure registry per process, so two users opening "Прогресс" at the same
    moment were racing for it; building figures directly avoids that."""
    import asyncio
    import datetime as dt

    import charts

    base = dt.datetime(2026, 5, 4, 12, 0)
    points = [(base + dt.timedelta(days=i), 100.0 + i) for i in range(12)]

    pngs = await asyncio.gather(*[
        asyncio.to_thread(
            charts.render_metric_over_sessions, points, f"График {i}", "кг"
        )
        for i in range(8)
    ])

    assert all(png.startswith(b"\x89PNG") for png in pngs)
    assert all(len(png) > 1000 for png in pngs)


async def test_onboarding_teaches_the_set_format_and_the_ai_trainer(fresh_db, user_id):
    """Первый экран учит ровно одному, чего человек не угадает сам, — формату
    строки подхода, — и называет AI-тренера: программу под себя и разбор своей
    истории иначе находят случайно или никогда."""
    from handlers import workout

    text, _png = await workout._menu_view(user_id)

    assert "100 8" in text
    assert "AI-тренер" in text
    assert "программу" in text


async def test_onboarding_fits_on_one_screen(fresh_db, user_id):
    """Кнопки под текстом — то, ради чего экран существует; из-за полотна в
    четырнадцать строк «НАЧАТЬ ТРЕНИРОВКУ» уезжала под сгиб. Ограничение
    грубое, но оно ловит ровно тот регресс, который уже случался: онбординг
    снова начал расти списком фич."""
    from handlers import workout

    text, _png = await workout._menu_view(user_id)

    assert len(text.splitlines()) <= 8
