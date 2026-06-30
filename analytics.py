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


def brzycki_e1rm(weight: float, reps: int) -> float:
    if reps <= 1:
        return weight
    if reps >= 37:
        return weight
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

    # Drop records dominated by another from the same session (same reps at a
    # lower weight, or same weight at fewer reps) — only the best one is worth a notification.
    for r in reps_records:
        dominated = any(
            other is not r and other.extra >= r.extra and other.value >= r.value
            for other in reps_records
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


@dataclass
class Dashboard:
    total_workouts: int
    this_week: int  # workouts in the current calendar week (Mon-Sun)
    last_30_days: int
    days_since_last: Optional[int]  # None if no workouts yet
    week_streak: int  # consecutive weeks with >=1 workout, ending at the current week


def _week_monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


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
