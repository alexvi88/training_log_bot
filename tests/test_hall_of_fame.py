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
        tonnage_equivalent="Это как 25 слонов 🐘.",
        best_week_streak=6,
        longest_workout_seconds=5400,
        top_lifts=[("Жим лёжа", 120.0, 3, 132.0)],
    )
    assert "42" in text
    assert "125 тонн" in text
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
    assert "Поднято за всё время" in text


def test_hall_of_fame_folds_the_whole_record_list():
    lifts = [(f"Упражнение {i}", 100.0 + i, 5, 130.0 - i) for i in range(8)]
    text = formatting.build_hall_of_fame(
        total_workouts=42, tonnage_kg=125000, tonnage_equivalent=None,
        best_week_streak=6, longest_workout_seconds=5400, top_lifts=lifts,
    )
    open_part, sep, folded = text.partition("<blockquote expandable>")
    assert sep
    assert open_part.count("• ") == 0, "the list is not split — it folds whole"
    assert folded.count("• ") == len(lifts)


def test_hall_of_fame_short_list_is_not_folded():
    text = formatting.build_hall_of_fame(
        total_workouts=3, tonnage_kg=1000, tonnage_equivalent=None,
        best_week_streak=0, longest_workout_seconds=0,
        top_lifts=[("Жим лёжа", 120.0, 3, 132.0)],
    )
    assert "blockquote" not in text


def test_hall_of_fame_bodyweight_record_counts_reps():
    text = formatting.build_hall_of_fame(
        total_workouts=10, tonnage_kg=5000, tonnage_equivalent=None,
        best_week_streak=0, longest_workout_seconds=0,
        top_lifts=[("Подтягивания", 0.0, 15, 0.0)],
    )
    assert "Подтягивания — 15 повторов" in text
    assert "e1RM" not in text


def test_hall_of_fame_trims_records_to_fit_the_message():
    lifts = [(f"Очень длинное название упражнения номер {i}", 100.0, 5, 130.0) for i in range(200)]
    text = formatting.build_hall_of_fame(
        total_workouts=42, tonnage_kg=125000, tonnage_equivalent=None,
        best_week_streak=6, longest_workout_seconds=5400, top_lifts=lifts,
        max_chars=1000,
    )
    assert formatting.telegram_length(text) <= 1000
    assert "показано" in text
