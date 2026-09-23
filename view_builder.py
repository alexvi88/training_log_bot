"""Turns raw workout/block/set rows from db.py into formatting.py view objects."""

import datetime as dt
from dataclasses import dataclass
from typing import Any

import analytics
import db
import i18n
from formatting import BlockView, ExerciseBlockView


@dataclass
class WorkoutRows:
    """Всё, из чего собирается тренировка, — выбрано пачкой по разу на таблицу
    (load_workout_rows), а не по запросу на блок/упражнение. Раньше карточка
    и JSON тренировки ходили в базу за каждым блоком, подходами, упражнением,
    группой и заметкой по отдельности, а GET /workouts/{id} — дважды за ответ:
    78 запросов на тренировку из 6 упражнений.

    Строки те же, что отдавали одиночные db-функции (list_blocks_for_workout,
    get_block_exercises, list_sets_for_block, get_exercise, get_muscle_group,
    get_workout_exercise_note), и в том же порядке внутри блока.
    """

    blocks: list[Any]
    exercises_by_block: dict[int, list[Any]]
    sets_by_block: dict[int, list[Any]]
    exercises: dict[int, Any]
    groups: dict[int, Any]
    notes: dict[int, str]


async def load_workout_rows(workout_id: int) -> WorkoutRows:
    blocks = await db.list_blocks_for_workout(workout_id)
    exercises_by_block: dict[int, list[Any]] = {}
    for be in await db.list_block_exercises_for_workout(workout_id):
        exercises_by_block.setdefault(be["block_id"], []).append(be)
    sets_by_block: dict[int, list[Any]] = {}
    for s in await db.list_sets_for_workout(workout_id):
        sets_by_block.setdefault(s["block_id"], []).append(s)
    exercises = await db.get_exercises_by_ids(
        be["exercise_id"] for bes in exercises_by_block.values() for be in bes
    )
    groups = await db.get_muscle_groups_by_ids(ex["primary_group_id"] for ex in exercises.values())
    notes = await db.get_workout_exercise_notes(workout_id)
    return WorkoutRows(blocks, exercises_by_block, sets_by_block, exercises, groups, notes)


async def build_block_views(
    workout_id: int,
    formula: str = "epley",
    previous_before: str | None = None,
    mark_golds: bool = False,
    mark_records: bool = False,
    *,
    rows: WorkoutRows | None = None,
    workout: Any = None,
) -> list[BlockView]:
    """previous_before: if set (a workout's started_at), each block also gets the
    set breakdown from that exercise's last session strictly before that date.

    mark_golds: flag the set that beats the exercise's all-time best e1RM (the
    live 🥇). Costs one aggregate query (all exercises at once), so it is opt-in — the
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

    rows/workout: уже выбранные строки этой тренировки (load_workout_rows) и
    её строка workouts — GET /workouts/{id} выбирает их один раз на весь
    ответ и отдаёт сюда же, а не второй раз.
    """
    if rows is None:
        rows = await load_workout_rows(workout_id)
    order: list[int] = []
    merged: dict[int, dict] = {}

    def group_info(group_id: int | None) -> str:
        if group_id is None:
            return i18n.t("view.no_group")
        g = rows.groups.get(group_id)
        return g["name"] if g else "?"

    for block in rows.blocks:
        block_exs = rows.exercises_by_block.get(block["id"], [])
        sets = rows.sets_by_block.get(block["id"], [])
        if not block_exs:
            continue

        ex_id = block_exs[0]["exercise_id"]
        if ex_id not in merged:
            order.append(ex_id)
            ex = rows.exercises.get(ex_id)
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

    if not (mark_golds or mark_records):
        workout = None
    elif workout is None:
        workout = await db.get_workout(workout_id)

    # 🥇-планка и история упражнений — одним запросом на все упражнения
    # тренировки, а не по запросу на каждое. Планка — при любой выбранной
    # строке тренировки (и при mark_records тоже), как считалось и раньше.
    best_before: dict[int, float] = {}
    if workout is not None and order:
        best_before = await db.max_e1rm_before_workout_by_exercise(
            workout["user_id"], order, workout_id, formula
        )
    before = previous_before or (workout["started_at"] if mark_records else None)
    prior_by_exercise: dict[int, list[analytics.SessionStats]] = {}
    if before is not None and order:
        prior_by_exercise = await _prior_sessions_by_exercise(
            order, workout_id, before, formula, last_session_only=not mark_records
        )

    views: list[BlockView] = []
    for ex_id in order:
        entry = merged[ex_id]
        ex = entry["exercise"]
        gname = group_info(ex["primary_group_id"])
        gold_index = None
        if workout is not None:
            gold_index = best_gold_index(
                [
                    (load, reps, rpe)
                    for load, (_w, reps), rpe in zip(
                        entry["loads"], entry["sets"], entry["rpes"], strict=True
                    )
                ],
                best_before.get(ex_id, 0),
                formula,
            )
        prev_sets = None
        prev_set_rpes = None
        prev_started_at = None
        record_e1rm_delta = None
        record_reps = None
        if before is not None:
            prior = prior_by_exercise.get(ex_id, [])
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
                            analytics.SetRow(
                                load, reps, workout_id, workout["started_at"], rpe
                            )
                            for load, (_w, reps), rpe in zip(
                                entry["loads"], entry["sets"], entry["rpes"], strict=True
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
                note=rows.notes.get(ex_id),
                gold_index=gold_index,
                set_loads=entry["loads"],
                record_e1rm_delta=record_e1rm_delta,
                record_reps=record_reps,
            )
        )

    return views


def best_gold_index(
    loaded_sets: list[tuple[float, int, float | None]], previous_best: float, formula: str
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
    for i, (load, reps, rpe) in enumerate(loaded_sets):
        if reps <= 0:
            continue
        score = analytics.e1rm(load, reps, formula, rpe)
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
    data = await load_workout_rows(workout_id)
    rows: list[tuple[str, str]] = []
    for block in data.blocks:
        for be in data.exercises_by_block.get(block["id"], []):
            if be["exercise_id"] in seen:
                continue
            seen.add(be["exercise_id"])
            ex = data.exercises.get(be["exercise_id"])
            if ex is None:
                continue
            group = data.groups.get(ex["primary_group_id"]) if ex["primary_group_id"] else None
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
    return _duration_from_span(span)


def _duration_from_span(span: tuple[str, str] | None) -> float | None:
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

    Длительности всех тренировок — одним GROUP BY
    (db.list_finished_workout_set_spans), тем же правилом, что
    workout_duration_seconds: раньше по запросу на тренировку, 150+ на
    экран зала славы.
    """
    longest = 0.0
    for row in await db.list_finished_workout_set_spans(user_id):
        if row["started_at"] == row["finished_at"]:
            continue
        span = None if row["first_at"] is None else (row["first_at"], row["last_at"])
        seconds = _duration_from_span(span)
        if seconds is not None and seconds > longest:
            longest = seconds
    return longest


async def _prior_sessions_by_exercise(
    exercise_ids: list[int],
    workout_id: int,
    before: str,
    formula: str,
    last_session_only: bool = False,
) -> dict[int, list[analytics.SessionStats]]:
    """Every earlier session of each exercise, oldest first — the history both
    «прошлая» и строка рекорда читают из одной выборки, чтобы не ходить в базу
    за одними и теми же подходами дважды. Одним запросом на все упражнения
    тренировки и уже обрезанная по `before` в SQL: раньше — запрос на
    упражнение и вся его история, отфильтрованная в Python.

    last_session_only — когда нужна только «прошлая» (без строки рекорда):
    тогда из базы приезжает лишь последняя сессия, prior[-1] тот же.

    Нагрузкой (db.load_of), а не записанным весом: рекорд и дельта сравниваются
    с тем же числом, по которому считается e1RM подхода.
    """
    rows_by_exercise: dict[int, list[analytics.SetRow]] = {}
    for r in await db.list_prior_sets_for_exercises(
        exercise_ids, workout_id, before, last_session_only=last_session_only
    ):
        rows_by_exercise.setdefault(r["exercise_id"], []).append(
            analytics.SetRow(db.load_of(r), r["reps"], r["workout_id"], r["started_at"], r["rpe"])
        )
    result: dict[int, list[analytics.SessionStats]] = {}
    for ex_id, set_rows in rows_by_exercise.items():
        sessions = analytics.group_sets_by_session(set_rows)
        for session in sessions:
            session.formula = formula
        result[ex_id] = sessions
    return result


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


async def sets_beat_record(
    ex_id: int, workout_id: int, logged: list[tuple[float, int, float | None]], formula: str
) -> bool:
    """True if any of the sets just logged is a genuine all-time record for this
    exercise — a new best e1RM or a new heaviest weight (or, for bodyweight moves,
    the most reps in a set). Compared against every prior finished session, so
    the current workout's own earlier sets are excluded.

    Live, per-set signal — different from `_session_record` above, which
    compares the whole SESSION's best set against the best prior session and
    only makes sense once the session (or exercise block) is done. This one
    answers "does the bot's 🔥 reaction fire right now", the same question the
    HTTP API needs answered for the app's live feed (see api_v1.log_set).
    """
    workout = await db.get_workout(workout_id)
    if workout is None:
        return False
    started = workout["started_at"]
    history_rows = await db.list_sets_for_exercise(ex_id, exclude_workout_id=workout_id)
    history_set_rows = [
        analytics.SetRow(db.load_of(r), r["reps"], r["workout_id"], r["started_at"], r["rpe"])
        for r in history_rows
        if r["started_at"] < started
    ]
    prior_sessions = analytics.group_sets_by_session(history_set_rows)
    for s in prior_sessions:
        s.formula = formula
    if not prior_sessions:
        return False  # first-ever session with this exercise — nothing to beat yet
    prior = analytics.compute_personal_records(prior_sessions)
    is_bodyweight = all(w == 0 for w, _r, _rpe in logged)
    if is_bodyweight:
        prior_best_reps = max(prior.max_reps_at_weight.values(), default=0)
        return any(r > prior_best_reps for _w, r, _rpe in logged)
    for weight, reps, rpe in logged:
        if weight > prior.max_weight:
            return True
        if analytics.e1rm(weight, reps, formula, rpe) > prior.max_e1rm:
            return True
    return False
