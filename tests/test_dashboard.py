"""Dashboard stats (analytics.compute_dashboard) + its text/card formatters."""

import datetime as dt

import pytest

import analytics
import charts
import formatting
from formatting import ExerciseBlockView


def d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def test_dashboard_empty():
    dash = analytics.compute_dashboard([], d("2026-06-26"))
    assert dash == analytics.Dashboard(0, 0, 0, None, 0)
    # New user gets no dashboard block at all.
    assert formatting.dashboard_stat_lines(dash) == []


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


def test_dashboard_stat_lines_hides_short_streak():
    dash = analytics.Dashboard(
        total_workouts=3, this_week=1, last_30_days=3, days_since_last=1, week_streak=1
    )
    lines = formatting.dashboard_stat_lines(dash)
    labels = [label for label, _ in lines]
    assert "Серия: " not in labels  # streak < 2 is not motivating, hidden
    assert ("Последние 30 дней: ", "3 тренировки") in lines


def test_dashboard_stat_lines_shows_streak_and_plurals():
    dash = analytics.Dashboard(
        total_workouts=21, this_week=2, last_30_days=8, days_since_last=0, week_streak=5
    )
    lines = formatting.dashboard_stat_lines(dash)
    assert ("Серия: ", "5 недель подряд") in lines
    assert ("Эта неделя: ", "2 тренировки") in lines
    assert ("Последние 30 дней: ", "8 тренировок") in lines


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
    assert "5 сетов" in footer


def test_render_workout_card_returns_png():
    png = charts.render_workout_card(
        "26.06.2026 (пт)", ["Жим лёжа [ГРУДЬ]", "  100×8, 100×8"], "1 упражнение · 2 рабочих сета · 1600 кг",
        note="Хорошая тренировка",
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_year_heatmap_returns_png():
    today = dt.date(2026, 7, 12)
    counts = {
        dt.date(2026, 7, 10): 1,
        dt.date(2026, 7, 8): 2,
        dt.date(2026, 7, 6): 5,  # multi-workout day, must render as a single filled square
        dt.date(2025, 7, 20): 1,  # near the year-ago edge of the grid
        dt.date(2020, 1, 1): 1,  # far outside the grid, must be ignored
    }
    start = dt.date(2025, 7, 13)  # roughly a year back, snapped to Monday inside the renderer
    stat_lines = [("Последние 30 дней: ", "4 тренировки")]
    png = charts.render_year_heatmap(counts, today, start, stat_lines)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_year_heatmap_handles_empty_counts():
    png = charts.render_year_heatmap({}, dt.date(2026, 7, 12), dt.date(2026, 7, 1), [])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_year_heatmap_starts_at_first_workout():
    """A brand-new user shouldn't get 52 empty weeks padded onto the grid."""
    today = dt.date(2026, 7, 12)
    start = dt.date(2026, 7, 6)
    stat_lines = [("Последние 30 дней: ", "1 тренировка")]
    png = charts.render_year_heatmap({dt.date(2026, 7, 10): 1}, today, start, stat_lines)
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
async def test_menu_view_shows_onboarding_steps_for_new_user(user_id, fresh_db):
    from handlers.workout import _ONBOARDING, _menu_view

    text, _ = await _menu_view(user_id)
    assert text == _ONBOARDING
    assert "Выбирай группу мышц" in text


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
async def test_menu_view_shows_weekly_volume(user_id, fresh_db):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    today = dt.datetime.now()
    workout_id = await db.create_finished_workout(
        user_id, today.isoformat(), (today + dt.timedelta(hours=1)).isoformat()
    )
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 1, 0, 100, 8)
    await db.add_set(block_id, ex_id, 2, 0, 100, 8)

    from handlers.workout import _menu_view

    text, _ = await _menu_view(user_id)
    assert "Объём за неделю:" in text
    assert "Грудь: <b>2</b>" in text
