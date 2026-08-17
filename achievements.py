"""Achievement catalog and pure detection logic.

Kept free of DB/Telegram so the "which badges does this state earn?" rule is
trivially testable. handlers/workout.py builds an AchievementContext at finish
time, awards the new codes (db.award_achievements) and celebrates them on the
completion card; handlers/history.py renders the full grid.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Optional

import i18n


@dataclass(frozen=True)
class Achievement:
    """`code` — идентификатор, лежит в БД (таблица `achievements`) и не должен
    меняться никогда; `title`/`description` — то, что видит человек, поэтому
    они НЕ поля, а свойства, читающие каталог i18n по ключу, производному от
    кода, в момент обращения. Это принципиально: CATALOG собирается один раз
    на импорт модуля, один процесс обслуживает всех пользователей сразу, а
    язык каждого известен только в момент рендера (см. i18n.current_lang) —
    если бы title/description были обычными строковыми полями, весь каталог
    навсегда застыл бы на языке того, кто первым дёрнул этот модуль после
    старта процесса.
    """
    code: str
    emoji: str

    @property
    def title(self) -> str:
        return i18n.t(f"achievement.{self.code}.title")

    @property
    def description(self) -> str:
        return i18n.t(f"achievement.{self.code}.description")


# Ordered for display (easiest → rarest within each theme). Текст — в
# locales/*.json по ключам achievement.<code>.title/description.
CATALOG: list[Achievement] = [
    Achievement("first", "🌱"),
    Achievement("w10", "🔟"),
    Achievement("w25", "💪"),
    Achievement("w50", "🏋️"),
    Achievement("w100", "💯"),
    Achievement("streak4", "📅"),
    Achievement("streak12", "🔥"),
    Achievement("streak26", "❄️"),
    Achievement("streak52", "🎖"),
    Achievement("club100", "🥉"),
    Achievement("club140", "🥈"),
    Achievement("club180", "🥇"),
    Achievement("club220", "🏅"),
    Achievement("ton10", "🪨"),
    Achievement("ton50", "🚚"),
    Achievement("ton100", "🐘"),
    Achievement("ton500", "🚂"),
    Achievement("ton1000", "🐋"),
    Achievement("w200", "🗿"),
    Achievement("w500", "🏛"),
    Achievement("variety20", "🎨"),
    Achievement("variety50", "🧰"),
    Achievement("groups6", "🕸"),
    Achievement("vol25", "🧨"),
    Achievement("session5t", "🏗"),
    Achievement("combine8", "🚜"),
    Achievement("superset1", "🔀"),
    Achievement("bw25", "🤸"),
    Achievement("weekend_double", "⚔️"),
    Achievement("all_weekdays", "🗓"),
    Achievement("early_bird", "🌅"),
    Achievement("early10", "🌄"),
    Achievement("night_owl", "🦉"),
    Achievement("marathon", "⏳"),
    Achievement("bwlog30", "📏"),
    Achievement("food7", "🍱"),
    Achievement("new_year", "🎄"),
    Achievement("dec31", "🎇"),
]

BY_CODE: dict[str, Achievement] = {a.code: a for a in CATALOG}

# Thresholds (kg for weight, kg for tonnage, weeks, count).
_WORKOUT_TIERS = [
    (1, "first"), (10, "w10"), (25, "w25"), (50, "w50"), (100, "w100"),
    (200, "w200"), (500, "w500"),
]
_VARIETY_TIERS = [(20, "variety20"), (50, "variety50")]
_STREAK_TIERS = [(4, "streak4"), (12, "streak12"), (26, "streak26"), (52, "streak52")]
_WEIGHT_TIERS = [(100, "club100"), (140, "club140"), (180, "club180"), (220, "club220")]
_TONNAGE_TIERS = [
    (10_000, "ton10"), (50_000, "ton50"), (100_000, "ton100"),
    (500_000, "ton500"), (1_000_000, "ton1000"),
]


@dataclass
class AchievementContext:
    total_workouts: int
    lifetime_tonnage_kg: float
    best_week_streak: int
    max_weight_kg: float
    distinct_exercises: int
    # --- lifetime aggregates for the newer badge families. All of these are
    # "best/count ever" numbers on purpose: an aggregate survives resync (the
    # full recomputation after an edit/delete) without walking workouts one by
    # one, and a per-session record like "25 подходов за тренировку" IS a
    # lifetime max — of a per-session number.
    distinct_groups: int = 0
    max_session_sets: int = 0
    max_session_tonnage_kg: float = 0.0
    max_session_exercises: int = 0
    has_superset: bool = False
    max_bodyweight_reps: int = 0
    early_workouts: int = 0
    has_weekend_pair: bool = False
    all_weekdays_covered: bool = False
    has_dec31: bool = False
    bodyweight_logs: int = 0
    food_diary_best_run: int = 0
    # Attributes of the workout that just finished (None when evaluating aggregates only).
    workout_start_hour: Optional[int] = None
    workout_date: Optional[dt.date] = None
    workout_duration_seconds: Optional[float] = None


def weekend_pair_exists(dates: list[dt.date]) -> bool:
    """Суббота и воскресенье одной и той же недели — обе с тренировкой."""
    days = set(dates)
    return any(d.weekday() == 5 and d + dt.timedelta(days=1) in days for d in days)


def longest_daily_run(dates: list[dt.date]) -> int:
    """Длина самой длинной цепочки дней подряд (для «Недели учёта»)."""
    days = sorted(set(dates))
    best = run = 0
    prev: Optional[dt.date] = None
    for d in days:
        run = run + 1 if prev is not None and (d - prev).days == 1 else 1
        best = max(best, run)
        prev = d
    return best


def earned_codes(ctx: AchievementContext) -> set[str]:
    """All achievement codes the given state qualifies for (held or not)."""
    codes: set[str] = set()
    for n, code in _WORKOUT_TIERS:
        if ctx.total_workouts >= n:
            codes.add(code)
    for n, code in _STREAK_TIERS:
        if ctx.best_week_streak >= n:
            codes.add(code)
    for kg, code in _WEIGHT_TIERS:
        # Допуск на округление при переключении кг↔lb: живой прогон снял
        # «Клуб 180» у человека, который реально поднимал 180кг — 180 * lb/kg,
        # округлённые до 0.1 при хранении в lb (db.scale_user_set_weights), и
        # обратно в kg дают 179.99, а не 180.0. Единица измерения — это то, как
        # число ПОКАЗЫВАЮТ, а не то, что человек поднял; предпочтение в
        # отображении не должно стоить уже заработанного значка. 0.05кг —
        # больше любой возможной ошибки одного шага округления (максимум
        # ~0.023кг на round(x, 1) в lb).
        if ctx.max_weight_kg >= kg - 0.05:
            codes.add(code)
    for kg, code in _TONNAGE_TIERS:
        if ctx.lifetime_tonnage_kg >= kg:
            codes.add(code)
    for n, code in _VARIETY_TIERS:
        if ctx.distinct_exercises >= n:
            codes.add(code)
    if ctx.distinct_groups >= 6:
        codes.add("groups6")
    if ctx.max_session_sets >= 25:
        codes.add("vol25")
    if ctx.max_session_tonnage_kg >= 5_000:
        codes.add("session5t")
    if ctx.max_session_exercises >= 8:
        codes.add("combine8")
    if ctx.has_superset:
        codes.add("superset1")
    if ctx.max_bodyweight_reps >= 25:
        codes.add("bw25")
    if ctx.early_workouts >= 10:
        codes.add("early10")
    if ctx.has_weekend_pair:
        codes.add("weekend_double")
    if ctx.all_weekdays_covered:
        codes.add("all_weekdays")
    if ctx.has_dec31:
        codes.add("dec31")
    if ctx.bodyweight_logs >= 30:
        codes.add("bwlog30")
    if ctx.food_diary_best_run >= 7:
        codes.add("food7")
    if ctx.workout_start_hour is not None:
        if ctx.workout_start_hour < 7:
            codes.add("early_bird")
        if ctx.workout_start_hour >= 22:
            codes.add("night_owl")
    if ctx.workout_duration_seconds is not None and ctx.workout_duration_seconds >= 2 * 3600:
        codes.add("marathon")
    if ctx.workout_date is not None and (ctx.workout_date.month, ctx.workout_date.day) == (1, 1):
        codes.add("new_year")
    return codes
