"""«Оценка» прогресса 0–10 на любой день D: календарные недели и скользящие окна силы.

Формула утверждена владельцем; D — местный день атлета. Данные — законченные
тренировки (перенесённая история — тоже данные) и подходы с повторами; e1RM —
по формуле атлета (db._e1rm_sql).

**Точка отсчёта.** `start_day` — первая тренировка после последнего перерыва
(BREAK_DAYS дней подряд без тренировок) или первая вообще. Всё, что раньше
неё, не считается нигде: ни недели регулярности и объёма, ни группы объёма, ни
база прогрессии (вернуться слабее — не «падать»). Новичок и вернувшийся
считаются одинаково, разная только калибровка.

**Недели** регулярности и объёма — календарные, понедельник–воскресенье, по
местному дню. Берутся четыре последние законченные недели и текущая,
незаконченная: текущая идёт в счёт, только если не опускает итог — считаются
оба набора, (текущая + три законченные) и (четыре законченные), и остаётся
лучший по регулярности и объёму вместе (ничья — с текущей). Так у атлета с
ровными двумя тренировками в неделю оценка не скачет изо дня в день: скользящие
7-дневные блоки то ловили, то теряли тренировку на краю.

1. **Прогрессия (5).** Для каждого упражнения с весом (seed_data.progression_kind
   и is_reps_only): текущее = лучший e1RM за [D-13, D] (нужна хоть одна
   тренировка там), база = лучший за [D-41, D-28] (не раньше start_day).
   Упражнения там не было — лучший из первых 14 дней его истории от
   start_day, но только если первый такой подход был не позже D-28; иначе
   упражнение не оценивается. Изменение Δ% → баллы упражнения по
   кусочно-линейной шкале без полосы шума: Δ ≤ −3% → 0; до 0 — 0.3 + 0.1·Δ;
   выше 0 — 0.3 + 0.14·Δ; +5% и больше → 1 (lift_points). Упражнения своим
   весом и без нагрузки («bodyweight», «no_load», свои «только повторы»)
   меряются той же шкалой по лучшим повторам в одном подходе; на время — не
   оцениваются. Полоса «стоит» (±NOISE_PCT, у повторов ±REPS_NOISE_PCT) — только
   для слов тренера. Базовое движение (seed_data.movement_pattern
   у идентичности каталога) весит 2, изоляция и своё упражнение — 1. Баллы =
   5 × взвешенное среднее. Окна скользящие: это максимумы, а не счёт, и от
   края окна они не дёргаются. Лёгкая техническая тренировка лучшего за 14
   дней не опускает.
2. **Регулярность (3).** Доля недели = min(тренировок, 2)/2, баллы = 3 ×
   среднее по неделям с start_day и позже.
3. **Объём (2).** Группы, тренированные за [D-41, D] (встроенные, без
   «Другое»); доля недели = среднее по ним min(подходов в неделе, 6)/6 —
   больше подходов никогда не снижает, а брошенная группа идёт в счёт нулём
   (бывший «баланс» сложен сюда: одна брошенная группа не штрафуется дважды).
   Баллы = 2 × среднее долей недель (с start_day). Неделя-разгрузка (подходов
   меньше 60% медианы остальных недель и верхний e1RM ниже медианы их верхних)
   из среднего выпадает; выпали бы все — берём все. «Заброшенная» группа
   (`neglected`: была за 6 недель, ни подхода за [D-13, D]) — только подсветка
   и реплика тренера.

Оценка = сумма частей / сумма их максимумов × 10, до десятых. Прогрессии ещё
нет — она идёт в итог баллом «без изменений» (NEUTRAL_LIFT_POINTS = 0.3 на
упражнение, 1.5 из 5): аккуратный новичок видит 6.5, а не 10, и рост потом
оценку поднимает, а не «роняет».

Статусы дня:
- "empty" — тренировок нет вовсе;
- "paused" — последняя тренировка PAUSE_DAYS дней назад и раньше: оценки нет,
  есть последняя оценка до паузы;
- "calibrating" — с start_day прошло меньше 28 дней у новичка (14 — у
  вернувшегося): части считаются, итога нет;
- "partial" — прогрессии ещё нет (у новичка она открывается не раньше
  NEWCOMER_PROGRESSION_DAYS дней истории) или нет групп для объёма;
- "ok" — все части.

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

PART_KEYS = ("progression", "regularity", "volume")
POINTS = {"progression": 5.0, "regularity": 3.0, "volume": 2.0}

CURRENT_DAYS = 14
BASELINE_FROM, BASELINE_TO = 41, 28  # база — дни [D-41, D-28]
FALLBACK_MIN_AGE = 28                 # первый подход не позже D-28
FIRST_DAYS = 14                       # «первые 14 дней истории» (так же в db)
# Шкала баллов упражнения по изменению Δ% (кусочно-линейная, без полосы шума):
# Δ ≤ −3% → 0; −3% < Δ ≤ 0 → 0.3 + 0.1·Δ; 0 < Δ < +5% → 0.3 + 0.14·Δ; Δ ≥ +5% → 1.
ZERO_PCT = -3.0                       # падение на 3% и больше — ноль
FLAT_POINTS = 0.3                     # без изменений
DOWN_SLOPE = 0.1                      # балла за процент падения
UP_SLOPE = 0.14                       # балла за процент роста
FULL_PCT = 5.0                        # +5% за окно — полный балл упражнения
# Полоса «стоит на месте» — только для слов тренера (стоит / растёт / проседает),
# в баллы не входит. Повторы грубые: ±1 повтор из 20 — ещё «стоит».
NOISE_PCT = 1.0
REPS_NOISE_PCT = 5.0
COMPOUND_WEIGHT = 2.0
ISOLATION_WEIGHT = 1.0
# Прогрессии ещё нет — в итог она идёт баллом «без изменений» на упражнение
# (0.3 × 5 = 1.5 из 5).
NEUTRAL_LIFT_POINTS = FLAT_POINTS

BLOCKS = 4                            # недель в счёте
TARGET_WORKOUTS = 2                   # две тренировки в неделю — максимум
VOLUME_TARGET_SETS = 6
DELOAD_SETS_SHARE = 0.6
GROUP_DAYS = 42                       # группы объёма — тренированные за 6 недель
NEGLECT_DAYS = 14                     # ни подхода столько дней — группа заброшена

BREAK_DAYS = 21                       # столько дней подряд без тренировок — перерыв
PAUSE_DAYS = 14                       # последняя тренировка так давно — пауза
NEWCOMER_CALIBRATION_DAYS = 28
RETURN_CALIBRATION_DAYS = 14
NEWCOMER_PROGRESSION_DAYS = 42

# Сколько дней истории нужно одному дню оценки (самое дальнее окно — база
# прогрессии, D-41) — с запасом.
LOOKBACK_DAYS = 75
HISTORY_DAYS = 28
HISTORY_STEP = 7
PERIODS = (28, 90, 365)
# Недостача меньше этого — подсказывать нечего.
LEVER_MIN_POINTS = 0.2

# Чем меряется прогрессия упражнения: e1RM или лучшие повторы в подходе;
# None — не оценивается (на время).
METRIC_E1RM = "e1rm"
METRIC_REPS = "reps"

# (день первого подхода, лучшее значение меры первых 14 дней, мера или None)
FirstWeeks = dict[int, tuple[dt.date, float, Optional[str]]]


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
    #: Мера прогрессии упражнения (METRIC_E1RM / METRIC_REPS), None — не оценивается.
    metric: Optional[str] = METRIC_E1RM
    reps: int = 0

    @property
    def weighted(self) -> bool:
        return self.metric == METRIC_E1RM

    @property
    def value(self) -> float:
        return self.e1rm if self.metric == METRIC_E1RM else float(self.reps)


@dataclass(frozen=True)
class LiftScore:
    exercise_id: int
    name: str
    #: База и текущее — в мере упражнения (e1RM или повторы).
    baseline: Optional[float]
    current: float
    change_pct: Optional[float]
    points: Optional[float]
    weight: float
    metric: str = METRIC_E1RM

    @property
    def rated(self) -> bool:
        return self.points is not None

    @property
    def by_reps(self) -> bool:
        return self.metric == METRIC_REPS

    @property
    def noise_pct(self) -> float:
        return REPS_NOISE_PCT if self.by_reps else NOISE_PCT


@dataclass(frozen=True)
class GroupScore:
    group: str
    #: Подходов на текущей календарной неделе (до D).
    sets_7d: int
    #: Подходов за все четыре недели набора (с разгрузкой).
    sets_28d: int
    #: Доля объёма 0..1; None — недель в счёте нет.
    points: Optional[float]
    neglected: bool
    #: Подходов в среднем за неделю — по тем же неделям, что и балл (в счёте,
    #: без разгрузок). Это и рисует карточка объёма: число и балл не расходятся.
    sets_per_week: float = 0.0
    #: Подходы по каждой из недель набора, свежая первой (как `weeks`), —
    #: и разгрузки, и недели вне счёта тоже: что из них идёт в балл, говорят
    #: флаги недель.
    week_sets: tuple[int, ...] = ()


@dataclass(frozen=True)
class WeekBlock:
    start: dt.date
    end: dt.date
    workouts: int
    #: Неделя с start_day и позже — входит в регулярность и объём.
    counted: bool
    #: Разгрузка — выпала из объёма.
    deload: bool = False
    #: Текущая (незаконченная) календарная неделя.
    current: bool = False


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
    #: Недели в счёте, свежая первой (текущая — только если вошла в счёт).
    weeks: list[WeekBlock] = field(default_factory=list)
    phase: Optional[Phase] = None
    progression_from: Optional[dt.date] = None
    lever: Optional[Lever] = None
    #: Текущая календарная неделя — для реплики «на этой неделе», в счёте она или нет.
    this_week: Optional[WeekBlock] = None
    # Заполняет report() для статуса "paused".
    last_score: Optional[float] = None
    last_score_date: Optional[dt.date] = None

    def part(self, key: str) -> Part:
        return next(p for p in self.parts if p.key == key)

    @property
    def deload(self) -> bool:
        """Текущая неделя в счёте и она — разгрузка."""
        return bool(self.weeks) and self.weeks[0].current and self.weeks[0].deload

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
    """Баллы упражнения 0..1 за изменение меры (e1RM или повторов) в процентах."""
    if change_pct <= ZERO_PCT:
        return 0.0
    if change_pct <= 0:
        return FLAT_POINTS + DOWN_SLOPE * change_pct
    if change_pct >= FULL_PCT:
        return 1.0
    return FLAT_POINTS + UP_SLOPE * change_pct


def _catalog_identities() -> frozenset[str]:
    return frozenset(name for _group, name in seed_data.EXERCISE_TEMPLATES)


def lift_weight(identity: Optional[str], group: Optional[str]) -> float:
    """Вес упражнения в прогрессии: базовое движение каталога — 2, остальное — 1."""
    if identity and identity in _catalog_identities() and seed_data.movement_pattern(identity, group):
        return COMPOUND_WEIGHT
    return ISOLATION_WEIGHT


def metric_of(identity: Optional[str], bodyweight_load: Optional[str]) -> Optional[str]:
    """Чем меряется прогрессия упражнения: e1RM у упражнений с весом, лучшие
    повторы — у своего веса и без нагрузки, None — на время."""
    kind = seed_data.progression_kind(identity)
    if kind == "timed":
        return None
    # «Подтягивания с весом», «Брусья с весом»: повторы с разным довеском не
    # сравнить, а нагрузка подхода (вес тела + довесок, load_weight) есть —
    # меряем e1RM, как штангу.
    if seed_data.is_weighted_bodyweight(identity):
        return METRIC_E1RM
    if kind in ("bodyweight", "no_load") or seed_data.is_reps_only(identity, bodyweight_load):
        return METRIC_REPS
    return METRIC_E1RM


def is_weighted(identity: Optional[str], bodyweight_load: Optional[str]) -> bool:
    """Меряется ли упражнение по e1RM (вес есть и e1RM что-то значит)."""
    return metric_of(identity, bodyweight_load) == METRIC_E1RM


def first_weeks_from_rows(rows: Iterable[SetRow], start: dt.date) -> FirstWeeks:
    """Первые 14 дней каждого упражнения начиная со `start` — по строкам
    (после возвращения: база прогрессии — только то, что после перерыва)."""
    first: dict[int, dt.date] = {}
    metric: dict[int, Optional[str]] = {}
    rows = [r for r in rows if r.day >= start]
    for r in rows:
        if r.exercise_id not in first or r.day < first[r.exercise_id]:
            first[r.exercise_id] = r.day
        metric[r.exercise_id] = r.metric
    best: dict[int, float] = {}
    for r in rows:
        if r.day <= first[r.exercise_id] + dt.timedelta(days=FIRST_DAYS - 1):
            best[r.exercise_id] = max(best.get(r.exercise_id, 0.0), r.value)
    return {ex: (day, best.get(ex, 0.0), metric[ex]) for ex, day in first.items()}


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
        if r.metric is None or r.value <= 0 or r.day > d:
            continue
        if r.day >= cur_lo:
            current[r.exercise_id] = max(current.get(r.exercise_id, 0.0), r.value)
            meta[r.exercise_id] = r
        elif _in(r.day, base_lo, base_hi):
            base[r.exercise_id] = max(base.get(r.exercise_id, 0.0), r.value)
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
            lifts.append(LiftScore(ex, m.name, baseline, cur, change, lift_points(change), weight, m.metric))
        else:
            lifts.append(LiftScore(ex, m.name, None, cur, None, None, weight, m.metric))
    rated = sorted((lift for lift in lifts if lift.rated), key=lambda lift: (lift.points, lift.name))
    unrated = sorted((lift for lift in lifts if not lift.rated), key=lambda lift: lift.name)
    if not rated:
        return None, unrated
    mean = sum(lift.weight * lift.points for lift in rated) / sum(lift.weight for lift in rated)
    return POINTS["progression"] * mean, rated + unrated


def week_monday(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def blocks(d: dt.date) -> list[tuple[dt.date, dt.date]]:
    """Текущая календарная неделя D и четыре законченные перед ней
    (понедельник–воскресенье), свежая первой."""
    mon = week_monday(d)
    return [
        (mon - dt.timedelta(days=7 * k), mon - dt.timedelta(days=7 * k - 6)) for k in range(BLOCKS + 1)
    ]


def week_blocks(workout_days: list[dt.date], start: Optional[dt.date], d: dt.date) -> list[WeekBlock]:
    """Пять недель (текущая первой); тренировки текущей — только до D."""
    return [
        WeekBlock(
            lo, hi,
            bisect.bisect_right(workout_days, min(hi, d)) - bisect.bisect_left(workout_days, lo),
            counted=start is not None and hi >= start,
            current=k == 0,
        )
        for k, (lo, hi) in enumerate(blocks(d))
    ]


def week_options(weeks: list[WeekBlock]) -> list[list[WeekBlock]]:
    """Наборы недель на выбор: (текущая + три законченные) и, если в них есть
    неделя в счёте, (четыре законченные)."""
    options = [weeks[:BLOCKS]]
    if any(w.counted for w in weeks[1:]):
        options.append(weeks[1:BLOCKS + 1])
    return options


def regularity(weeks: list[WeekBlock]) -> float:
    counted = [w for w in weeks if w.counted] or weeks[:1]
    share = sum(min(w.workouts, TARGET_WORKOUTS) / TARGET_WORKOUTS for w in counted) / len(counted)
    return POINTS["regularity"] * share


def _counted_group(group: Optional[str]) -> bool:
    return bool(group) and group.strip().lower() not in formatting.VOLUME_HIDDEN_GROUPS


def deload_blocks(totals: list[int], tops: list[float]) -> list[bool]:
    """Какие недели — разгрузка: подходов меньше 60% медианы остальных и верхний
    e1RM ниже медианы их верхних. Пустая неделя — пропуск, а не разгрузка."""
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


def volume(
    rows: list[SetRow], weeks: list[WeekBlock], d: dt.date
) -> tuple[Optional[float], list[WeekBlock], list[GroupScore]]:
    """Объём (по неделям с отсчёта, без разгрузок), недели с пометкой разгрузки
    и разбор по группам. Группы — тренированные за GROUP_DAYS дней: брошенная
    идёт в счёт нулём. `rows` — уже без подходов до start_day и после D."""
    rows = [r for r in rows if _counted_group(r.group) and r.day <= d]
    groups = {r.group for r in rows if _in(r.day, d - dt.timedelta(days=GROUP_DAYS - 1), d)}
    recent = {r.group for r in rows if _in(r.day, d - dt.timedelta(days=NEGLECT_DAYS - 1), d)}
    mon = week_monday(d)
    this_week: dict[str, int] = {}
    for r in rows:
        if _in(r.day, mon, d):
            this_week[r.group] = this_week.get(r.group, 0) + 1
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
        WeekBlock(w.start, w.end, w.workouts, w.counted, i in deload, w.current) for i, w in enumerate(weeks)
    ]
    used = [i for i in idx if i not in deload] or idx
    shares: dict[str, float] = {}
    if used:
        for g in groups:
            shares[g] = sum(
                min(per_block[i].get(g, 0), VOLUME_TARGET_SETS) / VOLUME_TARGET_SETS for i in used
            ) / len(used)
    vol = POINTS["volume"] * sum(shares.values()) / len(shares) if shares else None
    order = {name: i for i, (name, *_rest) in enumerate(seed_data.MUSCLE_GROUP_PRESETS)}
    out = [
        GroupScore(
            g, this_week.get(g, 0), sum(b.get(g, 0) for b in per_block), shares.get(g), g not in recent,
            sets_per_week=sum(per_block[i].get(g, 0) for i in used) / len(used) if used else 0.0,
            week_sets=tuple(b.get(g, 0) for b in per_block),
        )
        for g in sorted(groups, key=lambda g: (order.get(g, 99), g))
    ]
    return vol, weeks, out


def progression_from(first_weeks: FirstWeeks, phase: Phase, d: dt.date) -> Optional[dt.date]:
    """Ближайший день, когда хоть одно оцениваемое упражнение получит базу: его
    первый подход с отсчёта + 28 дней (у новичка — не раньше 42 дней истории).
    None — такого дня впереди нет."""
    floor = None if phase.returned else phase.start_day + dt.timedelta(days=NEWCOMER_PROGRESSION_DAYS)
    out = []
    for first_day, _best, metric in first_weeks.values():
        if metric is None or first_day < phase.start_day:
            continue
        day = first_day + dt.timedelta(days=FALLBACK_MIN_AGE)
        if floor is not None:
            day = max(day, floor)
        if day > d:
            out.append(day)
    return min(out) if out else None


# --- день целиком ------------------------------------------------------------


def _lever(s: DayScore, unit: str) -> Optional[Lever]:
    """Рычаг дня; None — всё по плану (реплика-похвала)."""
    if s.status == "empty":
        return Lever("empty")
    if s.status == "paused":
        if s.last_score is None:
            return Lever("paused_no_score", {"days": s.pause_days})
        return Lever("paused", {"days": s.pause_days, "score": s.last_score})
    if s.status == "calibrating":
        if s.phase.returned:
            return Lever("comeback", {"date": s.ready_on})
        if s.this_week.workouts >= TARGET_WORKOUTS:
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
        change = weakest.change_pct or 0.0
        pct = formatting.format_weight(round(abs(change), 1))
        if weakest.by_reps:
            if abs(change) <= weakest.noise_pct:
                return Lever("lift_flat_reps", {"exercise": weakest.name})
            if change > 0:
                return Lever("lift_up_reps", {"exercise": weakest.name, "pct": pct})
            return Lever("lift_down_reps", {"exercise": weakest.name, "pct": pct})
        step = formatting.format_weight({"kg": 2.5, "lb": 5.0}.get(unit, 2.5))
        if abs(change) <= weakest.noise_pct:
            return Lever("lift_flat", {"exercise": weakest.name, "step": step})
        if change > 0:
            return Lever("lift_up", {"exercise": weakest.name, "pct": pct, "step": step})
        return Lever("lift_down", {"exercise": weakest.name, "pct": pct})
    if key == "regularity":
        done = s.this_week.workouts
        if done == 0:
            return Lever("regularity_none")
        if done < TARGET_WORKOUTS:
            return Lever("regularity_one")
        return Lever("regularity_keep")
    short = [g for g in s.groups if g.points is not None and g.sets_7d < VOLUME_TARGET_SETS]
    if not short:
        return Lever("volume_keep")
    g = min(short, key=lambda g: (g.sets_7d, not g.neglected))
    if g.neglected:
        return Lever("balance", {"group": g.group})
    need = VOLUME_TARGET_SETS - g.sets_7d
    if g.sets_7d == 0:
        return Lever("volume_zero", {"group": g.group, "need": need})
    return Lever("volume_more", {"group": g.group, "sets": g.sets_7d, "need": need})


def _blank(d: dt.date, status: str, workout_days: list[dt.date], phase: Optional[Phase]) -> DayScore:
    weeks = week_blocks(workout_days, None, d)
    return DayScore(
        d, status, None, [Part(k, None) for k in PART_KEYS],
        weeks=weeks[:BLOCKS], phase=phase, this_week=weeks[0],
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
    all_weeks = week_blocks(workout_days, start, d)
    best: Optional[tuple[float, float, Optional[float], list[WeekBlock], list[GroupScore]]] = None
    for option in week_options(all_weeks):
        reg = regularity(option)
        vol, weeks, groups = volume(window, option, d)
        total = reg + (vol or 0.0)
        if best is None or total > best[0] + 1e-9:
            best = (total, reg, vol, weeks, groups)
    _total, reg, vol, weeks, groups = best
    rated = [lift for lift in lifts if lift.rated]
    counted = [w for w in weeks if w.counted]
    parts = [
        Part("progression", prog, {"up": sum(1 for lift in rated if lift.change_pct > lift.noise_pct),
                                   "n": len(rated)}),
        Part("regularity", reg, {"full": sum(1 for w in counted if w.workouts >= TARGET_WORKOUTS),
                                 "half": sum(1 for w in counted if 0 < w.workouts < TARGET_WORKOUTS),
                                 "n": len(counted)}),
        Part("volume", vol, {"full": sum(1 for g in groups if g.points == 1.0),
                             "n": sum(1 for g in groups if g.points is not None),
                             "deload": bool(weeks[0].current and weeks[0].deload)}),
    ]
    s = DayScore(d, "ok", None, parts, lifts, groups, weeks, phase, this_week=all_weeks[0])
    if prog is None:
        s.progression_from = progression_from(first_weeks, phase, d)
    if history < s.calibration_days:
        s.status = "calibrating"
        return s
    s.score = total_score(parts)
    s.status = "ok" if all(p.points is not None for p in parts) else "partial"
    return s


def total_score(parts: list[Part]) -> float:
    """Сумма частей / сумма максимумов × 10. Прогрессии нет — она идёт
    баллом «без изменений», а не выпадает из знаменателя: иначе аккуратный
    новичок видел бы 10 и «падал», когда рост посчитан."""
    got = sum(p.points for p in parts if p.points is not None)
    top = sum(p.max for p in parts if p.points is not None)
    prog = next(p for p in parts if p.key == "progression")
    if prog.points is None:
        got += prog.max * NEUTRAL_LIFT_POINTS
        top += prog.max
    return round(got / top * 10, 1)


# --- ряд и отчёт ---------------------------------------------------------------


def history_days(d: dt.date, period_days: int) -> list[dt.date]:
    """Дни ряда, старые сначала: каждый седьмой назад от D на любом периоде
    (5 точек на 28 дней, 13 на 90, 53 на 365) — дневные точки только шум."""
    n = period_days // HISTORY_STEP + 1
    return [d - dt.timedelta(days=HISTORY_STEP * i) for i in range(n - 1, -1, -1)]


@dataclass
class ScoreReport:
    today: DayScore
    week_ago: DayScore
    history: list[DayScore]
    period_days: int
    #: Оценка точки ряда минус оценка за 7 дней до неё — по точке на каждую
    #: из `history`; у самой старой считается лишний день перед рядом.
    #: None — неделей раньше оценки не было (калибровка, до первой тренировки).
    history_deltas: list[Optional[float]] = field(default_factory=list)

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
    # Неделя перед рядом — для изменения у самой старой точки.
    first_day = history_days(d, period_days)[0] - dt.timedelta(days=HISTORY_STEP)
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
    # Ряд идёт шагом в 7 дней: неделя назад — предыдущая точка, а у самой
    # старой — один лишний день перед рядом.
    before = [at(history[0].day - dt.timedelta(days=HISTORY_STEP)), *history[:-1]]
    deltas = [
        None if h.score is None or b.score is None else round(h.score - b.score, 1)
        for h, b in zip(history, before, strict=True)
    ]
    return ScoreReport(today, week_ago, history, period_days, deltas)


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
            metric=metric_of(r["original_name"], r["bodyweight_load"]),
            reps=int(r["reps"] or 0),
        )
        for r in raw
    ]


def first_weeks_from_db(raw: Iterable[Any]) -> FirstWeeks:
    out: FirstWeeks = {}
    for r in raw:
        metric = metric_of(r["original_name"], r["bodyweight_load"])
        best = r["best_reps"] if metric == METRIC_REPS else r["best"]
        out[r["ex"]] = (dt.date.fromisoformat(r["first_day"]), float(best or 0), metric)
    return out


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
