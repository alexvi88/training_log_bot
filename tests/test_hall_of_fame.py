"""Hall of Fame: analytics.max_week_streak, db aggregates, and the built screen."""
import datetime as dt

import pytest

import analytics
import formatting
from handlers import history


def test_max_week_streak_finds_best_run_anywhere():
    d = dt.date(2026, 1, 5)  # a Monday
    # weeks 0,1,2 (a 3-run), gap, then weeks 5,6 (a 2-run)
    dates = [d, d + dt.timedelta(weeks=1), d + dt.timedelta(weeks=2),
             d + dt.timedelta(weeks=5), d + dt.timedelta(weeks=6)]
    assert analytics.max_week_streak(dates) == 3


def test_max_week_streak_empty():
    assert analytics.max_week_streak([]) == 0


def test_hall_of_fame_screen_empty():
    text = formatting.build_hall_of_fame(0, 0, None, 0, 0, [])
    assert "Пока пусто" in text


def test_hall_of_fame_screen_populated():
    text = formatting.build_hall_of_fame(
        total_workouts=42,
        tonnage_kg=125000,
        tonnage_equivalent="Это как 25 × 🐘 слон.",
        best_week_streak=6,
        longest_workout_seconds=5400,
        top_lifts=[("Жим лёжа", 120.0, 3, 132.0)],
    )
    assert "42" in text
    assert "125.0 т" in text
    assert "Жим лёжа" in text
    assert "1 ч 30 мин" in text


@pytest.mark.asyncio
async def test_aggregates_and_build_text(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", group_id)
    wid = await db.create_workout(user_id)
    block_id = await db.create_block(wid, "single")
    await db.add_block_exercise(block_id, bench, 0)
    await db.add_set(block_id, bench, 0, 0, 100.0, 5, None)
    await db.add_set(block_id, bench, 1, 0, 100.0, 5, None)
    await db.finish_workout(wid, None)

    agg = await db.hall_of_fame_aggregates(user_id)
    assert agg["tonnage"] == 1000.0  # 100*5 + 100*5
    assert agg["sets_count"] == 2
    assert await db.max_weight_ever(user_id) == 100.0
    assert await db.count_distinct_exercises_used(user_id) == 1

    text = await history.build_hall_of_fame_text(user_id)
    assert "Жим лёжа" in text
    assert "ЗАЛ СЛАВЫ" in text
