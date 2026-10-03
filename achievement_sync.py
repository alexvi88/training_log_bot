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
import timeutil
import view_builder

logger = logging.getLogger(__name__)


async def aggregate_context(user_id: int) -> achievements.AchievementContext:
    """Lifetime totals only — the per-workout fields stay None so a caller can
    fill them in for whichever workout it is evaluating.

    Public (no leading underscore) because it has a second caller besides this
    module: the achievements screen reuses it to know "current value" for the
    «Ближайшие» block (achievements.nearest_progress) — the same lifetime
    numbers that decide which badges are earned also say how close the locked
    ones are.

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
    tz_offset = int(user["tz_offset"]) if user else 0
    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id)]
    extremes = await db.achievement_extremes(user_id, tz_offset=tz_offset)
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
        # Местный час пользователя (см. db._local_hour) — раньше сверялся с
        # часом сервера (UTC), и мог разойтись с «Ранней пташкой» ниже, которая
        # для только что закрытой тренировки уже смещает час в местный.
        early_workouts=extremes["early_workouts"],
        has_weekend_pair=achievements.weekend_pair_exists(dates),
        all_weekdays_covered=len({d.weekday() for d in dates}) == 7,
        has_dec31=any((d.month, d.day) == (12, 31) for d in dates),
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
        ctx = await aggregate_context(user_id)
        user = await db.get_user(user_id)
        # started_at is the server clock (UTC) — shifted to the user's local
        # wall clock before reading hour/date, the same shift list_finished_
        # workout_dates already applies for has_dec31 above. Left as UTC, a
        # workout at 23:30 local (say, UTC-5) read as day-of-UTC could tag
        # "new_year" a day off from what "dec31"/the dashboard already agree on.
        local = timeutil.to_user_local(started_at, user)
        ctx.workout_start_hour = local.hour
        ctx.workout_date = local.date()
        ctx.workout_duration_seconds = duration_seconds
        return await db.award_achievements(user_id, achievements.earned_codes(ctx))
    except Exception:
        logger.exception("Achievement evaluation failed for workout %s", workout_id)
        return []


async def _earned_now(user_id: int) -> set[str]:
    """Every code the user's current history qualifies for, recomputed from
    scratch: lifetime aggregates plus the one-off codes any single workout can
    unlock (early bird / night owl / marathon / 1 января)."""
    ctx = await aggregate_context(user_id)
    user = await db.get_user(user_id)
    codes = achievements.earned_codes(ctx)
    for workout in await db.list_finished_workouts_meta(user_id):
        started = dt.datetime.fromisoformat(workout["started_at"])
        local = timeutil.to_user_local(started, user)
        ctx.workout_start_hour = local.hour
        ctx.workout_date = local.date()
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


async def earned_dates(user_id: int, codes) -> dict[str, str]:
    """code → когда значок был заработан на самом деле: начало первой
    тренировки, после которой история стала ему соответствовать.

    Нужен импорту истории: resync выдаёт значки разом, с отметкой «сейчас», и
    «Клуб 100», взятый в Hevy два года назад, выглядел взятым сегодня. Здесь
    история проигрывается по тренировкам в хронологическом порядке теми же
    правилами (achievements.earned_codes), а агрегаты считаются нарастающим
    итогом в памяти. Только для даты: выдаёт и отбирает значки по-прежнему
    resync, и код, для которого день не нашёлся, получает «сейчас».
    """
    wanted = set(codes)
    if not wanted:
        return {}
    user = await db.get_user(user_id)
    unit = user["unit"] if user else "kg"
    cur = await db.conn().execute(
        "SELECT w.id AS wid, w.started_at, w.finished_at, b.id AS block_id, s.exercise_id, "
        "e.primary_group_id AS grp, COALESCE(s.load_weight, s.weight) AS load, s.weight, s.reps "
        "FROM workouts w JOIN workout_blocks b ON b.workout_id = w.id "
        "JOIN sets s ON s.block_id = b.id JOIN exercises e ON e.id = s.exercise_id "
        "WHERE w.user_id = ? AND w.status = 'finished' ORDER BY w.started_at, w.id, s.id",
        (user_id,),
    )
    sessions: dict[int, dict] = {}
    for r in await cur.fetchall():
        item = sessions.setdefault(
            r["wid"], {"started_at": r["started_at"], "finished_at": r["finished_at"], "sets": []}
        )
        item["sets"].append(r)
    cur = await db.conn().execute(
        "SELECT logged_at FROM bodyweight_logs WHERE telegram_id = ? ORDER BY logged_at", (user_id,)
    )
    bw_logs = [r["logged_at"] for r in await cur.fetchall()]
    food_days = sorted(dt.date.fromisoformat(d) for d in await db.list_food_entry_dates(user_id))

    found: dict[str, str] = {}
    dates: list[dt.date] = []
    tonnage = 0.0
    max_weight = 0.0
    exercises: set[int] = set()
    groups: set[int] = set()
    max_sets = max_exercises = max_bw_reps = early = 0
    max_session_tonnage = 0.0
    has_superset = False
    for session in sessions.values():
        started = dt.datetime.fromisoformat(session["started_at"])
        local = timeutil.to_user_local(started, user)
        dates.append(local.date())
        rows = session["sets"]
        session_tonnage = sum((r["load"] or 0) * r["reps"] for r in rows)
        tonnage += session_tonnage
        max_weight = max([max_weight] + [r["load"] or 0 for r in rows])
        exercises.update(r["exercise_id"] for r in rows)
        groups.update(r["grp"] for r in rows if r["grp"] is not None)
        max_sets = max(max_sets, len(rows))
        max_exercises = max(max_exercises, len({r["exercise_id"] for r in rows}))
        max_session_tonnage = max(max_session_tonnage, session_tonnage)
        max_bw_reps = max([max_bw_reps] + [r["reps"] for r in rows if r["weight"] == 0])
        blocks: dict[int, set[int]] = {}
        for r in rows:
            blocks.setdefault(r["block_id"], set()).add(r["exercise_id"])
        has_superset = has_superset or any(len(v) > 1 for v in blocks.values())
        if local.hour < 7:
            early += 1
        finished = session["finished_at"]
        duration = None
        if finished and finished != session["started_at"]:
            duration = (dt.datetime.fromisoformat(finished) - started).total_seconds()
        stamp = session["started_at"]
        ctx = achievements.AchievementContext(
            total_workouts=len(dates),
            lifetime_tonnage_kg=formatting.to_kg(tonnage, unit),
            best_week_streak=analytics.max_week_streak(dates),
            max_weight_kg=formatting.to_kg(max_weight, unit),
            distinct_exercises=len(exercises),
            distinct_groups=len(groups),
            max_session_sets=max_sets,
            max_session_tonnage_kg=formatting.to_kg(max_session_tonnage, unit),
            max_session_exercises=max_exercises,
            has_superset=has_superset,
            max_bodyweight_reps=max_bw_reps,
            early_workouts=early,
            has_weekend_pair=achievements.weekend_pair_exists(dates),
            all_weekdays_covered=len({d.weekday() for d in dates}) == 7,
            has_dec31=any((d.month, d.day) == (12, 31) for d in dates),
            bodyweight_logs=sum(1 for t in bw_logs if t <= stamp),
            food_diary_best_run=achievements.longest_daily_run(
                [d for d in food_days if d <= local.date()]
            ),
            workout_start_hour=local.hour,
            workout_date=local.date(),
            workout_duration_seconds=duration,
        )
        for code in achievements.earned_codes(ctx) & wanted:
            found.setdefault(code, stamp)
        if len(found) == len(wanted):
            break
    now = db.now_iso()
    return {code: found.get(code, now) for code in wanted}
