"""«Оценка» прогресса 0–10 — скользящая, на любой день D (без календарных недель).

Формула утверждена владельцем; все окна отсчитываются назад от D — местного дня
атлета. Данные — законченные тренировки (перенесённая история — тоже данные) и
подходы с повторами; e1RM — по формуле атлета (db._e1rm_sql).

**Точка отсчёта.** `start_day` — первая тренировка после последнего перерыва
(BREAK_DAYS дней подряд без тренировок) или первая вообще. Всё, что раньше
неё, не считается нигде: ни блоки регулярности и объёма, ни группы баланса, ни
база прогрессии (вернуться слабее — не «падать»). Новичок и вернувшийся
считаются одинаково, разная только калибровка.

1. **Прогрессия (4).** Для каждого упражнения с весом (не «своим весом без
   отягощения», не на время, не без нагрузки — seed_data.progression_kind и
   is_reps_only): текущее = лучший e1RM за [D-13, D] (нужна хоть одна
   тренировка там), база = лучший за [D-41, D-28] (не раньше start_day).
   Упражнения там не было — лучший из первых 14 дней его истории от
   start_day, но только если первый такой подход был не позже D-28; иначе
   упражнение не оценивается. Изменение в процентах → баллы упражнения
   непрерывно: |Δ| ≤ 1% — шум, 0.5; иначе 0.5 + Δ/10 в пределах [0, 1]
   (+5% → 1, −5% → 0). Базовое движение (seed_data.movement_pattern у
   идентичности каталога) весит 2, изоляция и своё упражнение — 1. Баллы =
   4 × взвешенное среднее. Лёгкая техническая тренировка лучшего за 14 дней не
   опускает.
2. **Регулярность (3).** Четыре блока по 7 дней назад от D; доля блока =
   min(тренировок, 2)/2, баллы = 3 × среднее по блокам с start_day и позже.
3. **Объём (2).** Группы, тренированные за [D-27, D] (встроенные, без
   «Другое»); доля блока = среднее по ним min(подходов в блоке, 6)/6 — больше
   подходов никогда не снижает. Баллы = 2 × среднее долей блоков (с start_day).
   Блок-разгрузка (подходов меньше 60% медианы остальных блоков и верхний e1RM
   ниже медианы их верхних) из среднего выпадает; выпали бы все — берём все.
4. **Баланс (1).** Группы за [D-41, D]; «заброшенная» — ни подхода ни в
   [D-6, D], ни в [D-13, D-7]. Баллы = 1 − заброшенных/групп (1.0 без групп).

Оценка = сумма имеющихся частей / сумма их максимумов × 10, до десятых.

Статусы дня:
- "empty" — тренировок нет вовсе;
- "paused" — последняя тренировка PAUSE_DAYS дней назад и раньше: оценки нет,
  есть последняя оценка до паузы;
- "calibrating" — с start_day прошло меньше 28 дней у новичка (14 — у
  вернувшегося): части считаются, итога нет;
- "partial" — прогрессии ещё нет (у новичка она открывается не раньше
  NEWCOMER_PROGRESSION_DAYS дней истории), итог — по остальным частям;
- "ok" — все четыре части.

Модуль — чистые функции над строками `SetRow` и днями тренировок: формула
проверяется таблицей случаев без базы, `collect` только достаёт строки. Текстов
здесь нет — подписи и реплику тренера собирает api_v1_progress_score через i18n
по ключам и числам из `Lever`/`Part`.
"""

from __future__ import annotations

import bisect
import datetime as dt
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import formatting
import seed_data

PART_KEYS = ("progression", "regularity", "volume", "balance")
POINTS = {"progression": 4.0, "regularity": 3.0, "volume": 2.0, "balance": 1.0}

CURRENT_DAYS = 14
BASELINE_FROM, BASELINE_TO = 41, 28  # база — дни [D-41, D-28]
FALLBACK_MIN_AGE = 28                 # первый подход не позже D-28
FIRST_DAYS = 14                       # «первые 14 дней истории» (так же в db)
NOISE_PCT = 1.0
FULL_PCT = 5.0                        # +5% за окно — полный балл упражнения
COMPOUND_WEIGHT = 2.0
ISOLATION_WEIGHT = 1.0

BLOCKS = 4
TARGET_WORKOUTS = 2                   # две тренировки в неделю — максимум
VOLUME_TARGET_SETS = 6
DELOAD_SETS_SHARE = 0.6
BALANCE_DAYS = 42

BREAK_DAYS = 21                       # столько дней подряд без тренировок — перерыв
PAUSE_DAYS = 14                       # последняя тренировка так давно — пауза
NEWCOMER_CALIBRATION_DAYS = 28
RETURN_CALIBRATION_DAYS = 14
NEWCOMER_PROGRESSION_DAYS = 42

# Сколько дней истории нужно одному дню оценки (самое дальнее окно — база
# прогрессии, D-41) — с запасом.
LOOKBACK_DAYS = 75
HISTORY_DAYS = 28
PERIODS = (28, 90, 365)
# Недостача меньше этого — подсказывать нечего.
LEVER_MIN_POINTS = 0.2

_SKIP_KINDS = frozenset({"bodyweight", "no_load", "timed"})

# (день первого подхода, лучший e1RM первых 14 дней, упражнение с весом)
FirstWeeks = dict[int, tuple[dt.date, float, bool]]


@dataclass(frozen=True)
class SetRow:
    workout_id: int
    day: dt.date
    exercise_id: int
    name: str
    identity: Optional[str]
    #: Встроенная группа (русская идентичность), None — своя или «Другое».
    group: Optional[str]
    e1rm: float
    #: Упражнение с весом — участвует в прогрессии.
    weighted: bool = True


@dataclass(frozen=True)
class LiftScore:
    exercise_id: int
    name: str
    baseline: Optional[float]
    current: float
    change_pct: Optional[float]
    points: Optional[float]
    weight: float

    @property
    def rated(self) -> bool:
        return self.points is not None


@dataclass(frozen=True)
class GroupScore:
    group: str
    sets_7d: int
    sets_28d: int
    #: Доля объёма 0..1; None — группа не тренировалась за 28 дней (только баланс).
    points: Optional[float]
    neglected: bool


@dataclass(frozen=True)
class WeekBlock:
    start: dt.date
    end: dt.date
    workouts: int
    #: Блок с start_day и позже — входит в регулярность и объём.
    counted: bool
    #: Разгрузка — выпала из объёма.
    deload: bool = False


@dataclass(frozen=True)
class Part:
    key: str
    #: Баллы части (не округлённые); None — данных мало.
    points: Optional[float]
    #: Числа для подписи под частью (ключ подписи — в api_v1_progress_score).
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def max(self) -> float:
        return POINTS[self.key]


@dataclass(frozen=True)
class Lever:
    """Один шаг, который сильнее всего поднимет оценку, — вид и числа для текста."""

    kind: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Phase:
    """Где атлет на день D: старт отсчёта, вернулся ли, последняя тренировка."""

    start_day: dt.date
    returned: bool
    last_day: dt.date
    workouts_since_start: int


@dataclass
class DayScore:
    day: dt.date
    status: str
    score: Optional[float]
    parts: list[Part]
    lifts: list[LiftScore] = field(default_factory=list)
    groups: list[GroupScore] = field(default_factory=list)
    weeks: list[WeekBlock] = field(default_factory=list)
    phase: Optional[Phase] = None
    progression_from: Optional[dt.date] = None
    lever: Optional[Lever] = None
    # Заполняет report() для статуса "paused".
    last_score: Optional[float] = None
    last_score_date: Optional[dt.date] = None

    def part(self, key: str) -> Part:
        return next(p for p in self.parts if p.key == key)

    @property
    def deload(self) -> bool:
        return bool(self.weeks) and self.weeks[0].deload

    @property
    def calibration_days(self) -> int:
        returned = self.phase is not None and self.phase.returned
        return RETURN_CALIBRATION_DAYS if returned else NEWCOMER_CALIBRATION_DAYS

    @property
    def ready_on(self) -> Optional[dt.date]:
        if self.phase is None:
            return None
        return self.phase.start_day + dt.timedelta(days=self.calibration_days)

    @property
    def pause_days(self) -> Optional[int]:
        return (self.day - self.phase.last_day).days if self.phase else None


# --- фаза ---------------------------------------------------------------------


def phase_at(workout_days: list[dt.date], d: dt.date) -> Optional[Phase]:
    """Старт отсчёта на день D: первая тренировка после последнего перерыва
    (BREAK_DAYS дней подряд без тренировок) или первая вообще. None — до D
    тренировок нет."""
    n = bisect.bisect_right(workout_days, d)
    if n == 0:
        return None
    i = n - 1
    while i > 0 and (workout_days[i] - workout_days[i - 1]).days <= BREAK_DAYS:
        i -= 1
    return Phase(workout_days[i], i > 0, workout_days[n - 1], n - i)


# --- части ------------------------------------------------------------------


def lift_points(change_pct: float) -> float:
    """Баллы упражнения 0..1 за изменение e1RM в процентах."""
    if abs(change_pct) <= NOISE_PCT:
        return 0.5
    return min(1.0, max(0.0, 0.5 + change_pct / (2 * FULL_PCT)))


def _catalog_identities() -> frozenset[str]:
    return frozenset(name for _group, name in seed_data.EXERCISE_TEMPLATES)


def lift_weight(identity: Optional[str], group: Optional[str]) -> float:
    """Вес упражнения в прогрессии: базовое движение каталога — 2, остальное — 1."""
    if identity and identity in _catalog_identities() and seed_data.movement_pattern(identity, group):
        return COMPOUND_WEIGHT
    return ISOLATION_WEIGHT


def is_weighted(identity: Optional[str], bodyweight_load: Optional[str]) -> bool:
    """Участвует ли упражнение в прогрессии: вес есть и e1RM что-то значит."""
    if seed_data.progression_kind(identity) in _SKIP_KINDS:
        return False
    return not seed_data.is_reps_only(identity, bodyweight_load)


def first_weeks_from_rows(rows: Iterable[SetRow], start: dt.date) -> FirstWeeks:
    """Первые 14 дней каждого упражнения начиная со `start` — по строкам
    (после возвращения: база прогрессии — только то, что после перерыва)."""
    first: dict[int, dt.date] = {}
    weighted: dict[int, bool] = {}
    rows = [r for r in rows if r.day >= start]
    for r in rows:
        if r.exercise_id not in first or r.day < first[r.exercise_id]:
            first[r.exercise_id] = r.day
        weighted[r.exercise_id] = r.weighted
    best: dict[int, float] = {}
    for r in rows:
        if r.day <= first[r.exercise_id] + dt.timedelta(days=FIRST_DAYS - 1):
            best[r.exercise_id] = max(best.get(r.exercise_id, 0.0), r.e1rm)
    return {ex: (day, best.get(ex, 0.0), weighted[ex]) for ex, day in first.items()}


def _in(day: dt.date, lo: dt.date, hi: dt.date) -> bool:
    return lo <= day <= hi


def progression(
    rows: list[SetRow], first_weeks: FirstWeeks, d: dt.date, start: Optional[dt.date] = None,
    *, allowed: bool = True,
) -> tuple[Optional[float], list[LiftScore]]:
    """Баллы прогрессии и разбор по упражнениям (оценённые — слабые первыми,
    потом неоценённые). `allowed=False` — прогрессию ещё рано считать: все
    упражнения идут неоценёнными."""
    cur_lo = d - dt.timedelta(days=CURRENT_DAYS - 1)
    base_lo, base_hi = d - dt.timedelta(days=BASELINE_FROM), d - dt.timedelta(days=BASELINE_TO)
    if start is not None:
        base_lo = max(base_lo, start)
    current: dict[int, float] = {}
    base: dict[int, float] = {}
    meta: dict[int, SetRow] = {}
    for r in rows:
        if not r.weighted or r.e1rm <= 0 or r.day > d:
            continue
        if r.day >= cur_lo:
            current[r.exercise_id] = max(current.get(r.exercise_id, 0.0), r.e1rm)
            meta[r.exercise_id] = r
        elif _in(r.day, base_lo, base_hi):
            base[r.exercise_id] = max(base.get(r.exercise_id, 0.0), r.e1rm)
    lifts = []
    for ex, cur in current.items():
        m = meta[ex]
        baseline = base.get(ex) if allowed else None
        if baseline is None and allowed:
            first = first_weeks.get(ex)
            if first and first[0] <= d - dt.timedelta(days=FALLBACK_MIN_AGE) and first[1] > 0:
                baseline = first[1]
        weight = lift_weight(m.identity, m.group)
        if baseline:
            change = (cur / baseline - 1) * 100
            lifts.append(LiftScore(ex, m.name, baseline, cur, change, lift_points(change), weight))
        else:
            lifts.append(LiftScore(ex, m.name, None, cur, None, None, weight))
    rated = sorted((lift for lift in lifts if lift.rated), key=lambda lift: (lift.points, lift.name))
    unrated = sorted((lift for lift in lifts if not lift.rated), key=lambda lift: lift.name)
    if not rated:
        return None, unrated
    mean = sum(lift.weight * lift.points for lift in rated) / sum(lift.weight for lift in rated)
    return POINTS["progression"] * mean, rated + unrated


def blocks(d: dt.date) -> list[tuple[dt.date, dt.date]]:
    """Четыре 7-дневных блока назад от D, свежий первым."""
    return [(d - dt.timedelta(days=7 * k + 6), d - dt.timedelta(days=7 * k)) for k in range(BLOCKS)]


def week_blocks(workout_days: list[dt.date], start: Optional[dt.date], d: dt.date) -> list[WeekBlock]:
    return [
        WeekBlock(
            lo, hi, bisect.bisect_right(workout_days, hi) - bisect.bisect_left(workout_days, lo),
            counted=start is not None and hi >= start,
        )
        for lo, hi in blocks(d)
    ]


def regularity(weeks: list[WeekBlock]) -> float:
    counted = [w for w in weeks if w.counted] or weeks[:1]
    share = sum(min(w.workouts, TARGET_WORKOUTS) / TARGET_WORKOUTS for w in counted) / len(counted)
    return POINTS["regularity"] * share


def _counted_group(group: Optional[str]) -> bool:
    return bool(group) and group.strip().lower() not in formatting.VOLUME_HIDDEN_GROUPS


def deload_blocks(totals: list[int], tops: list[float]) -> list[bool]:
    """Какие блоки — разгрузка: подходов меньше 60% медианы остальных и верхний
    e1RM ниже медианы их верхних. Пустой блок — пропуск, а не разгрузка."""
    out = []
    for i, (total, top) in enumerate(zip(totals, tops, strict=True)):
        others_t = totals[:i] + totals[i + 1:]
        others_top = tops[:i] + tops[i + 1:]
        out.append(
            bool(others_t) and total > 0
            and total < DELOAD_SETS_SHARE * statistics.median(others_t)
            and top < statistics.median(others_top)
        )
    return out


def volume_and_balance(
    rows: list[SetRow], weeks: list[WeekBlock], d: dt.date
) -> tuple[Optional[float], float, list[WeekBlock], list[GroupScore]]:
    """Объём (по блокам с отсчёта, без разгрузок), баланс, блоки с пометкой
    разгрузки и разбор по группам. `rows` — уже без подходов до start_day."""
    rows = [r for r in rows if _counted_group(r.group)]
    groups28 = {r.group for r in rows if _in(r.day, d - dt.timedelta(days=27), d)}
    groups42 = {r.group for r in rows if _in(r.day, d - dt.timedelta(days=BALANCE_DAYS - 1), d)}
    per_block: list[dict[str, int]] = []
    totals: list[int] = []
    tops: list[float] = []
    for w in weeks:
        inside = [r for r in rows if _in(r.day, w.start, w.end)]
        counts: dict[str, int] = {}
        for r in inside:
            counts[r.group] = counts.get(r.group, 0) + 1
        per_block.append(counts)
        totals.append(len(inside))
        tops.append(max((r.e1rm for r in inside if r.weighted), default=0.0))
    idx = [i for i, w in enumerate(weeks) if w.counted]
    flags = deload_blocks([totals[i] for i in idx], [tops[i] for i in idx])
    deload = {i for i, flag in zip(idx, flags, strict=True) if flag}
    weeks = [
        WeekBlock(w.start, w.end, w.workouts, w.counted, i in deload) for i, w in enumerate(weeks)
    ]
    used = [i for i in idx if i not in deload] or idx
    shares: dict[str, float] = {}
    if used:
        for g in groups28:
            shares[g] = sum(
                min(per_block[i].get(g, 0), VOLUME_TARGET_SETS) / VOLUME_TARGET_SETS for i in used
            ) / len(used)
    volume = POINTS["volume"] * sum(shares.values()) / len(shares) if shares else None
    week, prev = per_block[0], per_block[1]
    neglected = {g for g in groups42 if not week.get(g) and not prev.get(g)}
    balance = 1.0 - len(neglected) / len(groups42) if groups42 else 1.0
    order = {name: i for i, (name, *_rest) in enumerate(seed_data.MUSCLE_GROUP_PRESETS)}
    groups = [
        GroupScore(g, week.get(g, 0), sum(b.get(g, 0) for b in per_block), shares.get(g), g in neglected)
        for g in sorted(groups42, key=lambda g: (order.get(g, 99), g))
    ]
    return volume, POINTS["balance"] * balance, weeks, groups


def progression_from(first_weeks: FirstWeeks, phase: Phase, d: dt.date) -> Optional[dt.date]:
    """Ближайший день, когда хоть одно упражнение с весом получит базу: его
    первый подход с отсчёта + 28 дней (у новичка — не раньше 42 дней истории).
    None — такого дня впереди нет."""
    floor = None if phase.returned else phase.start_day + dt.timedelta(days=NEWCOMER_PROGRESSION_DAYS)
    out = []
    for first_day, _best, weighted in first_weeks.values():
        if not weighted or first_day < phase.start_day:
            continue
        day = first_day + dt.timedelta(days=FALLBACK_MIN_AGE)
        if floor is not None:
            day = max(day, floor)
        if day > d:
            out.append(day)
    return min(out) if out else None


# --- день целиком ------------------------------------------------------------


def _lever(s: DayScore, unit: str) -> Optional[Lever]:
    if s.status == "empty":
        return Lever("empty")
    if s.status == "paused":
        if s.last_score is None:
            return Lever("paused_no_score", {"days": s.pause_days})
        return Lever("paused", {"days": s.pause_days, "score": s.last_score})
    if s.status == "calibrating":
        if s.phase.returned:
            return Lever("comeback", {"date": s.ready_on})
        if s.weeks[0].workouts >= TARGET_WORKOUTS:
            return Lever("calibrating_keep", {"date": s.ready_on})
        return Lever("calibrating", {"date": s.ready_on})
    lever = _part_lever(s, unit)
    if lever is None and s.progression_from is not None:
        return Lever("progression_from", {"date": s.progression_from})
    return lever


def _part_lever(s: DayScore, unit: str) -> Optional[Lever]:
    missing = {p.key: p.max - p.points for p in s.parts if p.points is not None}
    if s.deload:
        missing.pop("volume", None)  # в разгрузку объём не добираем
    if not missing:
        return None
    key = max(missing, key=lambda k: (missing[k], -PART_KEYS.index(k)))
    if missing[key] < LEVER_MIN_POINTS:
        return None
    if key == "progression":
        weakest = next(lift for lift in s.lifts if lift.rated)
        step = formatting.format_weight({"kg": 2.5, "lb": 5.0}.get(unit, 2.5))
        change = weakest.change_pct or 0.0
        pct = formatting.format_weight(round(abs(change), 1))
        if abs(change) <= NOISE_PCT:
            return Lever("lift_flat", {"exercise": weakest.name, "step": step})
        if change > 0:
            return Lever("lift_up", {"exercise": weakest.name, "pct": pct, "step": step})
        return Lever("lift_down", {"exercise": weakest.name, "pct": pct})
    if key == "regularity":
        done = s.weeks[0].workouts
        if done == 0:
            return Lever("regularity_none")
        if done < TARGET_WORKOUTS:
            return Lever("regularity_one")
        return Lever("regularity_keep")
    if key == "volume":
        short = [g for g in s.groups if g.points is not None and g.sets_7d < VOLUME_TARGET_SETS]
        if not short:
            return Lever("volume_keep")
        g = min(short, key=lambda g: g.sets_7d)
        need = VOLUME_TARGET_SETS - g.sets_7d
        if g.sets_7d == 0:
            return Lever("volume_zero", {"group": g.group, "need": need})
        return Lever("volume_more", {"group": g.group, "sets": g.sets_7d, "need": need})
    neglected = [g for g in s.groups if g.neglected]
    return Lever("balance", {"group": neglected[0].group}) if neglected else None


def _blank(d: dt.date, status: str, workout_days: list[dt.date], phase: Optional[Phase]) -> DayScore:
    return DayScore(
        d, status, None, [Part(k, None) for k in PART_KEYS],
        weeks=week_blocks(workout_days, None, d), phase=phase,
    )


def score_day(
    rows: list[SetRow],
    workout_days: list[dt.date],
    first_weeks: FirstWeeks,
    d: dt.date,
    unit: str = "kg",
    *,
    full: bool = True,
) -> DayScore:
    """Оценка на день D. `rows` — подходы хотя бы за LOOKBACK_DAYS дней до D
    (а после возвращения — с его начала; более поздние отбрасываются),
    `workout_days` — местные дни всех законченных тренировок по возрастанию,
    `first_weeks` — первые 14 дней каждого упражнения за всю историю.
    `full=False` — без реплики тренера (для ряда истории)."""
    phase = phase_at(workout_days, d)
    if phase is None:
        s = _blank(d, "empty", workout_days, None)
    elif (d - phase.last_day).days >= PAUSE_DAYS:
        s = _blank(d, "paused", workout_days, phase)
    else:
        s = _score_active(rows, workout_days, first_weeks, d, phase)
    if full:
        s.lever = _lever(s, unit)
    return s


def _score_active(
    rows: list[SetRow], workout_days: list[dt.date], first_weeks: FirstWeeks, d: dt.date, phase: Phase
) -> DayScore:
    start = phase.start_day
    history = (d - start).days
    window = [r for r in rows if _in(r.day, max(start, d - dt.timedelta(days=LOOKBACK_DAYS)), d)]
    if phase.returned:
        first_weeks = first_weeks_from_rows(rows, start)
    allowed = phase.returned or history >= NEWCOMER_PROGRESSION_DAYS
    prog, lifts = progression(window, first_weeks, d, start, allowed=allowed)
    weeks = week_blocks(workout_days, start, d)
    reg = regularity(weeks)
    vol, bal, weeks, groups = volume_and_balance(window, weeks, d)
    rated = [lift for lift in lifts if lift.rated]
    counted = [w for w in weeks if w.counted]
    parts = [
        Part("progression", prog, {"up": sum(1 for lift in rated if lift.change_pct > NOISE_PCT),
                                   "n": len(rated)}),
        Part("regularity", reg, {"full": sum(1 for w in counted if w.workouts >= TARGET_WORKOUTS),
                                 "n": len(counted)}),
        Part("volume", vol, {"full": sum(1 for g in groups if g.points == 1.0),
                             "n": sum(1 for g in groups if g.points is not None),
                             "deload": bool(weeks[0].deload)}),
        Part("balance", bal, {"neglected": sum(1 for g in groups if g.neglected), "groups": len(groups)}),
    ]
    s = DayScore(d, "ok", None, parts, lifts, groups, weeks, phase)
    if prog is None:
        s.progression_from = progression_from(first_weeks, phase, d)
    if history < s.calibration_days:
        s.status = "calibrating"
        return s
    have = [p for p in parts if p.points is not None]
    s.score = round(sum(p.points for p in have) / sum(p.max for p in have) * 10, 1)
    s.status = "ok" if len(have) == len(parts) else "partial"
    return s


# --- ряд и отчёт ---------------------------------------------------------------


def history_days(d: dt.date, period_days: int) -> list[dt.date]:
    """Дни ряда, старые сначала: на 28 дней — каждый день, на дольше — каждый
    седьмой назад от D (13 точек на 90 дней, 53 на 365)."""
    step = 1 if period_days <= HISTORY_DAYS else 7
    n = (period_days - 1) // step + 1
    return [d - dt.timedelta(days=step * i) for i in range(n - 1, -1, -1)]


@dataclass
class ScoreReport:
    today: DayScore
    week_ago: DayScore
    history: list[DayScore]
    period_days: int

    @property
    def delta_7d(self) -> Optional[float]:
        if self.today.score is None or self.week_ago.score is None:
            return None
        return round(self.today.score - self.week_ago.score, 1)

    @property
    def best(self) -> Optional[DayScore]:
        graded = [h for h in self.history if h.score is not None]
        # Ничья — свежий день.
        return max(reversed(graded), key=lambda h: h.score) if graded else None


def needed_from(workout_days: list[dt.date], d: dt.date, period_days: int) -> dt.date:
    """С какого дня грузить подходы, чтобы посчитать ряд за `period_days`:
    окно LOOKBACK_DAYS у самого раннего дня ряда, начало возвращения (база
    прогрессии после перерыва) и последняя оценка перед паузой."""
    first_day = history_days(d, period_days)[0]
    out = first_day - dt.timedelta(days=LOOKBACK_DAYS)
    for day in (first_day, d):
        phase = phase_at(workout_days, day)
        if phase is None:
            continue
        out = min(out, phase.last_day - dt.timedelta(days=LOOKBACK_DAYS))
        if phase.returned:
            out = min(out, phase.start_day)
    return out


def report(
    rows: list[SetRow],
    workout_days: list[dt.date],
    first_weeks: FirstWeeks,
    d: dt.date,
    unit: str = "kg",
    period_days: int = HISTORY_DAYS,
) -> ScoreReport:
    """Оценка на D, на D-7 и ряд за `period_days` — из одного набора строк."""
    rows = sorted(rows, key=lambda r: r.day)
    days = [r.day for r in rows]

    def at(day: dt.date) -> DayScore:
        phase = phase_at(workout_days, day)
        lo_day = day - dt.timedelta(days=LOOKBACK_DAYS)
        if phase is not None and phase.returned:
            lo_day = min(lo_day, phase.start_day)
        lo = bisect.bisect_left(days, lo_day)
        hi = bisect.bisect_right(days, day)
        return score_day(rows[lo:hi], workout_days, first_weeks, day, unit, full=False)

    history = [at(day) for day in history_days(d, period_days)]
    today = history[-1]
    if today.status == "paused":
        # Последняя оценка до паузы: на день последней тренировки, а если её
        # тогда не было (калибровка) — последняя непустая из ряда.
        last = at(today.phase.last_day)
        if last.score is None:
            last = next((h for h in reversed(history) if h.score is not None), last)
        if last.score is not None:
            today.last_score, today.last_score_date = last.score, last.day
    today.lever = _lever(today, unit)
    week_ago = next((h for h in history if h.day == d - dt.timedelta(days=7)), None) or at(d - dt.timedelta(days=7))
    return ScoreReport(today, week_ago, history, period_days)


# --- база ---------------------------------------------------------------------


def rows_from_db(raw: Iterable[Any]) -> list[SetRow]:
    return [
        SetRow(
            workout_id=r["workout_id"],
            day=dt.date.fromisoformat(r["day"]),
            exercise_id=r["exercise_id"],
            name=r["display_name"],
            identity=r["original_name"],
            group=r["group_name"],
            e1rm=float(r["e1rm"] or 0),
            weighted=is_weighted(r["original_name"], r["bodyweight_load"]),
        )
        for r in raw
    ]


def first_weeks_from_db(raw: Iterable[Any]) -> FirstWeeks:
    return {
        r["ex"]: (
            dt.date.fromisoformat(r["first_day"]),
            float(r["best"] or 0),
            is_weighted(r["original_name"], r["bodyweight_load"]),
        )
        for r in raw
    }


async def collect(user_id: int, user: Any, d: dt.date, period_days: int = HISTORY_DAYS) -> ScoreReport:
    """Всё для оценки одним заходом в базу: дни всех тренировок, подходы за
    нужный отрезок (needed_from) и первые две недели каждого упражнения."""
    import db
    import timeutil

    tz = timeutil.offset_hours(user)
    formula = user["e1rm_formula"]
    workout_days = [
        dt.date.fromisoformat(x) for x in await db.list_finished_workout_dates(user_id, tz_offset=tz)
    ]
    start = needed_from(workout_days, d, period_days)
    raw = await db.progress_score_sets(user_id, start.isoformat(), d.isoformat(), formula, tz_offset=tz)
    first = first_weeks_from_db(await db.exercise_first_two_weeks_e1rm(user_id, formula, tz_offset=tz))
    return report(rows_from_db(raw), workout_days, first, d, user["unit"], period_days)
