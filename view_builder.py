"""Turns raw workout/block/set rows from db.py into formatting.py view objects."""

import datetime as dt

import analytics
import db
from formatting import BlockView, ExerciseBlockView


async def build_block_views(
    workout_id: int,
    formula: str = "epley",
    previous_before: str | None = None,
    mark_golds: bool = False,
) -> list[BlockView]:
    """previous_before: if set (a workout's started_at), each block also gets the
    set breakdown from that exercise's last session strictly before that date.

    mark_golds: flag the set that beats the exercise's all-time best e1RM (the
    live 🥇). Costs one aggregate query per exercise, so it is opt-in — the
    live tracker and the finish card want it, history and admin views don't.

    An exercise logged as more than one block in the same workout (e.g. 2 sets
    up front and 2 more at the end) is deliberately allowed at entry time — but
    everything downstream (the summary card, its e1RM/tonnage, PR detection)
    should see it as one exercise, not two "подходов нет"-adjacent duplicates.
    Blocks are merged here, in encounter order, keyed by exercise_id.
    """
    blocks = await db.list_blocks_for_workout(workout_id)
    order: list[int] = []
    merged: dict[int, dict] = {}
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

        ex_id = block_exs[0]["exercise_id"]
        if ex_id not in merged:
            order.append(ex_id)
            ex = await db.get_exercise(ex_id)
            merged[ex_id] = {
                "exercise": ex,
                "sets": [],
                "loads": [],
                "rpes": [],
            }
        entry = merged[ex_id]
        # Два разных числа на подход, и оба нужны: `sets` — то, что записал
        # человек, и то, что показывается ("0×12" подтягиваний), а `loads` —
        # фактическая нагрузка (db.load_of), по которой считают e1RM рекорды и
        # графики. Показывать нагрузку вместо записанного веса нельзя, считать
        # по записанному весу — тоже: карточка расходилась с залом славы.
        entry["sets"].extend((s["weight"], s["reps"]) for s in sets)
        entry["loads"].extend(db.load_of(s) for s in sets)
        entry["rpes"].extend(s["rpe"] for s in sets)

    workout = await db.get_workout(workout_id) if mark_golds else None

    views: list[BlockView] = []
    for ex_id in order:
        entry = merged[ex_id]
        ex = entry["exercise"]
        gname = await group_info(ex["primary_group_id"])
        gold_index = None
        if workout is not None:
            gold_index = _best_gold_index(
                [
                    (load, reps)
                    for load, (_w, reps) in zip(entry["loads"], entry["sets"], strict=True)
                ],
                await db.max_e1rm_before_workout(
                    workout["user_id"], ex_id, workout_id, formula
                ),
                formula,
            )
        prev_sets = None
        prev_set_rpes = None
        prev_started_at = None
        if previous_before is not None:
            prev = await _previous_session_sets(ex_id, workout_id, previous_before)
            if prev is not None:
                prev_sets, prev_set_rpes, prev_started_at = prev
        views.append(
            ExerciseBlockView(
                group_name=gname,
                exercise_name=ex["display_name"],
                sets=entry["sets"],
                formula=formula,
                exercise_id=ex_id,
                prev_sets=prev_sets,
                set_rpes=entry["rpes"] if any(r is not None for r in entry["rpes"]) else None,
                prev_set_rpes=prev_set_rpes,
                prev_started_at=prev_started_at,
                note=await db.get_workout_exercise_note(workout_id, ex_id),
                gold_index=gold_index,
                set_loads=entry["loads"],
            )
        )

    return views


def _best_gold_index(
    loaded_sets: list[tuple[float, int]], previous_best: float, formula: str
) -> int | None:
    """Index of the session's best set, if it clears the exercise's all-time
    best e1RM. Only the best one is marked: two 🥇 in one exercise would read
    as a bug, and the later set is the one that stands as the record anyway.

    Пары (нагрузка, повторы), а не (записанный вес, повторы): планка приходит из
    db.max_e1rm_before_workout, а та считает по load_weight. По сырому весу
    подтягивания с поясом не брали 🥇 никогда — их «10 кг» не могли перебить
    рекорд в 105 кг, который сами же и поставили.
    """
    best_index = None
    best_score = previous_best
    for i, (load, reps) in enumerate(loaded_sets):
        if reps <= 0:
            continue
        score = analytics.e1rm(load, reps, formula)
        if score > best_score:
            best_score, best_index = score, i
    return best_index


async def workout_pick_exercises(workout_id: int) -> list[tuple[str, str]]:
    """(имя, группа мышц) для каждого упражнения тренировки, в порядке блоков и
    без повторов — из чего собираются списки выбора тренировки (повторить план,
    создать программу).

    Суперсеты разворачиваются целиком: в план уезжают оба упражнения блока, и
    показывать только первое значило бы обещать не то, что человек получит.
    Группа — пустая строка, если у упражнения её нет: подписывать «[БЕЗ ГРУППЫ]»
    в списке из восьми строк дороже, чем промолчать.
    """
    seen: set[int] = set()
    rows: list[tuple[str, str]] = []
    for block in await db.list_blocks_for_workout(workout_id):
        for be in await db.get_block_exercises(block["id"]):
            if be["exercise_id"] in seen:
                continue
            seen.add(be["exercise_id"])
            ex = await db.get_exercise(be["exercise_id"])
            if ex is None:
                continue
            group = (
                await db.get_muscle_group(ex["primary_group_id"])
                if ex["primary_group_id"] else None
            )
            rows.append((ex["display_name"], group["name"] if group else ""))
    return rows


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
) -> tuple[list[tuple[float, int]], list[float | None], dt.datetime] | None:
    """The prior session's sets (weights/reps), their RPEs, and that session's
    date — or None if there's no prior session."""
    rows = await db.list_sets_for_exercise(exercise_id, exclude_workout_id=workout_id)
    set_rows = [
        analytics.SetRow(db.load_of(r), r["reps"], r["workout_id"], r["started_at"], r["rpe"])
        for r in rows
        if r["started_at"] < before
    ]
    if not set_rows:
        return None
    sessions = analytics.group_sets_by_session(set_rows)
    last = sessions[-1]
    return (
        [(s.weight, s.reps) for s in last.sets],
        [s.rpe for s in last.sets],
        dt.datetime.fromisoformat(last.started_at),
    )
