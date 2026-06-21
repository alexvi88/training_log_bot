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
    is_warmup: bool = False
    workout_id: Optional[int] = None
    started_at: Optional[str] = None


@dataclass
class SessionStats:
    workout_id: int
    started_at: str
    sets: list[SetRow]
    formula: str = "epley"

    @property
    def working_sets(self) -> list[SetRow]:
        return [s for s in self.sets if not s.is_warmup]

    @property
    def tonnage(self) -> float:
        return sum(s.weight * s.reps for s in self.working_sets)

    @property
    def total_reps(self) -> int:
        return sum(s.reps for s in self.working_sets)

    @property
    def is_bodyweight_mode(self) -> bool:
        ws = self.working_sets
        return bool(ws) and all(s.weight == 0 for s in ws)

    @property
    def top_set(self) -> Optional[SetRow]:
        ws = self.working_sets
        if not ws:
            return None
        if self.is_bodyweight_mode:
            return max(ws, key=lambda s: s.reps)
        return max(ws, key=lambda s: e1rm(s.weight, s.reps, self.formula))

    @property
    def top_e1rm(self) -> float:
        ts = self.top_set
        if ts is None:
            return 0.0
        return e1rm(ts.weight, ts.reps, self.formula)

    @property
    def max_reps_in_set(self) -> int:
        ws = self.working_sets
        return max((s.reps for s in ws), default=0)


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


def linear_trend(points: list[tuple[dt.datetime, float]]) -> Optional[Trend]:
    """Least-squares slope of y over time, expressed per week."""
    if len(points) < 2:
        return None
    t0 = points[0][0]
    xs = [(p[0] - t0).total_seconds() / 604800 for p in points]  # weeks
    ys = [p[1] for p in points]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return Trend(slope_per_week=0.0, direction="flat")
    slope = num / den
    if abs(slope) < 1e-6:
        direction = "flat"
    else:
        direction = "up" if slope > 0 else "down"
    return Trend(slope_per_week=slope, direction=direction)


@dataclass
class PersonalRecords:
    max_weight: float = 0.0
    max_e1rm: float = 0.0
    max_session_tonnage: float = 0.0
    max_reps_at_weight: dict[float, int] = field(default_factory=dict)


def compute_personal_records(sessions: list[SessionStats]) -> PersonalRecords:
    pr = PersonalRecords()
    for session in sessions:
        if session.tonnage > pr.max_session_tonnage:
            pr.max_session_tonnage = session.tonnage
        for s in session.working_sets:
            if s.weight > pr.max_weight:
                pr.max_weight = s.weight
            val = e1rm(s.weight, s.reps, session.formula)
            if val > pr.max_e1rm:
                pr.max_e1rm = val
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

    for s in new_session.working_sets:
        val = e1rm(s.weight, s.reps, new_session.formula)
        if val > prior_pr.max_e1rm:
            records.append(NewRecord(kind="e1rm", value=val))
            prior_pr.max_e1rm = val

    reps_records: list[NewRecord] = []
    for s in new_session.working_sets:
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


def volume_by_muscle_group(
    rows: Iterable[tuple[str, float, int]]
) -> dict[str, float]:
    """rows: iterable of (group_label, weight, reps) for working sets in a period."""
    totals: dict[str, float] = {}
    for group_label, weight, reps in rows:
        totals[group_label] = totals.get(group_label, 0.0) + weight * reps
    return totals
