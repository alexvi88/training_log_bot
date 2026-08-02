"""Расширение каталога ачивок: сессионные, календарные, RPE, разнообразие и
кросс-доменные (вес, еда).

Все новые бейджи — агрегаты «лучшее/счётчик за всё время», поэтому resync
пересчитывает их без обхода тренировок по одной, а правка истории честно
отзывает то, что перестало быть правдой.
"""
import datetime as dt

import pytest

import achievement_sync
import achievements
from achievements import AchievementContext, earned_codes

pytestmark = pytest.mark.asyncio


def _ctx(**kwargs) -> AchievementContext:
    return AchievementContext(
        total_workouts=kwargs.pop("total_workouts", 1),
        lifetime_tonnage_kg=kwargs.pop("lifetime_tonnage_kg", 0.0),
        best_week_streak=kwargs.pop("best_week_streak", 0),
        max_weight_kg=kwargs.pop("max_weight_kg", 0.0),
        distinct_exercises=kwargs.pop("distinct_exercises", 0),
        **kwargs,
    )


# ---------- чистая логика ----------


async def test_every_catalog_code_is_reachable():
    """Бейдж, который нельзя заработать, — это баг каталога: он вечно висит в
    «Ещё не открыты» и обесценивает счётчик N/40."""
    maxed = AchievementContext(
        total_workouts=1000, lifetime_tonnage_kg=2_000_000, best_week_streak=60,
        max_weight_kg=300, distinct_exercises=100, distinct_groups=10,
        max_session_sets=40, max_session_tonnage_kg=9_000, max_session_exercises=12,
        has_superset=True, max_bodyweight_reps=30, early_workouts=20,
        has_weekend_pair=True, all_weekdays_covered=True, has_dec31=True,
        max_rpe=10.0, rpe_sets=200, bodyweight_logs=50, food_diary_best_run=10,
        workout_start_hour=6, workout_date=dt.date(2026, 1, 1),
        workout_duration_seconds=3 * 3600,
    )
    earned = earned_codes(maxed)
    # night_owl взаимоисключающ с early_bird в одном контексте — проверяем отдельно.
    earned |= earned_codes(_ctx(workout_start_hour=23))
    assert earned == {a.code for a in achievements.CATALOG}


async def test_session_records_are_lifetime_maxima_of_per_session_numbers():
    ctx = _ctx(max_session_sets=25, max_session_tonnage_kg=5_000, max_session_exercises=8)
    got = earned_codes(ctx)
    assert {"vol25", "session5t", "combine8"} <= got

    below = _ctx(max_session_sets=24, max_session_tonnage_kg=4_999, max_session_exercises=7)
    assert {"vol25", "session5t", "combine8"}.isdisjoint(earned_codes(below))


async def test_rpe_family_requires_logged_rpe():
    assert "rpe10" in earned_codes(_ctx(max_rpe=10.0))
    assert "rpe10" not in earned_codes(_ctx(max_rpe=9.5))
    assert "rpe10" not in earned_codes(_ctx(max_rpe=None))
    assert "rpe100" in earned_codes(_ctx(rpe_sets=100))


async def test_weekend_pair_means_same_weekend_not_any_two_days():
    sat, sun = dt.date(2026, 8, 1), dt.date(2026, 8, 2)
    assert achievements.weekend_pair_exists([sat, sun])
    assert not achievements.weekend_pair_exists([sat, sun + dt.timedelta(weeks=1)])


async def test_food_run_counts_consecutive_days_only():
    days = [dt.date(2026, 8, d) for d in (1, 2, 3, 4, 5, 6, 7)]
    assert achievements.longest_daily_run(days) == 7
    with_gap = days[:3] + days[4:]
    assert achievements.longest_daily_run(with_gap) < 7
    assert "food7" in earned_codes(_ctx(food_diary_best_run=7))


# ---------- интеграция с БД и resync ----------


async def _workout(db, user_id, *, sets, started="2026-07-01T10:00:00", block_type="single",
                   group="Спина", exercise="Тяга"):
    gid = await db.create_muscle_group(user_id, group)
    ex_id = await db.create_exercise(user_id, exercise, gid)
    workout_id = await db.create_workout(user_id, started_at=started)
    block_id = await db.create_block(workout_id, block_type)
    await db.add_block_exercise(block_id, ex_id, 0)
    for weight, reps, *rpe in sets:
        await db.append_set(block_id, ex_id, 0, weight, reps, rpe[0] if rpe else None)
    await db.finish_workout(workout_id, finished_at=started)
    return workout_id


async def test_superset_and_volume_awarded_from_real_history(fresh_db, user_id):
    db = fresh_db
    await _workout(db, user_id, sets=[(100.0, 5, 10.0)] * 25, block_type="superset")

    added, _removed = await achievement_sync.resync(user_id)

    assert {"superset1", "vol25", "rpe10", "session5t"} <= set(added)


async def test_deleting_the_record_workout_revokes_the_session_badges(fresh_db, user_id):
    """Бейдж — утверждение о существующей истории: удалил тренировку-рекорд,
    и «Объёмный день» обязан уйти вместе с ней."""
    db = fresh_db
    big = await _workout(db, user_id, sets=[(50.0, 5)] * 25)
    await _workout(db, user_id, sets=[(50.0, 5)] * 3, started="2026-07-03T10:00:00",
                   group="Грудь", exercise="Жим")
    await achievement_sync.resync(user_id)
    assert "vol25" in await db.list_achievement_codes(user_id)

    await db.discard_workout(big)
    _added, removed = await achievement_sync.resync(user_id)

    assert "vol25" in removed
    assert "vol25" not in await db.list_achievement_codes(user_id)


async def test_session_tonnage_threshold_is_in_kilograms(fresh_db, user_id):
    """5 тонн — это 5000 кг, а не 5000 фунтов: у lb-юзера сырые числа больше
    в 2.2 раза, и без нормализации «Пятитонник» доставался за 2.3 тонны."""
    db = fresh_db
    await db.update_user(user_id, unit="lb")
    # 5000 lb ≈ 2268 кг — не порог.
    await _workout(db, user_id, sets=[(100.0, 5)] * 10)

    added, _ = await achievement_sync.resync(user_id)

    assert "session5t" not in added


async def test_food_diary_run_awards_and_survives_resync(fresh_db, user_id):
    db = fresh_db
    await _workout(db, user_id, sets=[(50.0, 5)])
    for i in range(7):
        await db.add_food_entry(
            user_id, eaten_on=(dt.date(2026, 8, 1) + dt.timedelta(days=i)).isoformat(),
            description="приём",
        )

    added, _ = await achievement_sync.resync(user_id)

    assert "food7" in added


async def test_bodyweight_log_count_awards(fresh_db, user_id):
    db = fresh_db
    await _workout(db, user_id, sets=[(50.0, 5)])
    for _ in range(30):
        await db.add_bodyweight_log(user_id, 82.5)

    added, _ = await achievement_sync.resync(user_id)

    assert "bwlog30" in added


async def test_all_weekdays_needs_all_seven(fresh_db, user_id):
    db = fresh_db
    monday = dt.date(2026, 7, 6)
    for i in range(6):  # пн–сб, без воскресенья
        await _workout(
            db, user_id, sets=[(50.0, 5)],
            started=f"{(monday + dt.timedelta(days=i)).isoformat()}T10:00:00",
            group=f"Г{i}", exercise=f"У{i}",
        )
    added, _ = await achievement_sync.resync(user_id)
    assert "all_weekdays" not in added
    assert "weekend_double" not in set(added)  # суббота есть, воскресенья нет

    await _workout(
        db, user_id, sets=[(50.0, 5)],
        started=f"{(monday + dt.timedelta(days=6)).isoformat()}T10:00:00",
        group="Г7", exercise="У7",
    )
    added, _ = await achievement_sync.resync(user_id)
    assert {"all_weekdays", "weekend_double"} <= set(added)
