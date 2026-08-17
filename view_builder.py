"""Turns raw workout/block/set rows from db.py into formatting.py view objects."""

import datetime as dt

import analytics
import db
import i18n
from formatting import BlockView, ExerciseBlockView


async def build_block_views(
    workout_id: int,
    formula: str = "epley",
    previous_before: str | None = None,
    mark_golds: bool = False,
    mark_records: bool = False,
) -> list[BlockView]:
    """previous_before: if set (a workout's started_at), each block also gets the
    set breakdown from that exercise's last session strictly before that date.

    mark_golds: flag the set that beats the exercise's all-time best e1RM (the
    live 🥇). Costs one aggregate query per exercise, so it is opt-in — the
    live tracker and the finish card want it, history and admin views don't.

    mark_records: заполнить рекорд упражнения (e1RM или повторы своим весом),
    поставленный именно в этой тренировке, — строка 🔥 внутри блока упражнения
    на карточке завершённой тренировки. Требует истории упражнения, поэтому
    считается из той же выборки, что и «прошлая», и только вместе с ней.

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
            return i18n.t("view.no_group")
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

    workout = await db.get_workout(workout_id) if (mark_golds or mark_records) else None

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
        record_e1rm_delta = None
        record_reps = None
        before = previous_before or (workout["started_at"] if mark_records else None)
        if before is not None:
            prior = await _prior_sessions(ex_id, workout_id, before, formula)
            if prior and previous_before is not None:
                last = prior[-1]
                prev_sets = [(s.weight, s.reps) for s in last.sets]
                prev_set_rpes = [s.rpe for s in last.sets]
                prev_started_at = dt.datetime.fromisoformat(last.started_at)
            if mark_records:
                record_e1rm_delta, record_reps = _session_record(
                    prior,
                    analytics.SessionStats(
                        workout_id=workout_id,
                        started_at=workout["started_at"],
                        sets=[
                            analytics.SetRow(load, reps, workout_id, workout["started_at"])
                            for load, (_w, reps) in zip(
                                entry["loads"], entry["sets"], strict=True
                            )
                        ],
                        formula=formula,
                    ),
                )
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
                record_e1rm_delta=record_e1rm_delta,
                record_reps=record_reps,
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
    with a fresh timestamp long after the session — that moment is identifiable
    (it can only be later than finished_at, since a live set is always logged
    before the workout is closed), so those sets are excluded from the span
    instead of just capping the total: находка 25 — a couple hours' delay
    between finishing and editing read as a real 2h+ session and neither the
    old 6h cap nor "как есть" caught it, and the number fed straight into
    "Самая длинная тренировка" and the «Марафонец» achievement.
    """
    if workout["started_at"] == workout["finished_at"]:
        return None
    span = await db.get_workout_set_span(workout["id"], before=workout["finished_at"])
    if span is None:
        return None
    first_at, last_at = span
    seconds = (dt.datetime.fromisoformat(last_at) - dt.datetime.fromisoformat(first_at)).total_seconds()
    if seconds > MAX_PLAUSIBLE_DURATION_SECONDS:
        return None
    return seconds


async def longest_workout_seconds(user_id: int) -> float:
    """Самая долгая тренировка для экрана Достижений — тем же правилом, что
    и «Марафонец» (achievements.py): без этого разные числа для одного и
    того же понятия расходились на одном экране (см. hall_of_fame_aggregates).
    """
    longest = 0.0
    for workout in await db.list_finished_workouts_meta(user_id):
        seconds = await workout_duration_seconds(workout)
        if seconds is not None and seconds > longest:
            longest = seconds
    return longest


async def _prior_sessions(
    exercise_id: int, workout_id: int, before: str, formula: str
) -> list[analytics.SessionStats]:
    """Every earlier session of this exercise, oldest first — the history both
    «прошлая» и строка рекорда читают из одной выборки, чтобы не ходить в базу
    за одними и теми же подходами дважды.

    Нагрузкой (db.load_of), а не записанным весом: рекорд и дельта сравниваются
    с тем же числом, по которому считается e1RM подхода.
    """
    rows = await db.list_sets_for_exercise(exercise_id, exclude_workout_id=workout_id)
    set_rows = [
        analytics.SetRow(db.load_of(r), r["reps"], r["workout_id"], r["started_at"], r["rpe"])
        for r in rows
        if r["started_at"] < before
    ]
    sessions = analytics.group_sets_by_session(set_rows)
    for session in sessions:
        session.formula = formula
    return sessions


def _session_record(
    prior: list[analytics.SessionStats], new_session: analytics.SessionStats
) -> tuple[float | None, int | None]:
    """(насколько e1RM выше прошлого лучшего, рекорд повторов) для этой сессии.

    Первая в истории сессия упражнения рекорда не даёт: бить нечего, а «рекорд»
    на каждом новом упражнении — это слово, которое перестаёт что-то значить.
    Своим весом e1RM тождественно нулю, там единственный осмысленный рекорд —
    повторы в подходе.
    """
    if not prior or not new_session.sets:
        return None, None
    prior_pr = analytics.compute_personal_records(prior)
    if new_session.is_bodyweight_mode:
        best = new_session.max_reps_in_set
        prev_best = max(prior_pr.max_reps_at_weight.values(), default=0)
        return None, (best if best > prev_best else None)
    delta = new_session.top_e1rm - prior_pr.max_e1rm
    return (delta if delta > 0 else None), None
