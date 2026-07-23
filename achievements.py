"""Achievement catalog and pure detection logic.

Kept free of DB/Telegram so the "which badges does this state earn?" rule is
trivially testable. handlers/workout.py builds an AchievementContext at finish
time, awards the new codes (db.award_achievements) and celebrates them on the
completion card; handlers/history.py renders the full grid.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Achievement:
    code: str
    emoji: str
    title: str
    description: str


# Ordered for display (easiest → rarest within each theme).
CATALOG: list[Achievement] = [
    Achievement("first", "🌱", "Первый шаг", "Первая тренировка в дневнике"),
    Achievement("w10", "🔟", "Разминка окончена", "10 тренировок"),
    Achievement("w25", "💪", "Втянулся", "25 тренировок"),
    Achievement("w50", "🏋️", "Полтинник", "50 тренировок"),
    Achievement("w100", "💯", "Сотка", "100 тренировок"),
    Achievement("streak4", "📅", "Месяц в строю", "4 недели подряд"),
    Achievement("streak12", "🔥", "Квартал без пропусков", "12 недель подряд"),
    Achievement("streak26", "❄️", "Полгода дисциплины", "26 недель подряд"),
    Achievement("streak52", "🎖", "Год под грифом", "52 недели подряд"),
    Achievement("club100", "🥉", "Клуб 100", "Поднял 100 кг в одном подходе"),
    Achievement("club140", "🥈", "Клуб 140", "Поднял 140 кг в одном подходе"),
    Achievement("club180", "🥇", "Клуб 180", "Поднял 180 кг в одном подходе"),
    Achievement("club220", "🏅", "Клуб 220", "Поднял 220 кг в одном подходе"),
    Achievement("ton10", "🪨", "10 тонн", "10 т суммарно за всё время"),
    Achievement("ton50", "🚚", "50 тонн", "50 т суммарно за всё время"),
    Achievement("ton100", "🐘", "100 тонн", "100 т суммарно за всё время"),
    Achievement("ton500", "🚂", "Товарный состав", "500 т суммарно за всё время"),
    Achievement("ton1000", "🐋", "Синий кит", "1000 т суммарно за всё время"),
    Achievement("variety20", "🎨", "Коллекционер", "20 разных упражнений"),
    Achievement("early_bird", "🌅", "Ранняя пташка", "Тренировка до 7 утра"),
    Achievement("night_owl", "🦉", "Ночная смена", "Тренировка после 22:00"),
    Achievement("marathon", "⏳", "Марафонец", "Тренировка длиннее 2 часов"),
    Achievement("new_year", "🎄", "С Новым годом!", "Тренировка 1 января"),
]

BY_CODE: dict[str, Achievement] = {a.code: a for a in CATALOG}

# Thresholds (kg for weight, kg for tonnage, weeks, count).
_WORKOUT_TIERS = [(1, "first"), (10, "w10"), (25, "w25"), (50, "w50"), (100, "w100")]
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
    # Attributes of the workout that just finished (None when evaluating aggregates only).
    workout_start_hour: Optional[int] = None
    workout_date: Optional[dt.date] = None
    workout_duration_seconds: Optional[float] = None


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
        if ctx.max_weight_kg >= kg:
            codes.add(code)
    for kg, code in _TONNAGE_TIERS:
        if ctx.lifetime_tonnage_kg >= kg:
            codes.add(code)
    if ctx.distinct_exercises >= 20:
        codes.add("variety20")
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
