"""Pure-Python metrics: e1RM, tonnage, trend regression, PR detection.

Operates on plain dicts/rows of working sets, so it stays decoupled from the
DB layer and is trivially testable.
"""

import datetime as dt
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import config
import i18n

# RPE говорит, сколько повторов осталось в запасе: @9 — ещё один, @8 — два.
# Подход «110×8 @9» по усилию — это подход на 9 повторов, и считать его наравне
# с отказным 110×8 значит терять ровно ту разницу, ради которой RPE и пишется.
# Поэтому в формулу уходят не записанные повторы, а повторы до отказа
# (effective_reps). Где RPE не проставлен — считается как раньше, по записанным.
#
# Потолок на запас: парсер пускает любой RPE из (0, 10], а @2 на разминке дал бы
# +8 повторов и e1RM выше рабочего максимума. Дальше пятёрки оценка «сколько я
# ещё смог бы» перестаёт быть оценкой, так что запас режется здесь.
RIR_CAP = 5.0


def reps_in_reserve(rpe: Optional[float]) -> float:
    if rpe is None:
        return 0.0
    return min(max(10.0 - rpe, 0.0), RIR_CAP)


def effective_reps(reps: int, rpe: Optional[float] = None) -> float:
    """Повторы до отказа: записанные плюс запас из RPE."""
    return reps + reps_in_reserve(rpe)


def epley_e1rm(weight: float, reps: int, rpe: Optional[float] = None) -> float:
    eff = effective_reps(reps, rpe)
    if eff <= 1:
        return weight
    return weight * (1 + eff / 30)


# Brzycki is only meaningful in the low-rep range it was fitted on. Its
# denominator (37 - reps) collapses as reps climb: at 30 reps it already returns
# 5x the weight, at 36 reps it returns 36x, and the old `reps >= 37` guard sat
# just past the peak — so a burnout set like 20kg x 36 produced a 720kg e1RM that
# became the exercise's permanent record. Epley stays monotone and gentle at any
# rep count, and the two formulas agree *exactly* at 10 reps
# (1 + 10/30 == 36/27 == 4/3), so handing over there is seamless rather than a step.
BRZYCKI_MAX_REPS = 10


def brzycki_e1rm(weight: float, reps: int, rpe: Optional[float] = None) -> float:
    eff = effective_reps(reps, rpe)
    if eff <= 1:
        return weight
    if eff > BRZYCKI_MAX_REPS:
        return epley_e1rm(weight, reps, rpe)
    return weight * 36 / (37 - eff)


def counts_for_e1rm(reps: int) -> bool:
    """Годится ли подход для расчётного максимума: не больше
    config.E1RM_MAX_REPS повторов. Формулы подогнаны под малые повторы, и
    5×55 давал «e1RM 14кг» с гантелей на 5кг — рекорд из шума."""
    return reps <= config.E1RM_MAX_REPS


# Изменение e1RM меньше этого — шум, а не прогресс или спад: округление
# блинов, полповтора RPE, другая формула на соседней тренировке. Порог — в
# килограммах и в процентах от исходного, срабатывает любой из двух.
E1RM_NOISE_KG = 1.0
E1RM_NOISE_SHARE = 0.02


def e1rm_change_is_noise(first: float, last: float, unit: str = "kg") -> bool:
    """«↓0.1кг с первой тренировки» — не спад: такая разница показывается
    ровной линией, а не красной стрелкой вниз (бот и /v1 одинаково; зовут
    только для падения — рост показывается как есть)."""
    delta = abs(last - first)
    kg_threshold = E1RM_NOISE_KG * (config.LB_PER_KG if unit == "lb" else 1.0)
    return delta < kg_threshold or (first > 0 and delta < first * E1RM_NOISE_SHARE)


def e1rm(
    weight: float, reps: int, formula: str = "epley", rpe: Optional[float] = None
) -> float:
    """Расчётный максимум подхода. 0 — у подхода больше config.E1RM_MAX_REPS
    повторов: такой подход в e1RM не участвует (ни в рекордах, ни в графике,
    ни в подсказке «цель»), см. counts_for_e1rm."""
    if not counts_for_e1rm(reps):
        return 0.0
    if formula == "brzycki":
        return brzycki_e1rm(weight, reps, rpe)
    return epley_e1rm(weight, reps, rpe)


@dataclass
class SetRow:
    weight: float
    reps: int
    workout_id: Optional[int] = None
    started_at: Optional[str] = None
    rpe: Optional[float] = None  # запас повторов для e1RM (см. effective_reps)


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
        # Подходы длиннее E1RM_MAX_REPS дают e1RM 0 — среди них лучший тот,
        # что тяжелее, а не первый попавшийся.
        return max(self.sets, key=lambda s: (e1rm(s.weight, s.reps, self.formula, s.rpe), s.weight, s.reps))

    @property
    def top_e1rm(self) -> float:
        ts = self.top_set
        if ts is None:
            return 0.0
        return e1rm(ts.weight, ts.reps, self.formula, ts.rpe)

    @property
    def top_e1rm_ignoring_rpe(self) -> float:
        """Лучший e1RM сессии по ЗАПИСАННЫМ повторам, без запаса из RPE.

        Нужен там, где смотрят на НАКЛОН по нескольким сессиям. RPE —
        самооценка с шагом в полбалла, и на одинаковой работе она гуляет: четыре
        сессии 140×5 с RPE 9.5 / 10 / 9.5 / 10 дают пилу ±1.2 кг, а метод
        наименьших квадратов на такой пиле выдаёт −0.5 кг/нед — «регрессируешь»
        на неизменных числах. Для
        вердикта у RPE свой канал (StallVerdict.avg_top_rpe), и наклону он не
        нужен. В показе, рекордах и сравнении двух сессий остаётся top_e1rm.
        """
        if not self.sets:
            return 0.0
        return max(e1rm(s.weight, s.reps, self.formula) for s in self.sets)

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
    """One rung of the rank ladder.

    `level` — идентификатор ступени, лежит в БД (`users.rank_level_seen`,
    db.py) и не должен меняться никогда. `name` — то, что читает человек,
    поэтому это НЕ поле, а свойство: RANKS собирается один раз на импорт
    модуля, а язык пользователя известен только в момент рендера (см.
    i18n.current_lang) — обычное строковое поле застыло бы на языке того, кто
    первым дёрнул модуль после старта процесса, для всех пользователей сразу.
    """
    level: int
    emoji: str
    min_workouts: int
    min_tonnage_kg: float
    min_per_week: float

    @property
    def name(self) -> str:
        return i18n.t(f"rank.{self.level}.name")


# Линейная лестница: звание растёт и от накопленного (тренировки, тоннаж), и от
# того, ходишь ли ты сейчас. Обе оси обязательны — тоннаж без регулярности это
# прошлые заслуги, а регулярность без объёма это разминка.
#
# Названия описывают этап, а не место в иерархии: «Втянулся» — про то, что
# человек уже ходит, а не про то, что он пока хуже остальных. Ни одно не
# подкалывает того, кто стоит ниже: тренер из подвала подкалывает только за
# пропуски (TONE_OF_VOICE), а звание висит на сводке каждый день — насмешка в
# нём работала бы постоянно. Соседние ступени должны ещё и различаться на слух:
# «Новичок» и «Новобранец» читались как одно и то же звание.
#
# Порог по числу тренировок — это календарь, а не усилие: при трёх тренировках в
# неделю (верхнее требование лестницы) 400 тренировок — это два с половиной года,
# и последние две ступени переставали быть целью. Ступени расставлены так, чтобы
# на честном темпе 3/нед лестница проходилась за ~2 года, а не за пять: месяц,
# три месяца, полгода, год, два. Тоннаж не трогаем — он и так почти никогда не
# самая слабая ось, ограничивают тренировки и частота.
# Названия — в locales/*.json по ключам rank.<level>.name.
RANKS: list[Rank] = [
    Rank(0, "🚪", 0, 0, 0.0),
    Rank(1, "🧱", 5, 5_000, 0.5),
    Rank(2, "🔩", 15, 25_000, 1.0),
    Rank(3, "⚙️", 40, 75_000, 1.5),
    Rank(4, "🪨", 80, 200_000, 2.0),
    Rank(5, "🦾", 160, 500_000, 2.5),
    Rank(6, "👑", 300, 1_000_000, 3.0),
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


@dataclass(frozen=True)
class RankGap:
    """Чего не хватает до следующего звания: ось и её недостача.

    Числа, а не готовая строка: фразу собирает formatting — «ещё 12 тренировок»
    требует русского согласования (plural_ru), которое живёт там же, где весь
    остальной текст.

    `axis` — "workouts" (не хватает тренировок, штук), "tonnage" (не хватает
    килограммов) или "frequency" (нужный темп, трен./нед — не недостача, а
    планка: её держат, а не добирают).
    """
    axis: str
    value: float


def rank_gap(
    current: Rank, total_workouts: int, tonnage_kg: float, per_week: float
) -> Optional[RankGap]:
    """Самая отстающая ось до следующего звания.

    Одна причина, а не список: «не хватает 12 тренировок» — это цель, а три
    строки с недостачами по всем осям читаются как отказ. «Самая отстающая» —
    по доле от порога, чтобы 40 тренировок из 50 не выигрывали у 5 тонн из 75.
    """
    nxt = next_rank(current)
    if nxt is None:
        return None
    gaps: list[tuple[float, RankGap]] = []
    if total_workouts < nxt.min_workouts:
        missing = nxt.min_workouts - total_workouts
        gaps.append((missing / max(nxt.min_workouts, 1), RankGap("workouts", missing)))
    if tonnage_kg < nxt.min_tonnage_kg:
        missing_kg = nxt.min_tonnage_kg - tonnage_kg
        gaps.append((missing_kg / max(nxt.min_tonnage_kg, 1), RankGap("tonnage", missing_kg)))
    if per_week < nxt.min_per_week:
        gaps.append((
            (nxt.min_per_week - per_week) / max(nxt.min_per_week, 0.1),
            RankGap("frequency", nxt.min_per_week),
        ))
    if not gaps:
        return None
    # По ключу, а не по кортежу целиком: при равных долях сравнение уезжало на
    # второй элемент, и ось выбиралась по алфавиту.
    return max(gaps, key=lambda g: g[0])[1]


def rank_progress(
    current: Rank, gap: Optional[RankGap], total_workouts: int, tonnage_kg: float, per_week: float
) -> Optional[float]:
    """Доля пути от текущего звания до следующего — для полоски под званием.

    По той же оси, что назвал `gap` («ещё 17 тренировок»): полоска и фраза
    рядом с ней обязаны говорить про одно и то же, иначе почти полная полоска
    над «ещё 17 тренировок» читается как ошибка. Путь меряется от порога
    текущего звания, а не от нуля: на «Станке» с 40 тренировками из 80 нужных
    полоска с нуля стояла бы на половине в первый же день на ступени.
    Нет следующего звания или нет недостачи — None, полоски нет.
    """
    nxt = next_rank(current)
    if nxt is None or gap is None:
        return None
    if gap.axis == "workouts":
        start, end, value = current.min_workouts, nxt.min_workouts, total_workouts
    elif gap.axis == "tonnage":
        start, end, value = current.min_tonnage_kg, nxt.min_tonnage_kg, tonnage_kg
    else:
        start, end, value = current.min_per_week, nxt.min_per_week, per_week
    if end <= start:
        return None
    return min(max((value - start) / (end - start), 0.0), 1.0)


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
            score = e1rm(row.weight, row.reps, formula, row.rpe)
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
            val = e1rm(s.weight, s.reps, session.formula, s.rpe)
            if val > pr.max_e1rm:
                pr.max_e1rm = val
                pr.best_e1rm_weight = s.weight
                pr.best_e1rm_reps = s.reps
            if s.reps > pr.max_reps_at_weight.get(s.weight, 0):
                pr.max_reps_at_weight[s.weight] = s.reps
    return pr


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
# (matches the AI trainer's methodology: 5-12 reps, double progression).
#
# The top of the range is where double progression flips from "add a rep" to
# "add weight" (see suggest_progression), so 12 is a push, not a ceiling to
# settle at: hit it and the next session's hint asks for the heavier bar.
REP_RANGE_MIN = 5
REP_RANGE_MAX = 12

# Диапазон по умолчанию — личная настройка атлета («🔢 Диапазон повторов» в
# настройках бота и приложения; users.rep_range_min/max, NULL = 5–12). Только
# эти пять пар и больше никаких: каждый пресет — осмысленная двойная
# прогрессия под свою цель, а произвольные «7–9» превращали бы подсказку в
# угадайку. Схема программы («3×6–12») и правило прогрессии по-прежнему
# старше — настройка заменяет только догадку на случай, когда плана нет.
REP_RANGE_PRESETS: tuple[tuple[int, int], ...] = (
    (3, 6),
    (5, 12),
    (8, 12),
    (8, 15),
    (12, 20),
)


def user_rep_range(user: Any) -> tuple[int, int]:
    """Диапазон по умолчанию у этого атлета: его пресет или 5–12.

    `user` — строка users (sqlite Row или dict). Пара, которой нет среди
    пресетов (ручная правка базы, колонки ещё нет), молча даёт 5–12: подсказка
    не должна ломаться из-за настройки."""
    try:
        low, high = user["rep_range_min"], user["rep_range_max"]
    except (KeyError, IndexError, TypeError):
        return (REP_RANGE_MIN, REP_RANGE_MAX)
    if (low, high) in REP_RANGE_PRESETS:
        return (int(low), int(high))
    return (REP_RANGE_MIN, REP_RANGE_MAX)

# Целевой недельный объём на группу мышц, в рабочих подходах. Round numbers
# rather than a citation: the dose-response literature usually quotes ~10
# sets/week as the point where a group is trained *enough*, with more still
# paying off for trained lifters — so treat 6 as "меньше этого мало похоже на
# тренировку группы", not as a scientific floor.
WEEKLY_VOLUME_MIN = 6
WEEKLY_VOLUME_MAX = 12

# Окно, за которое считается объём на главном экране. Скользящие 7 дней, а не
# календарная неделя: с понедельника счётчик показывал бы нули ровно в тот день,
# когда человек открывает бота планировать неделю.
VOLUME_WINDOW_DAYS = 7


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
    # Упражнение на время (seed_data.TIMED_TEMPLATES): target_reps — секунды.
    is_timed: bool = False
    # top_set_backoff: "top" — цель тяжёлого топ-сета, "backoff" — подходов
    # после него (вес — backoff_pct % от сегодняшнего топа). None — все прочие
    # правила, где подходы друг от друга не отличаются.
    role: Optional[str] = None
    backoff_pct: Optional[int] = None
    # backoff: сколько бэкофф-подходов программа просит после топа.
    backoff_sets: Optional[int] = None


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
    target_weight: float,
    last_weight: float,
    last_reps: int,
    formula: str,
    rep_range: Optional[tuple[int, int]] = None,
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
    # Без RPE, с обеих сторон сравнения: цель — это план на будущее, и RPE у неё
    # нет по определению. Подставить сюда RPE прошлого подхода значило бы
    # сравнивать его повторы до отказа с записанными повторами цели и требовать
    # на выходе лишний повтор — то есть отдавать человеку в план ту самую работу
    # «в отказ», от которой запас его и уберёг.
    low, high = rep_range or (REP_RANGE_MIN, REP_RANGE_MAX)
    if not counts_for_e1rm(last_reps):
        # Прошлый подход длиннее E1RM_MAX_REPS: расчётного максимума у него
        # нет, держать нечего — прибавка веса и верх диапазона.
        return high
    reference = e1rm(last_weight, last_reps, formula) * E1RM_HOLD_TOLERANCE
    for reps in range(low, high):
        if e1rm(target_weight, reps, formula) >= reference:
            return reps
    return high


# Границы шага и потолка повторов у правила прогрессии (routine_exercises.
# progression), зажимаемые здесь — а не только там, где правило ЗАПИСЫВАЕТСЯ
# (ai_trainer._clean_progression чистит то, что предложила модель). У правила
# есть и второй, доверенный на глаз, но не проверяемый путь записи —
# `PATCH /routine-exercises/{id}` (api_v1_programs.update_routine_exercise)
# пишет присланный клиентом progression дословно, без всякой чистки. Без
# клампа здесь опечатка или баг в приложении (step: 999999) переживает запись
# и всплывает только тут, в цели: «🎯 Цель: 1000059×5» вместо «62.5×5» — ровно
# то невозможное число, от которого и заведена вся эта функция. Один расчёт —
# значит и одна граница того, что он готов предложить, а не по одной на
# каждого, кто в состоянии дозаписать колонку в БД напрямую.
PROGRESSION_MIN_STEP = 0.25
# Потолок — в килограммах: шаг хранится в единицах пользователя, и одно число
# на обе единицы давало атлету в фунтах потолок в 25 lb (≈11 кг) — вдвое уже,
# чем в кг. Для фунтов — тот же физический потолок (progression_max_step).
PROGRESSION_MAX_STEP = 25.0


def progression_max_step(unit: str) -> float:
    """Верхняя граница шага прогрессии в единицах пользователя."""
    if unit == "lb":
        return round(PROGRESSION_MAX_STEP * config.LB_PER_KG, 2)
    return PROGRESSION_MAX_STEP
# Упражнения на время (планка): «повторы» — секунды, и шаг правила тоже в
# секундах (`step_unit: "sec"`, см. ai_trainer._fit_progression_to_kind).
# 5 секунд — заметно, но не скачок; больше 15 за раз — уже не прибавка, а
# другое упражнение.
TIMED_STEP_DEFAULT = 5
TIMED_STEP_MIN = 5
TIMED_STEP_MAX = 15

# Верх диапазона повторов у явного правила — тот же потолок, что у схемы
# подходов программы (ai_trainer.PROGRAM_MAX_REPS): невалидный reps_top не
# должен требовать от человека забега на сотни повторов в одном подходе.
PROGRESSION_MAX_REPS_TOP = 50


def suggest_progression(
    last_sets: list[tuple[float, int]],
    *,
    unit: str = "kg",
    inferred_step: Optional[float] = None,
    formula: str = "epley",
    rule: Optional[dict] = None,
    planned_reps: Optional[tuple[int, int]] = None,
    kind: str = "weight",
    today_sets: Optional[list[tuple[float, int]]] = None,
    last_rpes: Optional[list[Optional[float]]] = None,
    default_range: Optional[tuple[int, int]] = None,
) -> Optional[ProgressionSuggestion]:
    """Next-session target from last session's sets, by double progression.

    While the top working set is still inside the rep range, add a rep at the
    same weight; once it crossed the top of the range (>= REP_RANGE_MAX), bump
    the weight by one step (see weight_step_for) and restart at the lowest rep
    count that doesn't give back the e1RM already earned (_reps_holding_e1rm).
    Bodyweight sets (weight 0) simply chase one more rep.

    `planned_reps` — диапазон повторов из схемы на карточке («3×6–12» → (6, 12),
    разбирает formatting.parse_routine_target на стороне вызывающего). Цель
    обязана держаться внутри него: на экране эти две строки стоят рядом, и
    «План: 3×5–8» над «Цель: 52.5×10» — это не подсказка, а спор бота с самим
    собой. Диапазон по умолчанию (REP_RANGE_MIN..REP_RANGE_MAX) — догадка на
    случай, когда схемы нет вовсе, и написанному в программе она обязана
    уступать. Явное правило прогрессии (`rule`) всё равно старше: оно
    структурное и пишется под конкретное упражнение, а схема — свободный текст.

    `rule` — the progression the program itself prescribes for this exercise
    (routine_exercises.progression, written by the AI trainer — see
    db.set_routine_exercise_progression). Without it this function guesses the
    rep range from a global default and the step from history, which is the
    right behaviour for a lift the user just does; but when the program says
    «доходишь до 8 повторов — прибавляй 2.5», the hint has no business
    proposing anything else. Unknown or malformed rules fall through to the
    default, so a rule the model invented can never break the hint.

    `kind` — вид нагрузки упражнения (seed_data.progression_kind по
    идентичности). "timed": повторы — это секунды, цель — те же секунды плюс
    шаг в секундах при том же весе, никаких «+кг». "bodyweight"/"no_load": шаг
    в кг из правила программы и linear_load не действуют — их могла записать
    модель до того, как их стали снимать; без отягощения цель и так в
    повторах, а с поясом и блином — обычная двойная прогрессия по истории.

    `today_sets`/`last_rpes` нужны только правилу top_set_backoff (см.
    suggest_top_set_backoff): там первый подход дня и следующие — разные
    цели, а прибавка к топу зависит от записанного RPE.

    `default_range` — диапазон по умолчанию из настроек атлета
    (user_rep_range); без него — REP_RANGE_MIN..REP_RANGE_MAX. Работает
    только там, где нет ни схемы (`planned_reps`), ни правила с reps_top.
    """
    working = [(w, r) for w, r in last_sets if r > 0]
    if not working:
        return None
    if kind == "timed":
        top_weight = max(w for w, _ in working)
        best = max(r for w, r in working if w == top_weight)
        step = None
        if (rule or {}).get("step_unit") == "sec":
            step = _positive_number((rule or {}).get("step"))
        step = int(round(max(TIMED_STEP_MIN, min(step or TIMED_STEP_DEFAULT, TIMED_STEP_MAX))))
        return ProgressionSuggestion(
            "add_reps", top_weight, best + step, is_bodyweight=top_weight == 0,
            from_weight=top_weight, from_reps=best, is_timed=True,
        )
    if kind in ("bodyweight", "no_load") and rule:
        rule = (
            None if rule.get("rule") in ("linear_load", "top_set_backoff")
            else {k: v for k, v in rule.items() if k != "step"}
        )
    if all(w == 0 for w, _ in working):
        best_reps = max(r for _, r in working)
        return ProgressionSuggestion(
            "add_reps", 0.0, best_reps + 1, is_bodyweight=True,
            from_weight=0.0, from_reps=best_reps,
        )
    top_weight = max(w for w, _ in working)
    reps_at_top = max(r for w, r in working if w == top_weight)

    rule_name = (rule or {}).get("rule")
    if rule_name == "top_set_backoff":
        rpes = list(last_rpes or [])
        return suggest_top_set_backoff(
            [(w, r, rpes[i] if i < len(rpes) else None) for i, (w, r) in enumerate(last_sets)],
            today_sets or [], rule, unit=unit, inferred_step=inferred_step,
        )
    rule_step = _positive_number((rule or {}).get("step"))
    if rule_step is not None:
        rule_step = max(PROGRESSION_MIN_STEP, min(rule_step, progression_max_step(unit)))
    reps_top = _positive_int((rule or {}).get("reps_top"))
    if reps_top is not None:
        reps_top = min(reps_top, PROGRESSION_MAX_REPS_TOP)

    # linear_load: вес растёт каждую тренировку, повторы не при чём.
    if rule_name == "linear_load" and rule_step:
        target_weight = round(top_weight + rule_step, 2)
        return ProgressionSuggestion(
            "add_weight", target_weight, reps_at_top,
            from_weight=top_weight, from_reps=reps_at_top, from_rule=True,
        )

    # double_progression: тот же алгоритм, что и по умолчанию, но верх
    # диапазона и шаг берём из программы, а не угадываем.
    planned_range = planned_reps
    if rule_name == "double_progression" and reps_top:
        top_of_range = reps_top
    elif planned_range:
        top_of_range = planned_range[1]
    else:
        top_of_range = (default_range or (REP_RANGE_MIN, REP_RANGE_MAX))[1]
    from_rule = rule_name == "double_progression" and bool(reps_top or rule_step)

    if reps_at_top >= top_of_range:
        step = rule_step or weight_step_for(top_weight, unit, inferred_step)
        target_weight = round(top_weight + step, 2)
        return ProgressionSuggestion(
            "add_weight",
            target_weight,
            _reps_holding_e1rm(
                target_weight, top_weight, reps_at_top, formula,
                rep_range=(
                    (planned_range[0], top_of_range) if planned_range
                    else (min(default_range[0], top_of_range), top_of_range) if default_range
                    else None
                ),
            ),
            from_weight=top_weight,
            from_reps=reps_at_top,
            from_rule=from_rule,
        )
    return ProgressionSuggestion(
        "add_reps", top_weight, reps_at_top + 1,
        from_weight=top_weight, from_reps=reps_at_top, from_rule=from_rule,
    )


# top_set_backoff (ai_trainer._clean_progression): топ-сет — 1–8 повторов,
# бэкоффов 1–5, их вес — 70–95 % от топа. Прибавка к топу — только когда верх
# диапазона взят с запасом: RPE не выше этого порога, если его записали.
TOP_SET_REPS_MIN, TOP_SET_REPS_MAX = 1, 8
BACKOFF_SETS_MIN, BACKOFF_SETS_MAX = 1, 5
BACKOFF_PCT_MIN, BACKOFF_PCT_MAX = 70, 95
TOP_SET_RESERVE_RPE = 8.0


def plate_step(unit: str = "kg", inferred_step: Optional[float] = None) -> float:
    """Шаг, до которого округляется посчитанный процентом вес — чтобы цель
    была весом, который можно собрать на штанге, а не «144.0»."""
    base = DEFAULT_WEIGHT_STEP.get(unit, 2.5)
    if inferred_step is not None and 0 < inferred_step < base:
        return inferred_step
    return base


def round_to_plate(weight: float, unit: str = "kg", inferred_step: Optional[float] = None) -> float:
    step = plate_step(unit, inferred_step)
    return round(round(weight / step) * step, 2)


def suggest_top_set_backoff(
    last_sets: list[tuple[float, int, Optional[float]]],
    today_sets: list[tuple[float, int]],
    rule: dict,
    *,
    unit: str = "kg",
    inferred_step: Optional[float] = None,
) -> Optional[ProgressionSuggestion]:
    """Цель по схеме «топ-сет + бэкоффы»: один тяжёлый подход в диапазоне
    top_reps_min..max, потом backoff_sets подходов по backoff_pct % его веса на
    те же повторы.

    Сегодня ещё нет подходов — цель топ-сета: прошлый топ (самый тяжёлый
    подход) плюс step, если верх диапазона взят с запасом (записанный RPE не
    выше TOP_SET_RESERVE_RPE; без RPE хватает самих повторов), и тогда снова с
    низа диапазона; иначе тот же вес на повтор больше. Подходы сегодня есть —
    цель бэкоффа от СЕГОДНЯШНЕГО топа: прибавка или неудачный день уже в нём.
    """
    working = [(w, r, rpe) for w, r, rpe in last_sets if r > 0 and w > 0]
    today = [(w, r) for w, r in (today_sets or []) if r > 0 and w > 0]
    low = _positive_int(rule.get("top_reps_min")) or TOP_SET_REPS_MIN
    high = _positive_int(rule.get("top_reps_max")) or low
    low, high = sorted((
        max(TOP_SET_REPS_MIN, min(low, TOP_SET_REPS_MAX)),
        max(TOP_SET_REPS_MIN, min(high, TOP_SET_REPS_MAX)),
    ))
    pct = _positive_int(rule.get("backoff_pct")) or 85
    pct = max(BACKOFF_PCT_MIN, min(pct, BACKOFF_PCT_MAX))
    backoff_sets = _positive_int(rule.get("backoff_sets"))
    if today:
        top_weight, top_reps = max(today)
        return ProgressionSuggestion(
            "backoff", round_to_plate(top_weight * pct / 100, unit, inferred_step), top_reps,
            from_weight=top_weight, from_reps=top_reps, from_rule=True,
            role="backoff", backoff_pct=pct,
            backoff_sets=min(backoff_sets, BACKOFF_SETS_MAX) if backoff_sets else None,
        )
    if not working:
        return None
    top_weight = max(w for w, _, _ in working)
    reps_at_top, rpe_at_top = max(
        ((r, rpe) for w, r, rpe in working if w == top_weight), key=lambda x: x[0]
    )
    with_reserve = rpe_at_top is None or rpe_at_top <= TOP_SET_RESERVE_RPE
    if reps_at_top >= high and with_reserve:
        step = _positive_number(rule.get("step"))
        if step is not None:
            step = max(PROGRESSION_MIN_STEP, min(step, progression_max_step(unit)))
        else:
            step = weight_step_for(top_weight, unit, inferred_step)
        return ProgressionSuggestion(
            "add_weight", round(top_weight + step, 2), low,
            from_weight=top_weight, from_reps=reps_at_top, from_rule=True, role="top",
        )
    return ProgressionSuggestion(
        "add_reps", top_weight, max(low, min(reps_at_top + 1, high)),
        from_weight=top_weight, from_reps=reps_at_top, from_rule=True, role="top",
    )


# Неделя разгрузки: примерно 60 % подходов на примерно 90 % веса.
DELOAD_WEIGHT_PCT = 90
DELOAD_SETS_PCT = 60


def is_deload_week(program_start: dt.date, day: dt.date, every_weeks: Optional[int]) -> bool:
    """Каждая N-я календарная неделя (с понедельника), считая неделю, в которую
    программу сохранили, первой: при N=4 разгрузка на 4-й, 8-й, 12-й неделе.
    Правило нарочно простое — человек должен уметь пересчитать его сам."""
    if not every_weeks or every_weeks <= 0 or day < program_start:
        return False
    week = (_week_monday(day) - _week_monday(program_start)).days // 7 + 1
    return week % every_weeks == 0


def deload_suggestion(
    suggestion: ProgressionSuggestion, *, unit: str = "kg", inferred_step: Optional[float] = None
) -> ProgressionSuggestion:
    """Цель на неделю разгрузки: без прибавки, от прошлого подхода на
    DELOAD_WEIGHT_PCT % веса и те же повторы. Бэкофф уже считается от
    сегодняшнего (облегчённого) топа — второй раз его не режем. Вес тела и
    подходы на время держат свои повторы: там нечего снимать в процентах."""
    if suggestion.role == "backoff":
        return suggestion
    if suggestion.is_bodyweight or suggestion.is_timed or suggestion.from_weight <= 0:
        reps = suggestion.from_reps or suggestion.target_reps
        return ProgressionSuggestion(
            "deload", suggestion.from_weight if suggestion.is_timed else suggestion.target_weight,
            reps, is_bodyweight=suggestion.is_bodyweight, from_weight=suggestion.from_weight,
            from_reps=suggestion.from_reps, is_timed=suggestion.is_timed, role=suggestion.role,
        )
    return ProgressionSuggestion(
        "deload",
        round_to_plate(suggestion.from_weight * DELOAD_WEIGHT_PCT / 100, unit, inferred_step),
        suggestion.from_reps or suggestion.target_reps,
        from_weight=suggestion.from_weight, from_reps=suggestion.from_reps,
        from_rule=suggestion.from_rule, role=suggestion.role,
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


# Меньше стольких законченных тренировок — «самого продуктивного дня» нет:
# одна суббота в истории — не привычка, а одна суббота.
WEEKDAY_LEADER_MIN_WORKOUTS = 4


def most_frequent_weekday(
    workout_dates: Iterable[dt.date], min_lead: int = 2,
    min_workouts: int = WEEKDAY_LEADER_MIN_WORKOUTS,
) -> int | None:
    """Which weekday (0=Mon) the user trains on most, or None when nothing
    stands out.

    `min_lead` is how many workouts clear of the runner-up the winner must be:
    "твой самый продуктивный день" is a claim about a habit, and 5-vs-4 is
    noise, not a habit. With fewer than `min_workouts` workouts there is no
    habit yet — None. With enough workouts all on one weekday that weekday is
    the honest answer (someone who only trains on Saturdays is productive on
    Saturdays), and there is no runner-up for `min_lead` to apply to.
    """
    dates = list(workout_dates)
    if len(dates) < min_workouts:
        return None
    counts: dict[int, int] = {}
    for d in dates:
        counts[d.weekday()] = counts.get(d.weekday(), 0) + 1
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
    # max(0, ...) — «дней с последней тренировки» не бывает отрицательным.
    # Дата тренировки может оказаться позже «сегодня» (импорт из CSV, правка даты
    # вручную, а до перевода агрегатов на местные сутки — ещё и тренировка,
    # закрытая около полуночи), и тогда счётчик показывал «-1 день», что читается
    # как сломанный экран. Ноль — это «сегодня», ближайшее правдивое значение.
    days_since_last = max(0, (today - max(dates)).days)

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


# ---------- застой: растёт, стоит или падает ----------
#
# Вердикт — по НАКЛОНУ лучшего e1RM сессии за окно, а не по повторам
# на одном весе: многие тренируются с разным весом каждую тренировку, и пар
# «тот же вес» у них просто нет. Повторы на одном весе и прибавка веса — только
# опорные факты; вердикт они не переворачивают, кроме одного правила: вес
# подняли, а повторы на новом весе держатся — это не застой и не регресс.
#
# Раньше модель путала здесь веса: «на 77 кг повторы упали с 12 до 7», когда 12
# было на 73 (подход после 77×6), а на 77 повторы выросли 6→7.

# Сессий в окне меньше этого — судить не о чем: две-три точки складываются в
# любой наклон, и «встало» на них означает только «мало данных».
STALL_MIN_SESSIONS = 4

# Окно, за которое смотрим. Восемь недель: на меньшем окне нормальная
# разгрузочная неделя читается застоем, на большем — застой позапрошлого
# месяца, из которого человек уже вылез.
STALL_WINDOW_WEEKS = 8

# «Ровно» — наклон по модулю меньше этой доли от e1RM в неделю: 0.5% от 100 кг
# — полкило в неделю, два кило в месяц. Доля, а не килограммы: у гантелей на
# 12 кг и у приседа на 180 шум разный.
STALL_FLAT_SHARE = 0.005

# Застой называем только на ≥ стольких точках, растянутых на ≥ столько недель:
# три сессии за неделю ровными бывают у кого угодно.
STALL_MIN_POINTS = 3
STALL_MIN_SPAN_WEEKS = 3

# Столько недель на одном весе — «вес стоит» (для двойной прогрессии): на ней
# вес по определению держится пару недель, пока набираются повторы.
STALL_WEEKS_FLAT = 3

# Подходы длиннее — разминка или «сгорание», в e1RM не идут. Других фильтров
# по повторам нет: кто работает в 12-15, должен получить тренд.
TREND_MAX_REPS = 20


def _trend_e1rm_of(weight: float, reps: int, formula: str) -> float:
    # Мимо analytics.e1rm: тот обнуляет подходы длиннее E1RM_MAX_REPS (для
    # рекордов), а для наклона подход 60×14 у того, кто работает в 12-15, —
    # обычная рабочая точка. Сравнение тут — с собой же, тем же способом.
    if formula == "brzycki":
        return brzycki_e1rm(weight, reps)
    return epley_e1rm(weight, reps)


def trend_best_set(session: "SessionStats") -> Optional["SetRow"]:
    """Подход с лучшим e1RM сессии (без RPE), добивки на меньшем весе тоже
    в счёте. Подходы длиннее TREND_MAX_REPS не считаются, других фильтров по
    повторам нет."""
    sets = [s for s in session.sets if s.weight > 0 and 0 < s.reps <= TREND_MAX_REPS]
    if not sets:
        return None
    return max(sets, key=lambda s: (_trend_e1rm_of(s.weight, s.reps, session.formula), s.weight))


def trend_e1rm(session: "SessionStats") -> Optional[float]:
    """Лучший e1RM сессии (trend_best_set) — точка наклона. None — у сессии
    нет подходящего подхода, и в наклон она не идёт вовсе."""
    best = trend_best_set(session)
    return None if best is None else _trend_e1rm_of(best.weight, best.reps, session.formula)


# Сессия, чей лучший e1RM ниже этой доли от медианы трёх предыдущих, —
# «лёгкая»: разминка без рабочих подходов или недописанная тренировка. Прод:
# присед 05.10 записан одним «20×8» после сессий 92.5×5, e1RM ~25 обвалил наклон,
# и обе модели сказали «регресс, −10 кг/нед».
LIGHT_SESSION_SHARE = 0.7
LIGHT_SESSION_BASELINE = 3


def light_sessions(sessions: list["SessionStats"]) -> list["SessionStats"]:
    """Одиночные провалы: сессии, которые в наклон и вердикт не идут.

    Исключается только ОДИНОЧНАЯ лёгкая сессия — соседи по обе стороны
    нормальные (последняя сессия годится, если перед ней нормальная). Две
    лёгкие подряд — это уже разгрузка или возвращение после перерыва, и
    прятать их значило бы прятать правду.
    """
    values = [trend_e1rm(s) for s in sessions]
    candidate = [False] * len(sessions)
    baseline: list[float] = []
    for i, v in enumerate(values):
        if v is None:
            continue
        ref = baseline[-LIGHT_SESSION_BASELINE:]
        if ref and v < statistics.median(ref) * LIGHT_SESSION_SHARE:
            candidate[i] = True
            continue
        baseline.append(v)
    out = []
    for i, s in enumerate(sessions):
        if not candidate[i]:
            continue
        prev_light = i > 0 and candidate[i - 1]
        next_light = i + 1 < len(sessions) and candidate[i + 1]
        if not prev_light and not next_light:
            out.append(s)
    return out


@dataclass
class StallVerdict:
    """Растёт, стоит или падает это упражнение — и почему.

    kind: "growing" | "double_progression" (рост повторами на том же весе) |
    "progressing" (вес подняли, повторы на новом держатся) | "dead_end" |
    "regressing" | "unclear" (ровно, но точек или недель мало для «застоя»).
    """
    kind: str
    sessions_in_window: int
    e1rm_slope_per_week: Optional[float]
    top_weight: float
    weeks_weight_flat: Optional[float]
    reps_at_top_first: int
    reps_at_top_last: int
    # Верхний подход добрал верх диапазона — по методике это сигнал прибавить
    # вес, и его некому было применить проактивно.
    ready_to_add_weight: bool
    avg_top_rpe: Optional[float]
    # С какой даты стоит текущий рабочий вес (последняя прибавка в окне или
    # начало окна) и сколько сессий на нём было. None — упражнение на весе тела.
    weight_since: Optional[str] = None
    sessions_at_top_weight: int = 0
    # С какого веса прибавили до текущего внутри окна; None — прибавки в окне
    # не было (или вес тела).
    weight_raised_from: Optional[float] = None
    # Наклон в процентах от среднего e1RM (повторов — для веса тела) в неделю.
    slope_pct_per_week: Optional[float] = None
    trend_points: int = 0
    # Лучшая точка окна и последняя: (дата, вес, повторы, e1RM) — чтобы «почему»
    # называл оба факта, а не один удобный.
    e1rm_peak: Optional[tuple[str, float, int, float]] = None
    e1rm_latest: Optional[tuple[str, float, int, float]] = None
    # Первая точка окна — начало главного факта «e1RM и рабочий вес от → до».
    e1rm_first: Optional[tuple[str, float, int, float]] = None
    # Рабочий вес первой сессии окна (у последней — top_weight).
    first_top_weight: float = 0.0
    # Для веса тела — лучшие повторы первой и последней сессии окна.
    reps_window: Optional[tuple[str, int, str, int]] = None
    window_weeks: int = STALL_WINDOW_WEEKS
    # Одиночные лёгкие сессии окна, не взятые в расчёт: (дата, "20×8, ...").
    light_sessions: list[tuple[str, str]] = field(default_factory=list)


def _session_top_weight(session: SessionStats) -> float:
    return max((s.weight for s in session.sets), default=0.0)


def best_reps_at(session: SessionStats, weight: float) -> int:
    """Лучшие повторы сессии НА ЭТОМ весе (0 — такого веса в ней не было).

    Не max_reps_in_set: в сессии «77×6, 73×12» лучший подход по повторам — на
    73, и сравнение «повторы на рабочем весе было 12, стало 7» смешивало два
    разных веса.
    """
    return max((s.reps for s in session.sets if s.weight == weight), default=0)


def _last_weight_bump(sessions: list[SessionStats]) -> tuple[int, float]:
    """(индекс сессии с последней прибавкой, лучший вес ДО неё).

    Прибавка — сессия, чей рабочий вес выше всего, что было до неё в этом
    списке. Именно рекорд, а не «больше, чем в прошлый раз»: пила 100 → 95 →
    100 иначе каждую вторую сессию читалась бы как прибавка. Прибавок не было —
    (0, вес первой сессии). Одна точка правды для classify_stall и
    progress_facts.
    """
    best = _session_top_weight(sessions[0])
    idx, before = 0, best
    for i, s in enumerate(sessions[1:], start=1):
        w = _session_top_weight(s)
        if w > best:
            idx, before, best = i, best, w
    return idx, before


def classify_stall(
    sessions: list[SessionStats],
    today: dt.date,
    *,
    window_weeks: int = STALL_WINDOW_WEEKS,
    min_sessions: int = STALL_MIN_SESSIONS,
) -> Optional[StallVerdict]:
    """Разбор одного упражнения. None — данных в окне не хватает.

    Окно считается от СЕГОДНЯ, а не от последней сессии: упражнение, брошенное
    два месяца назад, не «встало» — его просто не делают, и в выдаче ему не
    место (иначе на вопрос «что у меня встало» приезжает список заброшенного).
    """
    since = today - dt.timedelta(weeks=window_weeks)
    light = {id(s) for s in light_sessions(sessions)}
    in_window = [
        s for s in sessions
        if dt.datetime.fromisoformat(s.started_at).date() >= since
    ]
    skipped = [
        (s.started_at[:10], ", ".join(f"{x.weight:g}×{x.reps}" for x in s.sets))
        for s in in_window if id(s) in light
    ]
    window = [s for s in in_window if id(s) not in light]
    if len(window) < min_sessions:
        return None

    bodyweight_only = all(s.is_bodyweight_mode for s in window)
    top_weight = _session_top_weight(window[-1])

    # Точка наклона — лучший e1RM сессии без RPE (полбалла самооценки иначе
    # решали бы, «встал» человек или «регрессирует»); у веса тела — повторы.
    points: list[tuple[dt.datetime, float]] = []
    marks: list[tuple[str, float, int, float]] = []
    for s in window:
        if bodyweight_only:
            value: Optional[float] = float(s.max_reps_in_set)
        else:
            best = trend_best_set(s)
            value = trend_e1rm(s)
            if best is not None and value:
                marks.append((s.started_at[:10], best.weight, best.reps, round(value, 1)))
        if value:
            points.append((dt.datetime.fromisoformat(s.started_at), value))
    trend = linear_trend(points)
    slope = trend.slope_per_week if trend else None
    mean = sum(v for _, v in points) / len(points) if points else 0.0
    rel = slope / mean if slope is not None and mean > 0 else None
    span_weeks = (points[-1][0].date() - points[0][0].date()).days / 7 if points else 0.0

    weeks_flat: Optional[float] = None
    weight_since: Optional[str] = None
    raised_from: Optional[float] = None
    if not bodyweight_only:
        # Сколько вес стоит — между первым и последним появлением текущего
        # рабочего веса, а не до сегодня.
        bump_idx, before = _last_weight_bump(window)
        if bump_idx > 0 and _session_top_weight(window[bump_idx]) == top_weight:
            raised_from = before
        last_bump_at = dt.datetime.fromisoformat(window[bump_idx].started_at).date()
        last_at = dt.datetime.fromisoformat(window[-1].started_at).date()
        weeks_flat = round((last_at - last_bump_at).days / 7, 1)
        weight_since = last_bump_at.isoformat()

    # Повторы на текущем рабочем весе — только подходов НА этом весе. Для веса
    # тела «текущий вес» нулевой и условие ловит все сессии.
    at_top = [
        s for s in window
        if bodyweight_only or _session_top_weight(s) == top_weight
    ]
    reps_first = best_reps_at(at_top[0], top_weight) if at_top else 0
    reps_last = best_reps_at(at_top[-1], top_weight) if at_top else 0

    rpes = [
        s.top_set.rpe for s in window
        if s.top_set is not None and s.top_set.rpe is not None
    ]
    avg_rpe = round(sum(rpes) / len(rpes), 1) if rpes else None

    enough = len(points) >= STALL_MIN_POINTS
    if rel is None or not enough:
        kind = "unclear"
    elif rel <= -STALL_FLAT_SHARE:
        kind = "regressing"
    elif rel < STALL_FLAT_SHARE:
        kind = "dead_end" if span_weeks >= STALL_MIN_SPAN_WEEKS else "unclear"
    else:
        weight_flat = bodyweight_only or (
            weeks_flat is not None and weeks_flat >= STALL_WEEKS_FLAT
        )
        # Рост повторами на том же весе — отдельное имя: на вопрос «почему вес
        # стоит» ответ «растёшь» не отвечает, а «схема работает, вес прибавится
        # сам» — отвечает.
        kind = "double_progression" if weight_flat and reps_last > reps_first else "growing"

    # Единственное правило, где опорные факты сильнее наклона: вес подняли в
    # окне, а повторы на новом весе держатся или растут — привыкание к новому
    # весу, не застой и не регресс.
    if kind in ("dead_end", "regressing") and raised_from is not None and reps_last >= reps_first:
        kind = "progressing"

    return StallVerdict(
        kind=kind,
        sessions_in_window=len(window),
        e1rm_slope_per_week=round(slope, 2) if slope is not None else None,
        top_weight=top_weight,
        weeks_weight_flat=weeks_flat,
        reps_at_top_first=reps_first,
        reps_at_top_last=reps_last,
        ready_to_add_weight=reps_last >= REP_RANGE_MAX,
        avg_top_rpe=avg_rpe,
        weight_since=weight_since,
        sessions_at_top_weight=len(at_top),
        weight_raised_from=raised_from,
        slope_pct_per_week=round(rel * 100, 2) if rel is not None else None,
        trend_points=len(points),
        e1rm_peak=max(marks, key=lambda m: m[3]) if marks else None,
        e1rm_latest=marks[-1] if marks else None,
        light_sessions=skipped,
        e1rm_first=marks[0] if marks else None,
        first_top_weight=_session_top_weight(window[0]),
        window_weeks=window_weeks,
        reps_window=(
            (window[0].started_at[:10], window[0].max_reps_in_set,
             window[-1].started_at[:10], window[-1].max_reps_in_set)
            if bodyweight_only else None
        ),
    )


# ---------- факты для тренера: тренд и застой, посчитанные кодом ----------
#
# Модель судила о застое по сырому списку подходов и путала веса: «на 77 кг
# повторы упали с 12 до 7», когда 12 было на 73, а на 77 повторы выросли 6→7.
# Всё, что можно утверждать про «встал / растёт / упали повторы», считается
# здесь, а промпт велит говорить только по этим полям.

# Рабочий вес — не легче этой доли от тяжёлого веса той же сессии: разминка на
# 50% иначе раздула бы same_weight_reps рядом ровных «10, 10, 10».
WORKING_WEIGHT_SHARE = 0.8
# Сколько весов отдаём в same_weight_reps (самые недавние) — ради токенов.
SAME_WEIGHT_MAX_WEIGHTS = 3
# «Изменение e1RM против четырёх недель назад».
E1RM_COMPARE_WEEKS = 4


def _date(session: SessionStats) -> str:
    return session.started_at[:10]


def _change(first: float, last: float) -> str:
    return "up" if last > first else ("down" if last < first else "flat")


def same_weight_reps(sessions: list[SessionStats]) -> list[dict[str, Any]]:
    """По каждому рабочему весу, встречавшемуся в ≥2 сессиях: (дата, лучшие
    повторы на ЭТОМ весе) по порядку и итог first→last. Самые недавние веса
    первыми, не больше SAME_WEIGHT_MAX_WEIGHTS."""
    by_weight: dict[float, list[tuple[str, int]]] = {}
    last_seen: dict[float, str] = {}
    for s in sessions:
        if not s.sets:
            continue
        top = _session_top_weight(s)
        for w in sorted({x.weight for x in s.sets if x.weight >= top * WORKING_WEIGHT_SHARE}):
            by_weight.setdefault(w, []).append((_date(s), best_reps_at(s, w)))
            last_seen[w] = s.started_at
    repeated = [w for w, pts in by_weight.items() if len(pts) >= 2]
    repeated.sort(key=lambda w: (last_seen[w], w), reverse=True)
    return [
        {
            "weight": w,
            "reps": [list(p) for p in by_weight[w]],
            "change": _change(by_weight[w][0][1], by_weight[w][-1][1]),
        }
        for w in repeated[:SAME_WEIGHT_MAX_WEIGHTS]
    ]


def e1rm_trend(
    sessions: list[SessionStats], today: dt.date, *, window_weeks: int = STALL_WINDOW_WEEKS
) -> Optional[dict[str, Any]]:
    """Лучший e1RM по сессиям за окно: наклон в неделю и изменение против
    сессии четырёхнедельной давности. None — у упражнения нет e1RM (вес тела).
    Точки — trend_e1rm без одиночных лёгких сессий (light_sessions), те же,
    по которым classify_stall считает наклон."""
    light = {id(s) for s in light_sessions(sessions)}
    scored = [
        (s, v) for s in sessions if id(s) not in light and (v := trend_e1rm(s)) is not None
    ]
    if not scored:
        return None
    since = today - dt.timedelta(weeks=window_weeks)
    window = [(s, v) for s, v in scored if dt.datetime.fromisoformat(s.started_at).date() >= since]
    trend = linear_trend(
        [(dt.datetime.fromisoformat(s.started_at), v) for s, v in window]
    )
    latest_s, latest_v = scored[-1]
    cutoff = today - dt.timedelta(weeks=E1RM_COMPARE_WEEKS)
    before = [
        (s, v) for s, v in scored if dt.datetime.fromisoformat(s.started_at).date() <= cutoff
    ]
    out: dict[str, Any] = {
        "window_weeks": window_weeks,
        "sessions_in_window": len(window),
        "slope_per_week": round(trend.slope_per_week, 2) if trend else None,
        "latest": [_date(latest_s), round(latest_v, 1)],
        "four_weeks_ago": None,
        "change_vs_four_weeks_ago": None,
    }
    if before:
        b_s, b_v = before[-1]
        out["four_weeks_ago"] = [_date(b_s), round(b_v, 1)]
        out["change_vs_four_weeks_ago"] = round(latest_v - b_v, 1)
    return out


def weight_increase(sessions: list[SessionStats], today: dt.date) -> Optional[dict[str, Any]]:
    """Последняя прибавка рабочего веса (по _last_weight_bump, как в
    classify_stall). None — прибавок не было или упражнение на весе тела."""
    if not sessions or all(s.is_bodyweight_mode for s in sessions):
        return None
    idx, before = _last_weight_bump(sessions)
    if idx == 0:
        return None
    s = sessions[idx]
    to = _session_top_weight(s)
    day = dt.datetime.fromisoformat(s.started_at).date()
    return {
        "date": _date(s),
        "from": before,
        "to": to,
        "step": round(to - before, 2),
        "weeks_ago": round((today - day).days / 7, 1),
    }


def _fmt(x: float) -> str:
    return f"{x:g}"


def stall_facts(sessions: list[SessionStats], today: dt.date, unit: str) -> dict[str, Any]:
    """classify_stall в виде, который модель пересказывает: вердикт, флаг
    «встал» (dead_end/regressing), с какой даты стоит рабочий вес и строка
    фактов «почему» — наклон, лучшая и последняя точка e1RM и опорные факты
    (прибавка веса, повторы на текущем весе)."""
    v = classify_stall(sessions, today)
    if v is None:
        since = today - dt.timedelta(weeks=STALL_WINDOW_WEEKS)
        n = sum(1 for s in sessions if dt.datetime.fromisoformat(s.started_at).date() >= since)
        return {
            "stalled": None,
            "verdict": None,
            "top_weight_since": None,
            "why": (
                f"{n} sessions in last {STALL_WINDOW_WEEKS} weeks, need "
                f"{STALL_MIN_SESSIONS} to judge"
            ),
        }
    return {
        "stalled": v.kind in ("dead_end", "regressing"),
        "verdict": v.kind,
        "top_weight_since": v.weight_since,
        "why": stall_why(v, unit),
    }


def stall_why(v: StallVerdict, unit: str) -> str:
    """Строка фактов к вердикту — одна на get_exercise_progress и
    get_stalled_lifts: модель пересказывает её, а не выводит сама."""
    parts: list[str] = []
    # Главный факт — первым: как изменились e1RM и рабочий вес за окно. Повторы
    # на одном весе дальше — только деталь к застою или свежей прибавке.
    if v.e1rm_first is not None and v.e1rm_latest is not None:
        f, last = v.e1rm_first, v.e1rm_latest
        pct = f" ({(last[3] - f[3]) / f[3] * 100:+.0f}%)" if f[3] > 0 else ""
        parts.append(
            f"{f[0]}→{last[0]}: best e1RM {f[3]:g}→{last[3]:g} {unit}{pct}, "
            f"top weight {_fmt(v.first_top_weight)}→{_fmt(v.top_weight)} {unit}"
        )
    elif v.reps_window is not None:
        d0, r0, d1, r1 = v.reps_window
        parts.append(f"{d0}→{d1}: bodyweight, best reps {r0}→{r1}")
    parts.append(f"{v.sessions_in_window} sessions in {v.window_weeks} weeks")
    if v.e1rm_slope_per_week is not None:
        what = "best reps" if v.weight_since is None else "best e1RM"
        per = "" if v.weight_since is None else f" {unit}"
        parts.append(
            f"{what} slope {v.e1rm_slope_per_week:+g}{per}/week ({v.slope_pct_per_week:+g}%/week)"
        )
    if (
        v.e1rm_peak is not None and v.e1rm_latest is not None
        and v.e1rm_peak not in (v.e1rm_latest, v.e1rm_first)
    ):
        def _mark(m: tuple[str, float, int, float]) -> str:
            return f"{m[3]:g} ({_fmt(m[1])}×{m[2]}, {m[0]})"
        parts.append(f"peak e1RM {_mark(v.e1rm_peak)} → latest {_mark(v.e1rm_latest)}")
    if v.weight_raised_from is not None:
        parts.append(
            f"weight raised {_fmt(v.weight_raised_from)}→{_fmt(v.top_weight)} {unit} "
            f"on {v.weight_since}"
        )
    if v.weight_since is not None:
        parts.append(
            f"detail: top weight {_fmt(v.top_weight)} {unit} since {v.weight_since} "
            f"({v.weeks_weight_flat:g} weeks, {v.sessions_at_top_weight} sessions on it)"
        )
        parts.append(
            f"reps at {_fmt(v.top_weight)} {unit}: {v.reps_at_top_first}→{v.reps_at_top_last}"
        )
    for day, sets in v.light_sessions:
        parts.append(f"{day}: only {sets} — looks like warm-up or not fully logged, left out of trend")
    return "; ".join(parts)


def progress_facts(
    sessions: list[SessionStats], today: dt.date, unit: str, *, recent: int
) -> dict[str, Any]:
    """Все четыре факта для get_exercise_progress. `recent` — сколько
    последних сессий смотрит same_weight_reps: ровно те, что модель видит
    списком, чтобы даты в фактах совпадали с датами в истории."""
    # Порядок — по важности: вердикт со сводкой окна (e1RM и вес от → до)
    # первым, повторы на одном весе — последней деталью.
    return {
        "stall": stall_facts(sessions, today, unit),
        "e1rm_trend": e1rm_trend(sessions, today),
        "weight_increase": weight_increase(sessions, today),
        "same_weight_reps": same_weight_reps(sessions[-recent:]),
    }


# ---------- вердикт по упражнению для AI-сводки тренера ----------
#
# Модель сводки тренера сама сравнивала подходы и ошибалась: «присед 92.5×5 →
# 70×8» назвала ростом (e1RM ~107.9 → ~88.7, это спад), «40×8 → 40×8» —
# ростом, «32×10 → 34×8» (~42.7 → ~43.1) — «improved». Вердикт считается здесь
# той же меркой, что у прогресса атлета (лучший e1RM сессии по всем подходам,
# trend_best_set: подходы длиннее TREND_MAX_REPS не в счёте, одиночная лёгкая
# сессия — light_sessions — тоже), а модель только пересказывает его.

LIFT_UP = "up"
LIFT_DROP = "drop"
LIFT_FLAT = "flat"            # лучший подход тот же самый (40×8 → 40×8)
LIFT_NEAR_FLAT = "near_flat"  # e1RM сдвинулся в пределах шума (e1rm_change_is_noise)


@dataclass
class LiftChange:
    """Первая и последняя сессия упражнения в данных сводки и вердикт по ним.

    first/last — (дата, вес, повторы). e1RM — None у упражнения своим весом:
    там сравниваются лучшие повторы. pct — изменение e1RM в процентах."""
    verdict: str
    first: tuple[str, float, int]
    last: tuple[str, float, int]
    first_e1rm: Optional[float]
    last_e1rm: Optional[float]
    pct: Optional[float]
    sessions: int

    @property
    def same_best_set(self) -> bool:
        return self.first[1:] == self.last[1:]


def lift_change(sessions: list[SessionStats], unit: str = "kg") -> Optional[LiftChange]:
    """Вердикт по упражнению между первой и последней его сессией. None — сравнивать
    нечего (меньше двух сессий с годным подходом).

    - лучший подход тот же (вес и повторы) — LIFT_FLAT;
    - вес выше, повторы держатся или растут — LIFT_UP (прибавка веса);
    - разница e1RM в пределах шума (меньше 1 кг или 2%) — LIFT_NEAR_FLAT;
    - иначе по знаку e1RM: LIFT_UP или LIFT_DROP.
    """
    ordered = sorted(sessions, key=lambda s: s.started_at)
    if ordered and all(s.is_bodyweight_mode for s in ordered):
        reps = [(s, s.max_reps_in_set) for s in ordered if s.max_reps_in_set > 0]
        if len(reps) < 2:
            return None
        (fs, fr), (ls, lr) = reps[0], reps[-1]
        verdict = LIFT_FLAT if fr == lr else (LIFT_UP if lr > fr else LIFT_DROP)
        return LiftChange(
            verdict, (_date(fs), 0.0, fr), (_date(ls), 0.0, lr), None, None, None, len(reps)
        )
    light = {id(s) for s in light_sessions(ordered)}
    scored = [
        (s, best) for s in ordered
        if id(s) not in light and (best := trend_best_set(s)) is not None
    ]
    if len(scored) < 2:
        return None
    (fs, fb), (ls, lb) = scored[0], scored[-1]
    fv = _trend_e1rm_of(fb.weight, fb.reps, fs.formula)
    lv = _trend_e1rm_of(lb.weight, lb.reps, ls.formula)
    if (fb.weight, fb.reps) == (lb.weight, lb.reps):
        verdict = LIFT_FLAT
    elif lb.weight > fb.weight and lb.reps >= fb.reps:
        verdict = LIFT_UP
    elif e1rm_change_is_noise(fv, lv, unit):
        verdict = LIFT_NEAR_FLAT
    else:
        verdict = LIFT_UP if lv > fv else LIFT_DROP
    return LiftChange(
        verdict,
        (_date(fs), fb.weight, fb.reps),
        (_date(ls), lb.weight, lb.reps),
        round(fv, 1), round(lv, 1),
        round((lv - fv) / fv * 100, 1) if fv > 0 else None,
        len(scored),
    )


CLIENT_PROGRESSING = "progressing"
CLIENT_MIXED = "mixed"
CLIENT_DROPPING = "dropping"
CLIENT_FLAT = "flat"


def client_lift_verdict(changes: list[LiftChange], has_record: bool) -> Optional[str]:
    """Общий вердикт атлета по вердиктам упражнений: «растёт» — только если
    e1RM где-то вырос (LIFT_UP или свежий рекорд). None — сравнивать нечего."""
    up = has_record or any(c.verdict == LIFT_UP for c in changes)
    down = any(c.verdict == LIFT_DROP for c in changes)
    if up and down:
        return CLIENT_MIXED
    if up:
        return CLIENT_PROGRESSING
    if down:
        return CLIENT_DROPPING
    return CLIENT_FLAT if changes else None
