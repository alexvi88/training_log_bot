"""achievements.earned_codes detection + db award/list + formatting screens."""
import datetime as dt

import pytest

import achievements
import formatting


def _ctx(**kw):
    base = dict(
        total_workouts=0, lifetime_tonnage_kg=0.0, best_week_streak=0,
        max_weight_kg=0.0, distinct_exercises=0,
    )
    base.update(kw)
    return achievements.AchievementContext(**base)


def test_workout_count_tiers():
    assert "first" in achievements.earned_codes(_ctx(total_workouts=1))
    assert "w10" not in achievements.earned_codes(_ctx(total_workouts=9))
    got = achievements.earned_codes(_ctx(total_workouts=100))
    assert {"first", "w10", "w25", "w50", "w100"} <= got


def test_weight_clubs():
    assert achievements.earned_codes(_ctx(max_weight_kg=145)) >= {"club100", "club140"}
    assert "club180" not in achievements.earned_codes(_ctx(max_weight_kg=145))


def test_tonnage_clubs():
    assert "ton100" in achievements.earned_codes(_ctx(lifetime_tonnage_kg=120_000))
    assert "ton1000" in achievements.earned_codes(_ctx(lifetime_tonnage_kg=1_000_000))


def test_streak_medals():
    assert achievements.earned_codes(_ctx(best_week_streak=52)) >= {"streak4", "streak12", "streak26", "streak52"}


def test_special_time_of_day():
    assert "early_bird" in achievements.earned_codes(_ctx(workout_start_hour=6))
    assert "night_owl" in achievements.earned_codes(_ctx(workout_start_hour=23))
    assert "early_bird" not in achievements.earned_codes(_ctx(workout_start_hour=12))


def test_special_marathon_and_new_year():
    assert "marathon" in achievements.earned_codes(_ctx(workout_duration_seconds=7300))
    assert "new_year" in achievements.earned_codes(_ctx(workout_date=dt.date(2026, 1, 1)))
    assert "new_year" not in achievements.earned_codes(_ctx(workout_date=dt.date(2026, 1, 2)))


def test_every_earned_code_has_catalog_entry():
    # A code with no metadata would crash the formatter.
    all_codes = achievements.earned_codes(
        _ctx(total_workouts=100, lifetime_tonnage_kg=2_000_000, best_week_streak=52,
             max_weight_kg=300, distinct_exercises=50, workout_start_hour=6,
             workout_date=dt.date(2026, 1, 1), workout_duration_seconds=9000)
    )
    assert all(c in achievements.BY_CODE for c in all_codes)


def test_format_new_achievements():
    assert formatting.format_new_achievements([]) is None
    line = formatting.format_new_achievements(["first"])
    assert "Первый шаг" in line and "достижение" in line.lower()


def test_build_achievements_screen_counts():
    text = formatting.build_achievements_screen({"first", "w10"})
    assert f"2/{len(achievements.CATALOG)}" in text
    assert "🔒" in text  # locked ones listed


@pytest.mark.asyncio
async def test_award_returns_only_new(fresh_db, user_id):
    db = fresh_db
    first = await db.award_achievements(user_id, {"first", "w10"})
    assert set(first) == {"first", "w10"}
    again = await db.award_achievements(user_id, {"first", "w10", "w25"})
    assert again == ["w25"]  # only the genuinely new one
    assert await db.list_achievement_codes(user_id) == {"first", "w10", "w25"}
