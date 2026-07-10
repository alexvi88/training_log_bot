"""Turns raw workout/block/set rows from db.py into formatting.py view objects."""

import datetime as dt

import analytics
import db
from formatting import BlockView, ExerciseBlockView


async def build_block_views(
    workout_id: int,
    formula: str = "epley",
    skip_empty: bool = True,
    previous_before: str | None = None,
) -> list[BlockView]:
    """previous_before: if set (a workout's started_at), each block also gets the
    set breakdown from that exercise's last session strictly before that date."""
    blocks = await db.list_blocks_for_workout(workout_id)
    views: list[BlockView] = []
    group_cache: dict[int | None, str] = {}

    async def group_info(group_id: int | None) -> str:
        if group_id is None:
            return "без группы"
        if group_id not in group_cache:
            g = await db.get_muscle_group(group_id)
            group_cache[group_id] = g["name"] if g else "?"
        return group_cache[group_id]

    for block in blocks:
        block_exs = await db.get_block_exercises(block["id"])
        sets = await db.list_sets_for_block(block["id"])
        if not block_exs:
            continue
        if skip_empty and not sets:
            continue

        ex_id = block_exs[0]["exercise_id"]
        ex = await db.get_exercise(ex_id)
        gname = await group_info(ex["primary_group_id"])
        sets_tuples = [(s["weight"], s["reps"]) for s in sets]
        prev_sets = None
        if previous_before is not None:
            prev_sets = await _previous_session_sets(ex_id, workout_id, previous_before)
        views.append(
            ExerciseBlockView(
                group_name=gname,
                exercise_name=ex["display_name"],
                sets=sets_tuples,
                formula=formula,
                exercise_id=ex_id,
                prev_sets=prev_sets,
            )
        )

    return views


MAX_PLAUSIBLE_DURATION_SECONDS = 6 * 3600


async def workout_duration_seconds(workout) -> float | None:
    """Time from the first logged set to the last, for workouts tracked live.

    Backfilled/imported workouts have started_at == finished_at (no live FSM ran),
    so the set timestamps only reflect data-entry time, not the actual session —
    duration is skipped for those. Editing a finished workout can also add a set
    with a fresh timestamp long after the session; an implausibly long span is
    treated the same way rather than shown as-is.
    """
    if workout["started_at"] == workout["finished_at"]:
        return None
    span = await db.get_workout_set_span(workout["id"])
    if span is None:
        return None
    first_at, last_at = span
    seconds = (dt.datetime.fromisoformat(last_at) - dt.datetime.fromisoformat(first_at)).total_seconds()
    if seconds > MAX_PLAUSIBLE_DURATION_SECONDS:
        return None
    return seconds


async def _previous_session_sets(
    exercise_id: int, workout_id: int, before: str
) -> list[tuple[float, int]] | None:
    rows = await db.list_sets_for_exercise(exercise_id, exclude_workout_id=workout_id)
    set_rows = [
        analytics.SetRow(r["weight"], r["reps"], r["workout_id"], r["started_at"])
        for r in rows
        if r["started_at"] < before
    ]
    if not set_rows:
        return None
    sessions = analytics.group_sets_by_session(set_rows)
    last = sessions[-1]
    return [(s.weight, s.reps) for s in last.sets]
