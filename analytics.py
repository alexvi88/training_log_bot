"""Pure-Python metrics: e1RM, tonnage, trend regression, PR detection.

Operates on plain dicts/rows of working sets, so it stays decoupled from the
DB layer and is trivially testable.
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


def epley_e1rm(weight: float, reps: int) -> float:
    if reps <= 1:
        return weight
    return weight * (1 + reps / 30)


# Brzycki is only meaningful in the low-rep range it was fitted on. Its
# denominator (37 - reps) collapses as reps climb: at 30 reps it already returns
# 5x the weight, at 36 reps it returns 36x, and the old `reps >= 37` guard sat
# just past the peak — so a burnout set like 20kg x 36 produced a 720kg e1RM that
# became the exercise's permanent record. Epley stays monotone and gentle at any
# rep count, and the two formulas agree *exactly* at 10 reps
# (1 + 10/30 == 36/27 == 4/3), so handing over there is seamless rather than a step.
BRZYCKI_MAX_REPS = 10


def brzycki_e1rm(weight: float, reps: int) -> float:
    if reps <= 1:
        return weight
    if reps > BRZYCKI_MAX_REPS:
        return epley_e1rm(weight, reps)
    return weight * 36 / (37 - reps)


def e1rm(weight: float, reps: int, formula: str = "epley") -> float:
    if formula == "brzycki":
        return brzycki_e1rm(weight, reps)
    return epley_e1rm(weight, reps)


@dataclass
class SetRow:
    weight: float
    reps: int
    workout_id: Optional[int] = None
    started_at: Optional[str] = None
    rpe: Optional[float] = None  # display-only; never enters e1RM/PR/trend math


@dataclass
class SessionStats:
    workout_id: int
    started_at: str
    sets: list[SetRow]
    formula: str = "epley"

    @property
    def tonnage(self) -> float:
        return sum(s.weight * s.reps for s in self.sets)

    @property
    def total_reps(self) -> int:
        return sum(s.reps for s in self.sets)

    @property
    def is_bodyweight_mode(self) -> bool:
        return bool(self.sets) and all(s.weight == 0 for s in self.sets)

    @property
    def top_set(self) -> Optional[SetRow]:
        if not self.sets:
            return None
        if self.is_bodyweight_mode:
            return max(self.sets, key=lambda s: s.reps)
        return max(self.sets, key=lambda s: e1rm(s.weight, s.reps, self.formula))

    @property
    def top_e1rm(self) -> float:
        ts = self.top_set
        if ts is None:
            return 0.0
        return e1rm(ts.weight, ts.reps, self.formula)

    @property
    def max_reps_in_set(self) -> int:
        return max((s.reps for s in self.sets), default=0)


def group_sets_by_session(rows: Iterable[SetRow]) -> list[SessionStats]:
    by_workout: dict[int, list[SetRow]] = {}
    started_at_by_workout: dict[int, str] = {}
    for r in rows:
        by_workout.setdefault(r.workout_id, []).append(r)
        started_at_by_workout[r.workout_id] = r.started_at
    sessions = [
        SessionStats(workout_id=wid, started_at=started_at_by_workout[wid], sets=sets)
        for wid, sets in by_workout.items()
    ]
    sessions.sort(key=lambda s: s.started_at)
    return sessions


@dataclass
class Trend:
    slope_per_week: float
    direction: str  # "up" | "down" | "flat"
    intercept: float = 0.0  # y at x=0 (t0, the first point's calendar day)


def linear_trend(points: list[tuple[dt.datetime, float]]) -> Optional[Trend]:
    """Least-squares slope of y over time, expressed per week.

    x is bucketed to calendar days: several sessions logged minutes apart on
    the same day would otherwise sit at near-identical x, and any y
    difference between them blows up into an absurd per-week slope.
    """
    if len(points) < 2:
        return None
    t0 = points[0][0].date()
    xs = [(p[0].date() - t0).days / 7 for p in points]  # weeks
    ys = [p[1] for p in points]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return Trend(slope_per_week=0.0, direction="flat", intercept=mean_y)
    slope = num / den
    direction = "flat" if abs(slope) < 1e-6 else ("up" if slope > 0 else "down")
    intercept = mean_y - slope * mean_x
    return Trend(slope_per_week=slope, direction=direction, intercept=intercept)


@dataclass(frozen=True)
class Rank:
    """One rung of the rank ladder."""
    level: int
    emoji: str
    name: str
    min_workouts: int
    min_tonnage_kg: float
    min_per_week: float


# Линейная лестница: звание растёт и от накопленного (тренировки, тоннаж), и от
# того, ходишь ли ты сейчас. Обе оси обязательны — тоннаж без регулярности это
# прошлые заслуги, а регулярность без объёма это разминка.
RANKS: list[Rank] = [
    Rank(0, "🚪", "Новичок", 0, 0, 0.0),
    Rank(1, "🧱", "Салага", 5, 5_000, 0.5),
    Rank(2, "🔩", "Работяга", 20, 25_000, 1.0),
    Rank(3, "⚙️", "Станок", 50, 75_000, 1.5),
    Rank(4, "🪨", "Тяжеловес", 100, 200_000, 2.0),
    Rank(5, "🦾", "Ветеран подвала", 200, 500_000, 2.5),
    Rank(6, "👑", "Дед зала", 400, 1_000_000, 3.0),
]

# Окно, по которому считается «ходишь ли сейчас». Восемь недель — достаточно,
# чтобы отпуск не ронял звание, и мало, чтобы прошлогодняя форма не считалась
# текущей.
RANK_FREQUENCY_WEEKS = 8


def workouts_per_week(workout_dates: Iterable[dt.date], today: dt.date, weeks: int = RANK_FREQUENCY_WEEKS) -> float:
    """Средняя частота тренировок за последние `weeks` недель."""
    since = today - dt.timedelta(weeks=weeks)
    recent = sum(1 for d in workout_dates if since < d <= today)
    return recent / weeks


def _level_by(predicate) -> int:
    level = 0
    for rank in RANKS[1:]:
        if predicate(rank):
            level = rank.level
        else:
            break
    return level


def rank_for(total_workouts: int, tonnage_kg: float, per_week: float) -> Rank:
    """Текущее звание — по слабейшей из осей, но перерыв стоит одну ступень.

    Минимум из осей нужен, чтобы тоннаж, набранный когда-то, не держал звание
    человеку, который полгода не заходит, а частые пустые заходы не давали
    «Деда зала» за месяц. Но без нижней границы ветеран с пятью сотнями
    тренировок за месяц простоя падал бы в «Новички» — это читается как
    сломанный счётчик, а не как честная оценка. Поэтому пол: на одну ступень
    ниже заработанного результатами, и не глубже.
    """
    by_results = _level_by(
        lambda r: total_workouts >= r.min_workouts and tonnage_kg >= r.min_tonnage_kg
    )
    by_frequency = _level_by(lambda r: per_week >= r.min_per_week)
    return RANKS[max(min(by_results, by_frequency), by_results - 1, 0)]


def next_rank(current: Rank) -> Optional[Rank]:
    return RANKS[current.level + 1] if current.level + 1 < len(RANKS) else None


def rank_gap(current: Rank, total_workouts: int, tonnage_kg: float, per_week: float) -> Optional[str]:
    """Чего конкретно не хватает до следующего звания — самая отстающая ось.

    Одна причина, а не список: «не хватает 12 тренировок» — это цель, а три
    строки с недостачами по всем осям читаются как отказ.
    """
    nxt = next_rank(current)
    if nxt is None:
        return None
    gaps: list[tuple[float, str]] = []
    if total_workouts < nxt.min_workouts:
        missing = nxt.min_workouts - total_workouts
        gaps.append((missing / max(nxt.min_workouts, 1), f"ещё {missing} трен."))
    if tonnage_kg < nxt.min_tonnage_kg:
        missing_t = (nxt.min_tonnage_kg - tonnage_kg) / 1000
        gaps.append((missing_t * 1000 / max(nxt.min_tonnage_kg, 1), f"ещё {missing_t:.1f} т"))
    if per_week < nxt.min_per_week:
        gaps.append((
            (nxt.min_per_week - per_week) / max(nxt.min_per_week, 0.1),
            f"держать {nxt.min_per_week:g} трен./нед",
        ))
    if not gaps:
        return None
    return max(gaps)[1]


@dataclass
class GoldBook:
    """The three all-time-best sets of one exercise, each with its date.

    A speedrunner's "gold split": the best segment ever, independent of how
    good the run around it was. Three categories because they peak on
    different days — the heaviest single, the best-e1RM set and the longest
    set are usually three different memories.
    """
    best_e1rm: float = 0.0
    best_e1rm_weight: float = 0.0
    best_e1rm_reps: int = 0
    best_e1rm_date: str = ""
    max_weight: float = 0.0
    max_weight_reps: int = 0
    max_weight_date: str = ""
    max_reps: int = 0
    max_reps_weight: float = 0.0
    max_reps_date: str = ""


def gold_book(sessions: list[SessionStats], formula: str = "epley") -> Optional[GoldBook]:
    """All-time golds from the exercise's full session history, or None when
    there is nothing logged yet."""
    book = GoldBook()
    found = False
    for session in sessions:
        day = session.started_at[:10]
        for row in session.sets:
            if row.reps <= 0:
                continue
            found = True
            score = e1rm(row.weight, row.reps, formula)
            if score > book.best_e1rm:
                book.best_e1rm = score
                book.best_e1rm_weight, book.best_e1rm_reps = row.weight, row.reps
                book.best_e1rm_date = day
            if row.weight > book.max_weight or (
                row.weight == book.max_weight and row.reps > book.max_weight_reps
            ):
                book.max_weight, book.max_weight_reps = row.weight, row.reps
                book.max_weight_date = day
            if row.reps > book.max_reps:
                book.max_reps, book.max_reps_weight = row.reps, row.weight
                book.max_reps_date = day
    return book if found else None


@dataclass
class PersonalRecords:
    max_weight: float = 0.0
    max_e1rm: float = 0.0
    best_e1rm_weight: float = 0.0
    best_e1rm_reps: int = 0
    max_session_tonnage: float = 0.0
    max_reps_at_weight: dict[float, int] = field(default_factory=dict)


def compute_personal_records(sessions: list[SessionStats]) -> PersonalRecords:
    pr = PersonalRecords()
    for session in sessions:
        if session.tonnage > pr.max_session_tonnage:
            pr.max_session_tonnage = session.tonnage
        for s in session.sets:
            if s.weight > pr.max_weight:
                pr.max_weight = s.weight
            val = e1rm(s.weight, s.reps, session.formula)
            if val > pr.max_e1rm:
                pr.max_e1rm = val
                pr.best_e1rm_weight = s.weight
                pr.best_e1rm_reps = s.reps
            if s.reps > pr.max_reps_at_weight.get(s.weight, 0):
                pr.max_reps_at_weight[s.weight] = s.reps
    return pr


@dataclass
class NewRecord:
    kind: str  # "weight" | "e1rm" | "tonnage" | "reps_at_weight"
    value: float
    extra: Optional[float] = None  # weight, for reps_at_weight


def detect_new_records(
    history_sessions: list[SessionStats], new_session: SessionStats
) -> list[NewRecord]:
    """Compare a freshly finished session against all prior sessions for the same exercise."""
    prior_pr = compute_personal_records(history_sessions)
    records: list[NewRecord] = []

    for s in new_session.sets:
        val = e1rm(s.weight, s.reps, new_session.formula)
        if val > prior_pr.max_e1rm:
            records.append(NewRecord(kind="e1rm", value=val))
            prior_pr.max_e1rm = val

    reps_records: list[NewRecord] = []
    for s in new_session.sets:
        prev_best = prior_pr.max_reps_at_weight.get(s.weight, 0)
        if s.reps > prev_best:
            reps_records.append(NewRecord(kind="reps_at_weight", value=s.reps, extra=s.weight))
            prior_pr.max_reps_at_weight[s.weight] = s.reps

    # Drop records dominated by any set actually performed in this session (same
    # reps at a lower weight, or same weight at fewer reps) — only the best one is
    # worth a notification. This must check against all sets, not just the ones
    # that individually beat history: a weight already matched historically (so
    # not itself "new") can still dominate a lighter new-weight-bucket record set
    # in the same session.
    for r in reps_records:
        dominated = any(
            other.weight >= r.extra
            and other.reps >= r.value
            and (other.weight, other.reps) != (r.extra, r.value)
            for other in new_session.sets
        )
        if not dominated:
            records.append(r)

    return records


@dataclass
class ComparisonDelta:
    e1rm_delta: float
    tonnage_delta: float
    prev_started_at: str


def compare_to_previous_session(sessions: list[SessionStats]) -> Optional[ComparisonDelta]:
    """sessions must be sorted ascending, with the new session last."""
    if len(sessions) < 2:
        return None
    prev, curr = sessions[-2], sessions[-1]
    return ComparisonDelta(
        e1rm_delta=curr.top_e1rm - prev.top_e1rm,
        tonnage_delta=curr.tonnage - prev.tonnage,
        prev_started_at=prev.started_at,
    )


# Default hypertrophy working range the progression assistant nudges toward
# (matches the AI trainer's methodology: 5-10 reps, double progression).
REP_RANGE_MIN = 5
REP_RANGE_MAX = 10

# Weekly working-set landmarks per muscle group (5-12 sets/week).
WEEKLY_VOLUME_MIN = 5
WEEKLY_VOLUME_MAX = 12


# Finished-workout counts worth celebrating right on the completion card
# (not a push — the user is looking at the screen the moment it happens).
_SMALL_MILESTONES = frozenset({1, 10, 25, 50, 75})


def is_workout_milestone(total_finished: int) -> bool:
    """True on the 1st/10th/25th/50th/75th workout, then every 100th (100, 200, …)."""
    if total_finished <= 0:
        return False
    return total_finished in _SMALL_MILESTONES or total_finished % 100 == 0


def classify_weekly_volume(sets_count: int) -> str:
    """Bucket a group's weekly set count vs the target range: none/low/in_range/high."""
    if sets_count <= 0:
        return "none"
    if sets_count < WEEKLY_VOLUME_MIN:
        return "low"
    if sets_count > WEEKLY_VOLUME_MAX:
        return "high"
    return "in_range"


# How long a muscle group needs before it's worth loading hard again. Scaled by
# how much work it got: a light session clears in about three days, a 12+ set
# one takes four. Deliberately a rule of thumb, not a model — it answers "что
# сегодня логичнее" and nothing more.
RECOVERY_HOURS_MIN = 72
RECOVERY_HOURS_MAX = 96
RECOVERY_SETS_FOR_MAX = 12


def recovery_percent(last_trained: dt.date, sets_done: int, today: dt.date) -> int:
    """0-100: how recovered a muscle group is, given when it was last trained
    and how many sets it took.

    Linear from 0% at the end of that session to 100% after the window. Never
    negative, never above 100 — this is shown as a readiness figure, and a
    number outside that range would read as a bug rather than as nuance.
    """
    days_since = (today - last_trained).days
    if days_since < 0:
        return 0
    load = min(max(sets_done, 0), RECOVERY_SETS_FOR_MAX) / RECOVERY_SETS_FOR_MAX
    window_hours = RECOVERY_HOURS_MIN + (RECOVERY_HOURS_MAX - RECOVERY_HOURS_MIN) * load
    return max(0, min(100, round(days_since * 24 / window_hours * 100)))


@dataclass
class ProgressionSuggestion:
    action: str  # "add_weight" | "add_reps"
    target_weight: float
    target_reps: int  # add_reps: reps to beat; add_weight: bottom-of-range reps to restart at
    is_bodyweight: bool = False
    # The set the target was derived from — the top working set of last session.
    # Carried so the hint can say *why* this number and not just assert it; the
    # commonest complaint about Fitbod is exactly that its numbers look random.
    from_weight: float = 0.0
    from_reps: int = 0
    # Цель пришла из правила, которое прописано в программе, а не выведена из
    # истории — подсказка это проговаривает, чтобы число не выглядело взятым
    # с потолка (см. formatting.format_progression_hint).
    from_rule: bool = False


# Fallback increment per unit, used when the exercise's own history says nothing
# about how it's loaded — the bot has no idea whether it's a barbell, a dumbbell
# rack or a stack.
DEFAULT_WEIGHT_STEP = {"kg": 2.5, "lb": 5.0}

# Past this load the default step stops reading as progress (+2.5kg on a 200kg
# pull is ~1%, well inside week-to-week noise) and the equipment jumps in bigger
# increments anyway, so the target moves by a bigger plate.
HEAVY_WEIGHT_THRESHOLD = {"kg": 200.0, "lb": 450.0}
HEAVY_WEIGHT_STEP = {"kg": 5.0, "lb": 10.0}


def infer_weight_step(history_weights: Iterable[float]) -> Optional[float]:
    """The gap between the two heaviest distinct weights this exercise was ever
    loaded with — the athlete's own evidence of what increments the equipment
    actually offers (2kg dumbbells, 1.25kg micro plates, a 5kg stack pin).

    Returns None when there's nothing to compare (fewer than two distinct
    weights). The caller decides whether the gap is believable: a big gap
    usually just means a backoff or warm-up set, not the equipment's step.
    """
    heaviest = sorted({round(w, 3) for w in history_weights if w > 0}, reverse=True)
    if len(heaviest) < 2:
        return None
    return round(heaviest[0] - heaviest[1], 2)


def weight_step_for(
    top_weight: float, unit: str = "kg", inferred_step: Optional[float] = None
) -> float:
    """How much weight to add when a lift outgrows the rep range.

    A step inferred from the exercise's own history wins whenever it's *finer*
    than the default — it means this exercise is loaded in smaller increments
    than a barbell, and suggesting a weight that doesn't exist on the rack is
    the failure mode worth avoiding. A coarser inferred gap is ignored: it's far
    more likely a backoff set than a real increment.
    """
    base = DEFAULT_WEIGHT_STEP.get(unit, 2.5)
    if inferred_step is not None and 0 < inferred_step < base:
        return inferred_step
    if top_weight >= HEAVY_WEIGHT_THRESHOLD.get(unit, 200.0):
        return HEAVY_WEIGHT_STEP.get(unit, 5.0)
    return base


# e1RM is an estimate, not a measurement, and the formulas disagree with each
# other by more than this. A target within 1% of the last session's estimate is
# the same effort, so it counts as holding — without the slack, the rep count
# would flip between 9 and 10 on rounding noise alone.
E1RM_HOLD_TOLERANCE = 0.99


def _reps_holding_e1rm(
    target_weight: float, last_weight: float, last_reps: int, formula: str
) -> int:
    """Fewest reps at target_weight whose e1RM still matches the last top set.

    Restarting at the bottom of the rep range after a weight bump can be a
    *step down* in strength: 127x10 is an e1RM of ~169, while 129.5x5 is only
    ~151. Offering that as "the goal" asks the athlete to do visibly less than
    last time. So the restart point is the lowest rep count in the range that
    holds the e1RM already reached, clamped to the range: REP_RANGE_MIN when
    the weight jump alone covers it, REP_RANGE_MAX when even a full range
    can't (the last session ran well past the range — the weight bump is still
    the right call, just not a reason to hand back reps).
    """
    reference = e1rm(last_weight, last_reps, formula) * E1RM_HOLD_TOLERANCE
    for reps in range(REP_RANGE_MIN, REP_RANGE_MAX):
        if e1rm(target_weight, reps, formula) >= reference:
            return reps
    return REP_RANGE_MAX


def suggest_progression(
    last_sets: list[tuple[float, int]],
    *,
    unit: str = "kg",
    inferred_step: Optional[float] = None,
    formula: str = "epley",
    rule: Optional[dict] = None,
) -> Optional[ProgressionSuggestion]:
    """Next-session target from last session's sets, by double progression.

    While the top working set is still inside the rep range, add a rep at the
    same weight; once it crossed the top of the range (>= REP_RANGE_MAX), bump
    the weight by one step (see weight_step_for) and restart at the lowest rep
    count that doesn't give back the e1RM already earned (_reps_holding_e1rm).
    Bodyweight sets (weight 0) simply chase one more rep.

    `rule` — the progression the program itself prescribes for this exercise
    (routine_exercises.progression, written by the AI trainer — see
    db.set_routine_exercise_progression). Without it this function guesses the
    rep range from a global default and the step from history, which is the
    right behaviour for a lift the user just does; but when the program says
    «доходишь до 8 повторов — прибавляй 2.5», the hint has no business
    proposing anything else. Unknown or malformed rules fall through to the
    default, so a rule the model invented can never break the hint.
    """
    working = [(w, r) for w, r in last_sets if r > 0]
    if not working:
        return None
    if all(w == 0 for w, _ in working):
        best_reps = max(r for _, r in working)
        return ProgressionSuggestion(
            "add_reps", 0.0, best_reps + 1, is_bodyweight=True,
            from_weight=0.0, from_reps=best_reps,
        )
    top_weight = max(w for w, _ in working)
    reps_at_top = max(r for w, r in working if w == top_weight)

    rule_name = (rule or {}).get("rule")
    rule_step = _positive_number((rule or {}).get("step"))
    reps_top = _positive_int((rule or {}).get("reps_top"))

    # linear_load: вес растёт каждую тренировку, повторы не при чём.
    if rule_name == "linear_load" and rule_step:
        target_weight = round(top_weight + rule_step, 2)
        return ProgressionSuggestion(
            "add_weight", target_weight, reps_at_top,
            from_weight=top_weight, from_reps=reps_at_top, from_rule=True,
        )

    # double_progression: тот же алгоритм, что и по умолчанию, но верх
    # диапазона и шаг берём из программы, а не угадываем.
    top_of_range = reps_top if (rule_name == "double_progression" and reps_top) else REP_RANGE_MAX
    from_rule = rule_name == "double_progression" and bool(reps_top or rule_step)

    if reps_at_top >= top_of_range:
        step = rule_step or weight_step_for(top_weight, unit, inferred_step)
        target_weight = round(top_weight + step, 2)
        return ProgressionSuggestion(
            "add_weight",
            target_weight,
            _reps_holding_e1rm(target_weight, top_weight, reps_at_top, formula),
            from_weight=top_weight,
            from_reps=reps_at_top,
            from_rule=from_rule,
        )
    return ProgressionSuggestion(
        "add_reps", top_weight, reps_at_top + 1,
        from_weight=top_weight, from_reps=reps_at_top, from_rule=from_rule,
    )


def _positive_number(raw: Any) -> Optional[float]:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _positive_int(raw: Any) -> Optional[int]:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


@dataclass
class Dashboard:
    total_workouts: int
    this_week: int  # workouts in the current calendar week (Mon-Sun)
    last_30_days: int
    days_since_last: Optional[int]  # None if no workouts yet
    week_streak: int  # consecutive weeks with >=1 workout, ending at the current week


def _week_monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def most_frequent_weekday(workout_dates: Iterable[dt.date], min_lead: int = 2) -> int | None:
    """Which weekday (0=Mon) the user trains on most, or None when nothing
    stands out.

    `min_lead` is how many workouts clear of the runner-up the winner must be:
    "твой самый продуктивный день" is a claim about a habit, and 5-vs-4 is
    noise, not a habit.
    """
    counts: dict[int, int] = {}
    for d in workout_dates:
        counts[d.weekday()] = counts.get(d.weekday(), 0) + 1
    if not counts:
        return None
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ranked) == 1:
        return ranked[0][0]
    return ranked[0][0] if ranked[0][1] - ranked[1][1] >= min_lead else None


def max_week_streak(workout_dates: Iterable[dt.date]) -> int:
    """Longest run of consecutive Mon–Sun weeks that each had at least one workout,
    anywhere in history (for the Hall of Fame — unlike Dashboard.week_streak, which
    only measures the streak ending now)."""
    weeks = sorted({_week_monday(d) for d in workout_dates})
    if not weeks:
        return 0
    best = run = 1
    for prev, cur in zip(weeks, weeks[1:], strict=False):
        run = run + 1 if cur - prev == dt.timedelta(days=7) else 1
        best = max(best, run)
    return best


def compute_dashboard(workout_dates: Iterable[dt.date], today: dt.date) -> Dashboard:
    """Summary stats for the main-menu dashboard.

    workout_dates: one date per finished workout (duplicates allowed — two
    workouts on the same day count twice for the totals).

    The weekly streak counts back consecutive Mon-Sun weeks that each have at
    least one workout. A one-week grace is given: if the current week is still
    empty but last week had a workout, the streak stays alive (so it doesn't
    reset to zero just because the user hasn't trained yet this week).
    """
    dates = list(workout_dates)
    if not dates:
        return Dashboard(0, 0, 0, None, 0)

    total = len(dates)
    this_monday = _week_monday(today)
    this_week = sum(1 for d in dates if _week_monday(d) == this_monday)
    last_30_days = sum(1 for d in dates if 0 <= (today - d).days < 30)
    days_since_last = (today - max(dates)).days

    weeks = {_week_monday(d) for d in dates}
    cursor = this_monday
    if cursor not in weeks:
        cursor = cursor - dt.timedelta(days=7)  # grace: allow an empty current week
    streak = 0
    while cursor in weeks:
        streak += 1
        cursor -= dt.timedelta(days=7)

    return Dashboard(
        total_workouts=total,
        this_week=this_week,
        last_30_days=last_30_days,
        days_since_last=days_since_last,
        week_streak=streak,
    )


# Recent-training window/threshold past which the logging screen stops
# spelling out "weight and reps, separated by a space" — someone training
# this often already knows the format, and it costs two lines on every set.
RECENT_TRAINING_WINDOW_DAYS = 14
RECENT_TRAINING_THRESHOLD = 3


def is_seasoned(workout_dates: Iterable[dt.date], today: dt.date) -> bool:
    """True once the athlete has finished RECENT_TRAINING_THRESHOLD+ workouts
    within the last RECENT_TRAINING_WINDOW_DAYS days (today inclusive)."""
    cutoff = today - dt.timedelta(days=RECENT_TRAINING_WINDOW_DAYS - 1)
    recent = sum(1 for d in workout_dates if cutoff <= d <= today)
    return recent >= RECENT_TRAINING_THRESHOLD
