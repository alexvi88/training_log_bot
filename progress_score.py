"""Оценка прогресса по неделям — балл 0–10 у каждой недели.

Зачем. «+20.7% e1RM за 4 недели» отвечает на один вопрос — «растут ли веса»,
и у опытного атлета, который честно прибавляет свой процент в месяц, выглядит
провалом рядом с новичком. Оценка отвечает на другой: **хорошо ли ты
прогрессируешь для своего уровня** — и складывает силу с тем, без чего роста не
бывает: регулярностью, прогрессией нагрузки и объёмом.

Четыре части, баллы складываются (максимумы — в POINTS):

1. **Сила (4.5).** Для каждого упражнения — устойчивый наклон лучшего e1RM
   тренировки за STRENGTH_WINDOW_WEEKS недель (Тейл–Сен: медиана наклонов по
   парам точек, одна случайная тяжёлая или лёгкая тренировка его не задирает),
   в процентах за месяц. Делится на НОРМУ роста для стажа атлета
   (`expected_monthly_pct`: новичок ~6%/мес, через год ~2%, через три — 1%):
   опытный с +1% получает столько же, сколько новичок с +6%. Мало точек —
   отношение тянется к нейтральному PRIOR_RATIO (доверие n/(n+3)), базовые
   движения весят вдвое больше изоляции. Упражнения своим весом идут
   повторами (1 + r/30 — тот же Эпли с весом 1).
2. **Регулярность (2.5).** Тренировки недели против своего обычного темпа
   (среднее за USUAL_WEEKS недель до неё), плюс доля недель с тренировкой за
   последние четыре. Дыра длиннее GAP_DAYS дней — минус.
3. **Прогрессия нагрузки (1.5).** Доля упражнений, в которых за последние
   четыре недели был новый лучший e1RM против восьми недель до них — то есть
   прибавили вес или повторы. Без этого силы не будет и дальше.
4. **Объём (1.5).** Рабочие подходы недели на группу мышц: VOLUME_FULL_MIN…
   VOLUME_FULL_MAX — полный балл, меньше — пропорционально, сильно больше —
   убывает. Группы — те, что атлет тренирует, плюс seed_data.CORE_VOLUME_GROUPS всегда:
   пропущенные ноги — это видно.

Разгрузка (подходов недели не больше DELOAD_SETS_SHARE от медианы четырёх
прошлых при обычном числе тренировок) — не провал: прогрессия и объём в такую
неделю засчитываются полностью, неделя помечена `deload`.

Оценки нет (`score is None`), когда данных мало: меньше MIN_SESSIONS_4W
тренировок за четыре недели или ни одного упражнения с силовым наклоном.
Неделя без тренировок при живой истории — оценка есть, и она низкая: подколка
про пропуск — единственная, которую разрешает тон.

Текущая неделя — `provisional`: регулярность считается от уже прошедших дней,
окончательная оценка — в понедельник.

Всё здесь — чистые функции над строками `ScoredSet`: формула проверяется
таблицей случаев без базы, а `collect` только достаёт строки. Текстов модуль
не держит — подписи собирает api_v1_progress_score через i18n.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import analytics
import formatting
import seed_data

POINTS = {"strength": 4.5, "consistency": 2.5, "overload": 1.5, "volume": 1.5}

STRENGTH_WINDOW_WEEKS = 8
STRENGTH_MIN_POINTS = 3
STRENGTH_MIN_SPAN_DAYS = 14
# Отношение «факт / норма» ограничено: одно упражнение с +40% не тащит балл.
RATIO_MIN = -1.5
RATIO_MAX = 1.5
# Куда тянется отношение при малом числе точек — «половина нормы»: новичок с
# тремя тренировками не получает ни ноль, ни максимум.
PRIOR_RATIO = 0.5
PRIOR_WEIGHT = 3
# Отношение → доля баллов силы: −0.5 нормы и хуже — ноль, 1.25 нормы — максимум.
STRENGTH_RATIO_FLOOR = -0.5
STRENGTH_RATIO_FULL = 1.25
BASE_LIFT_WEIGHT = 2.0
# Точка дальше этой доли от медианы соседей — ошибка ввода, не прогресс.
OUTLIER_SHARE = 0.25

# Норма роста e1RM в процентах за месяц по стажу (в месяцах): 1% + 5%·e^(−t/8).
EXPECTED_FLOOR_PCT = 1.0
EXPECTED_NOVICE_EXTRA_PCT = 5.0
EXPECTED_DECAY_MONTHS = 8.0
DAYS_PER_MONTH = 30.44

USUAL_WEEKS = 8
USUAL_MIN_WEEKS = 3
NEWBIE_USUAL = 2.0
GAP_DAYS = 9
GAP_PENALTY = 0.3

OVERLOAD_RECENT_WEEKS = 4
OVERLOAD_BASE_WEEKS = 8
OVERLOAD_MIN_SESSIONS = 2
# Новый лучший должен быть заметным: +0.5% e1RM — шум записи, не прибавка.
OVERLOAD_MIN_GAIN = 0.005

VOLUME_FULL_MIN = 6
VOLUME_FULL_MAX = 16
VOLUME_ZERO_AT = 40

DELOAD_SETS_SHARE = 0.6

# Недостача меньше этого — не о чем подсказывать.
LEVER_MIN_POINTS = 0.3

MIN_SESSIONS_4W = 4

TIERS = ((4.0, "warmup"), (6.0, "in_game"), (8.0, "in_shape"), (9.5, "machine"), (10.01, "legend"))


@dataclass(frozen=True)
class ScoredSet:
    workout_id: int
    day: dt.date
    exercise_id: int
    name: str
    identity: Optional[str]
    group: Optional[str]
    load: float
    weight: float
    reps: int


@dataclass
class Part:
    key: str
    share: float  # 0..1

    @property
    def points(self) -> float:
        return round(POINTS[self.key] * self.share, 1)

    @property
    def max(self) -> float:
        return POINTS[self.key]


@dataclass
class Lever:
    """Что сильнее всего поднимет балл — один шаг, с числами для текста."""

    kind: str  # "sessions" | "volume" | "overload" | "strength" | "comeback"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class WeekScore:
    week_start: dt.date
    score: Optional[float]
    provisional: bool
    deload: bool = False
    sessions: int = 0
    parts: list[Part] = field(default_factory=list)
    lever: Optional[Lever] = None
    # Лучшее и худшее упражнение по силе — для разбора недели.
    best_lift: Optional[tuple[str, float]] = None  # (имя, % в месяц)
    weak_lift: Optional[tuple[str, float]] = None

    @property
    def tier(self) -> Optional[str]:
        return tier_for(self.score)


def tier_for(score: Optional[float]) -> Optional[str]:
    if score is None:
        return None
    for bound, key in TIERS:
        if score < bound:
            return key
    return TIERS[-1][1]


def week_monday(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def expected_monthly_pct(training_months: float) -> float:
    """Нормальный рост e1RM за месяц на этом стаже, в процентах."""
    t = max(training_months, 0.0)
    return EXPECTED_FLOOR_PCT + EXPECTED_NOVICE_EXTRA_PCT * math.exp(-t / EXPECTED_DECAY_MONTHS)


def theil_sen_slope(points: list[tuple[float, float]]) -> Optional[float]:
    """Медиана наклонов по всем парам точек с разным x. None — пар нет."""
    slopes = [
        (y2 - y1) / (x2 - x1)
        for i, (x1, y1) in enumerate(points)
        for x2, y2 in points[i + 1:]
        if x2 != x1
    ]
    return statistics.median(slopes) if slopes else None


def _drop_outliers(values: list[tuple[dt.date, float]]) -> list[tuple[dt.date, float]]:
    """Убрать точки, которые дальше OUTLIER_SHARE от медианы двух соседей с
    каждой стороны (ошибка ввода: «800» вместо «80»)."""
    if len(values) < 4:
        return values
    out = []
    for i, (d, v) in enumerate(values):
        around = [x for _, x in values[max(0, i - 2):i] + values[i + 1:i + 3]]
        ref = statistics.median(around)
        if ref > 0 and abs(v - ref) / ref > OUTLIER_SHARE * 2:
            continue
        out.append((d, v))
    return out


def _sessions_by_exercise(rows: Iterable[ScoredSet]) -> dict[int, list[analytics.SessionStats]]:
    by_ex: dict[int, dict[int, analytics.SessionStats]] = {}
    for r in rows:
        sessions = by_ex.setdefault(r.exercise_id, {})
        s = sessions.get(r.workout_id)
        if s is None:
            s = sessions[r.workout_id] = analytics.SessionStats(r.workout_id, r.day.isoformat(), [])
        # Режим «своим весом» — по записанному весу, как у экрана прогресса:
        # подтягивания «×14» со снимком веса тела — это повторы, а не e1RM.
        s.sets.append(analytics.SetRow(r.load if r.weight else 0.0, r.reps, r.workout_id, s.started_at))
    return {
        ex: sorted(ss.values(), key=lambda s: s.started_at) for ex, ss in by_ex.items()
    }


def session_value(session: analytics.SessionStats) -> Optional[float]:
    """Точка силы тренировки: лучший e1RM без RPE, а своим весом — 1 + r/30."""
    if session.is_bodyweight_mode:
        reps = session.max_reps_in_set
        return 1 + reps / 30 if reps > 0 else None
    return analytics.trend_e1rm(session)


def _day(s: analytics.SessionStats) -> dt.date:
    return dt.date.fromisoformat(s.started_at[:10])


def lift_monthly_pct(sessions: list[analytics.SessionStats], end: dt.date) -> Optional[tuple[float, int]]:
    """(рост в % за месяц, число точек) по окну, кончающемуся `end`; None —
    точек мало или окно короткое."""
    start = end - dt.timedelta(weeks=STRENGTH_WINDOW_WEEKS) + dt.timedelta(days=1)
    in_window = [s for s in sessions if start <= _day(s) <= end]
    light = {id(s) for s in analytics.light_sessions(in_window)}
    points = [
        (_day(s), v) for s in in_window if id(s) not in light and (v := session_value(s)) is not None
    ]
    points = _drop_outliers(points)
    if len(points) < STRENGTH_MIN_POINTS:
        return None
    if (points[-1][0] - points[0][0]).days < STRENGTH_MIN_SPAN_DAYS:
        return None
    t0 = points[0][0]
    slope = theil_sen_slope([((d - t0).days, v) for d, v in points])
    base = statistics.median(v for _, v in points)
    if slope is None or base <= 0:
        return None
    return slope * DAYS_PER_MONTH / base * 100, len(points)


def _is_base_lift(identity: Optional[str], name: str, group: Optional[str]) -> bool:
    if seed_data.main_lift_identity(identity) is not None:
        return True
    return seed_data.movement_pattern(identity or name, group) is not None


def strength_share(
    by_ex: dict[int, list[analytics.SessionStats]],
    meta: dict[int, ScoredSet],
    end: dt.date,
    expected_pct: float,
) -> tuple[Optional[float], Optional[tuple[str, float]], Optional[tuple[str, float]]]:
    """Доля баллов силы 0..1 (None — нет ни одного упражнения с наклоном) и
    лучшее / худшее упражнение (имя, % в месяц)."""
    total_w = 0.0
    acc = 0.0
    lifts: list[tuple[str, float]] = []
    for ex, sessions in by_ex.items():
        got = lift_monthly_pct(sessions, end)
        if got is None:
            continue
        pct, n = got
        ratio = max(RATIO_MIN, min(RATIO_MAX, pct / expected_pct))
        trust = n / (n + PRIOR_WEIGHT)
        ratio = trust * ratio + (1 - trust) * PRIOR_RATIO
        m = meta[ex]
        weight = (BASE_LIFT_WEIGHT if _is_base_lift(m.identity, m.name, m.group) else 1.0) * math.sqrt(n)
        acc += ratio * weight
        total_w += weight
        lifts.append((m.name, round(pct, 1)))
    if total_w == 0:
        return None, None, None
    r = acc / total_w
    share = (r - STRENGTH_RATIO_FLOOR) / (STRENGTH_RATIO_FULL - STRENGTH_RATIO_FLOOR)
    lifts.sort(key=lambda x: x[1])
    best = lifts[-1] if lifts[-1][1] > 0 else None
    weak = lifts[0] if len(lifts) > 1 and lifts[0][1] <= 0 else None
    return max(0.0, min(1.0, share)), best, weak


def _usual_sessions(workout_days: list[dt.date], week_start: dt.date, first_day: dt.date) -> float:
    """Обычный темп — тренировок в неделю за USUAL_WEEKS недель до этой, но не
    раньше недели первой тренировки; истории меньше USUAL_MIN_WEEKS недель —
    NEWBIE_USUAL."""
    first_week = week_monday(first_day)
    weeks = min(USUAL_WEEKS, (week_start - first_week).days // 7)
    if weeks < USUAL_MIN_WEEKS:
        return NEWBIE_USUAL
    since = week_start - dt.timedelta(weeks=weeks)
    n = sum(1 for d in workout_days if since <= d < week_start)
    return max(1.0, n / weeks)


def consistency_share(
    workout_days: list[dt.date], week_start: dt.date, today: dt.date, first_day: dt.date
) -> tuple[float, int, float]:
    """(доля 0..1, тренировок за неделю, обычный темп)."""
    week_end = week_start + dt.timedelta(days=6)
    upto = min(week_end, today)
    done = sum(1 for d in workout_days if week_start <= d <= upto)
    usual = _usual_sessions(workout_days, week_start, first_day)
    elapsed = (upto - week_start).days + 1
    # Текущая неделя: от прошедших дней. Тренировка, сделанная в понедельник,
    # уже весит полную «норму» понедельника.
    expected = usual * (elapsed / 7) if upto < week_end else usual
    week_part = min(1.0, done / expected) if expected > 0 else 1.0
    four = [week_start - dt.timedelta(weeks=i) for i in range(4)]
    four = [w for w in four if w >= week_monday(first_day)]
    active = sum(1 for w in four if any(w <= d <= w + dt.timedelta(days=6) for d in workout_days))
    four_part = active / len(four) if four else 1.0
    share = 0.7 * week_part + 0.3 * four_part
    # Дыра: самый длинный промежуток без тренировок, который кончается в эту неделю.
    days = sorted({d for d in workout_days if d <= upto})
    prev = [d for d in days if d < week_start]
    in_week = [d for d in days if week_start <= d <= upto]
    gap = 0
    if prev and in_week:
        gap = (in_week[0] - prev[-1]).days
    elif prev and not in_week:
        gap = (upto - prev[-1]).days
    if gap > GAP_DAYS:
        share -= GAP_PENALTY
    return max(0.0, min(1.0, share)), done, usual


def overload_share(by_ex: dict[int, list[analytics.SessionStats]], end: dt.date) -> tuple[Optional[float], list[int]]:
    """Доля упражнений с новым лучшим за последние четыре недели против восьми
    до них; None — сравнивать не с чем. Вторым — упражнения без прибавки."""
    recent_start = end - dt.timedelta(weeks=OVERLOAD_RECENT_WEEKS) + dt.timedelta(days=1)
    base_start = recent_start - dt.timedelta(weeks=OVERLOAD_BASE_WEEKS)
    eligible = 0
    progressed = 0
    stuck: list[int] = []
    for ex, sessions in by_ex.items():
        recent = [v for s in sessions if recent_start <= _day(s) <= end and (v := session_value(s))]
        base = [v for s in sessions if base_start <= _day(s) < recent_start and (v := session_value(s))]
        if len(recent) < OVERLOAD_MIN_SESSIONS or not base:
            continue
        eligible += 1
        if max(recent) > max(base) * (1 + OVERLOAD_MIN_GAIN):
            progressed += 1
        else:
            stuck.append(ex)
    if eligible == 0:
        return None, []
    return progressed / eligible, stuck


def _group_volume_share(sets: int) -> float:
    if sets <= 0:
        return 0.0
    if sets < VOLUME_FULL_MIN:
        return sets / VOLUME_FULL_MIN
    if sets <= VOLUME_FULL_MAX:
        return 1.0
    if sets >= VOLUME_ZERO_AT:
        return 0.0
    return 1 - (sets - VOLUME_FULL_MAX) / (VOLUME_ZERO_AT - VOLUME_FULL_MAX)


def volume_share(
    rows: list[ScoredSet], week_start: dt.date, today: dt.date
) -> tuple[float, Optional[tuple[str, int]]]:
    """(доля 0..1, самая отстающая группа и её подходы)."""
    week_end = min(week_start + dt.timedelta(days=6), today)
    since = week_start - dt.timedelta(weeks=USUAL_WEEKS)
    trained = {
        r.group for r in rows
        if r.group and since <= r.day <= week_end
        and r.group.strip().lower() not in formatting.VOLUME_HIDDEN_GROUPS
    }
    canon = {g.strip().lower(): g for g in seed_data.CORE_VOLUME_GROUPS}
    canon.update({g.strip().lower(): g for g in sorted(trained)})
    counts: dict[str, int] = {k: 0 for k in canon}
    for r in rows:
        if r.group and week_start <= r.day <= week_end:
            k = r.group.strip().lower()
            if k in counts:
                counts[k] += 1
    elapsed = (week_end - week_start).days + 1
    scale = 7 / elapsed  # текущая неделя — темп на полную неделю
    shares = {k: _group_volume_share(round(v * scale)) for k, v in counts.items()}
    share = sum(shares.values()) / len(shares)
    worst_key = min(counts, key=lambda k: (shares[k], counts[k], k))
    worst = (canon[worst_key], counts[worst_key]) if shares[worst_key] < 1 else None
    return share, worst


def is_deload(rows: list[ScoredSet], week_start: dt.date, sessions: int, usual: float) -> bool:
    week_sets = sum(1 for r in rows if week_start <= r.day <= week_start + dt.timedelta(days=6))
    prior = []
    for i in range(1, 5):
        w = week_start - dt.timedelta(weeks=i)
        prior.append(sum(1 for r in rows if w <= r.day <= w + dt.timedelta(days=6)))
    prior = [p for p in prior if p > 0]
    if len(prior) < 3 or sessions == 0:
        return False
    return week_sets <= DELOAD_SETS_SHARE * statistics.median(prior) and sessions >= usual - 1


def _lever(score: WeekScore, done: int, usual: float, worst_group, stuck_names: list[str]) -> Optional[Lever]:
    """Шаг к баллу повыше, который можно сделать на этой же неделе: из
    регулярности, объёма и прогрессии — та, где недостача баллов больше. Сила —
    только когда остальное почти в порядке: «расти в силе» не действие, а
    итог трёх других."""
    if score.sessions == 0:
        return Lever("comeback")
    missing = {p.key: p.max - p.points for p in score.parts}
    doable = [k for k in ("consistency", "volume", "overload") if k in missing]
    key = max(doable, key=lambda k: missing[k])
    if missing[key] < LEVER_MIN_POINTS:
        key = "strength"
        if missing.get(key, 0) < LEVER_MIN_POINTS:
            return None
    if key == "consistency":
        need = max(1, math.ceil(usual) - done)
        return Lever("sessions", {"n": need})
    if key == "volume" and worst_group:
        group, sets = worst_group
        if sets > VOLUME_FULL_MAX:
            return Lever("volume_high", {"group": group, "n": sets, "max": VOLUME_FULL_MAX})
        return Lever("volume", {"group": group, "n": max(1, VOLUME_FULL_MIN - sets)})
    if key == "overload" and stuck_names:
        return Lever("overload", {"exercise": stuck_names[0]})
    return Lever("strength")


def score_weeks(
    rows: list[ScoredSet], today: dt.date, *, weeks: int = 12, first_day: Optional[dt.date] = None
) -> list[WeekScore]:
    """Оценки последних `weeks` недель, старые сначала; недели раньше первой
    тренировки не возвращаются. `rows` — подходы хотя бы за
    weeks + STRENGTH_WINDOW_WEEKS + OVERLOAD_BASE_WEEKS недель до `today`."""
    if not rows:
        return []
    first = first_day or min(r.day for r in rows)
    workout_day: dict[int, dt.date] = {}
    for r in rows:
        workout_day[r.workout_id] = r.day
    workout_days = sorted(workout_day.values())
    meta: dict[int, ScoredSet] = {}
    for r in rows:
        meta[r.exercise_id] = r
    by_ex = _sessions_by_exercise(rows)
    this_week = week_monday(today)
    out = []
    for i in range(weeks - 1, -1, -1):
        ws = this_week - dt.timedelta(weeks=i)
        if ws + dt.timedelta(days=6) < first:
            continue
        end = min(ws + dt.timedelta(days=6), today)
        provisional = ws == this_week
        sessions_4w = sum(1 for d in workout_days if end - dt.timedelta(days=27) <= d <= end)
        consistency, done, usual = consistency_share(workout_days, ws, today, first)
        months = (end - first).days / DAYS_PER_MONTH
        strength, best, weak = strength_share(by_ex, meta, end, expected_monthly_pct(months))
        ws_score = WeekScore(ws, None, provisional, sessions=done)
        if sessions_4w < MIN_SESSIONS_4W or strength is None:
            out.append(ws_score)
            continue
        overload, stuck = overload_share(by_ex, end)
        volume, worst_group = volume_share(rows, ws, today)
        deload = not provisional and is_deload(rows, ws, done, usual)
        if overload is None:
            overload = strength  # сравнивать не с чем — судим по силе
        if deload:
            overload = volume = 1.0
        parts = [
            Part("strength", strength),
            Part("consistency", consistency),
            Part("overload", overload),
            Part("volume", volume),
        ]
        ws_score.parts = parts
        ws_score.deload = deload
        ws_score.score = round(min(10.0, sum(p.points for p in parts)), 1)
        ws_score.best_lift = best
        ws_score.weak_lift = weak
        stuck_names = [meta[ex].name for ex in stuck]
        ws_score.lever = _lever(ws_score, done, usual, worst_group, stuck_names)
        out.append(ws_score)
    return out


def rows_from_db(raw: Iterable[Any]) -> list[ScoredSet]:
    return [
        ScoredSet(
            workout_id=r["workout_id"],
            day=dt.date.fromisoformat(r["day"]),
            exercise_id=r["exercise_id"],
            name=r["display_name"],
            identity=r["original_name"],
            group=r["group_name"],
            load=float(r["load"] or 0),
            weight=float(r["weight"] or 0),
            reps=int(r["reps"]),
        )
        for r in raw
    ]


HISTORY_WEEKS = STRENGTH_WINDOW_WEEKS + OVERLOAD_BASE_WEEKS + OVERLOAD_RECENT_WEEKS


async def collect(user_id: int, user: Any, today: dt.date, weeks: int = 12) -> list[WeekScore]:
    import db
    import timeutil

    tz = timeutil.offset_hours(user)
    dates = await db.list_finished_workout_dates(user_id, tz_offset=tz)
    if not dates:
        return []
    first_day = dt.date.fromisoformat(dates[0])
    start = week_monday(today) - dt.timedelta(weeks=weeks + HISTORY_WEEKS)
    raw = await db.list_scored_sets(user_id, start.isoformat(), today.isoformat(), tz_offset=tz)
    return score_weeks(rows_from_db(raw), today, weeks=weeks, first_day=first_day)
