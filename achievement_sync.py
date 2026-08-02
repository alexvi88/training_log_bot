"""Keeps the stored achievement set in sync with the workouts backing it.

Badges are derived state: every code in the `achievements` table is a claim
about the user's finished workouts ("поднял 220кг", "10 тонн", "52 недели
подряд"). Awarding is not enough — a workout logged with a typo (500 instead of
50) unlocks the whole weight-club ladder, and deleting or correcting that
workout has to take those badges back, otherwise the profile keeps a permanent
trophy for a set that no longer exists anywhere in the history.

`evaluate_after_finish` is the award-only path used when a workout is finished
(it never revokes, so a badge can't flicker off mid-celebration); `resync` is
the full recomputation used after a delete or an edit.
"""

import datetime as dt
import logging

import achievements
import analytics
import db
import formatting
import view_builder

logger = logging.getLogger(__name__)


async def _aggregate_context(user_id: int) -> achievements.AchievementContext:
    """Lifetime totals only — the per-workout fields stay None so a caller can
    fill them in for whichever workout it is evaluating.

    Weights are normalized to kilograms here. The thresholds behind "🏅 Клуб 220"
    and the tonnage badges are in kg (as the field names say), but the DB stores
    whatever unit the user picked — so a lb user was measured against kg
    thresholds and cleared "Клуб 100" with a 100 lb (45 kg) lift, and switching
    kg → lb multiplied every stored weight by 2.2 and handed out all four
    weight clubs at once. Badges are never revoked by the award-only path, so
    that grade of wrong is permanent.
    """
    user = await db.get_user(user_id)
    unit = user["unit"] if user else "kg"
    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id)]
    extremes = await db.achievement_extremes(user_id)
    food_days = [dt.date.fromisoformat(d) for d in await db.list_food_entry_dates(user_id)]
    return achievements.AchievementContext(
        total_workouts=await db.count_workouts(user_id),
        lifetime_tonnage_kg=formatting.to_kg(
            (await db.hall_of_fame_aggregates(user_id))["tonnage"], unit
        ),
        best_week_streak=analytics.max_week_streak(dates),
        max_weight_kg=formatting.to_kg(await db.max_weight_ever(user_id), unit),
        distinct_exercises=await db.count_distinct_exercises_used(user_id),
        distinct_groups=extremes["distinct_groups"],
        max_session_sets=extremes["max_sets"],
        max_session_tonnage_kg=formatting.to_kg(extremes["max_tonnage"], unit),
        max_session_exercises=extremes["max_exercises"],
        has_superset=bool(extremes["has_superset"]),
        max_bodyweight_reps=extremes["max_bw_reps"],
        # Час, как и у «Ранней пташки», пока серверный — единый корень B3
        # (хранение в UTC) чинится миграцией, и обе ачивки поедут вместе с ней.
        early_workouts=extremes["early_workouts"],
        has_weekend_pair=achievements.weekend_pair_exists(dates),
        all_weekdays_covered=len({d.weekday() for d in dates}) == 7,
        has_dec31=any((d.month, d.day) == (12, 31) for d in dates),
        max_rpe=extremes["max_rpe"],
        rpe_sets=extremes["rpe_sets"],
        bodyweight_logs=await db.count_bodyweight_logs(user_id),
        food_diary_best_run=achievements.longest_daily_run(food_days),
    )


async def evaluate_after_finish(
    user_id: int, workout_id: int, started_at: dt.datetime, duration_seconds: float | None
) -> list[str]:
    """Award any achievements the just-finished workout unlocked and return the
    new codes.

    Called after the workout is marked finished, so lifetime aggregates already
    include it. Never raises into the finish flow — a badge is a bonus, not a
    reason to break saving the workout.
    """
    try:
        ctx = await _aggregate_context(user_id)
        ctx.workout_start_hour = started_at.hour
        ctx.workout_date = started_at.date()
        ctx.workout_duration_seconds = duration_seconds
        return await db.award_achievements(user_id, achievements.earned_codes(ctx))
    except Exception:
        logger.exception("Achievement evaluation failed for workout %s", workout_id)
        return []


async def _earned_now(user_id: int) -> set[str]:
    """Every code the user's current history qualifies for, recomputed from
    scratch: lifetime aggregates plus the one-off codes any single workout can
    unlock (early bird / night owl / marathon / 1 января)."""
    ctx = await _aggregate_context(user_id)
    codes = achievements.earned_codes(ctx)
    for workout in await db.list_finished_workouts_meta(user_id):
        started = dt.datetime.fromisoformat(workout["started_at"])
        ctx.workout_start_hour = started.hour
        ctx.workout_date = started.date()
        # The duration lookup is a query per workout, so it is skipped once the
        # only badge it can add is already accounted for.
        ctx.workout_duration_seconds = (
            None if "marathon" in codes else await view_builder.workout_duration_seconds(workout)
        )
        codes |= achievements.earned_codes(ctx)
    return codes


async def resync(user_id: int) -> tuple[list[str], list[str]]:
    """Recompute the whole badge set from the surviving workouts, awarding what
    is newly true and revoking what no longer is. Returns (added, removed).

    Editing can go either way: dropping a bogus 500кг set costs the weight
    clubs, while correcting a date can complete a streak. Both directions are
    applied so the grid always matches the history behind it.

    Like the finish-time path, this never raises into the caller — a stale badge
    is better than a delete or an edit that appears to fail.
    """
    try:
        earned = await _earned_now(user_id)
        held = await db.list_achievement_codes(user_id)
        added = await db.award_achievements(user_id, earned - held)
        removed = await db.revoke_achievements(user_id, held - earned)
        return added, removed
    except Exception:
        logger.exception("Achievement resync failed for user %s", user_id)
        return [], []
