"""Pure-Python metrics: e1RM, tonnage, trend regression, PR detection.

Operates on plain dicts/rows of working sets, so it stays decoupled from the
DB layer and is trivially testable.
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable, Optional


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
) -> Optional[ProgressionSuggestion]:
    """Next-session target from last session's sets, by double progression.

    While the top working set is still inside the rep range, add a rep at the
    same weight; once it crossed the top of the range (>= REP_RANGE_MAX), bump
    the weight by one step (see weight_step_for) and restart at the lowest rep
    count that doesn't give back the e1RM already earned (_reps_holding_e1rm).
    Bodyweight sets (weight 0) simply chase one more rep.
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
    if reps_at_top >= REP_RANGE_MAX:
        step = weight_step_for(top_weight, unit, inferred_step)
        target_weight = round(top_weight + step, 2)
        return ProgressionSuggestion(
            "add_weight",
            target_weight,
            _reps_holding_e1rm(target_weight, top_weight, reps_at_top, formula),
            from_weight=top_weight,
            from_reps=reps_at_top,
        )
    return ProgressionSuggestion(
        "add_reps", top_weight, reps_at_top + 1, from_weight=top_weight, from_reps=reps_at_top
    )


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
