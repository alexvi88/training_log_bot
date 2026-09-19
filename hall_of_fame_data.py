"""Зал славы — сбор данных, отдельно от их рисования.

Бот показывает зал славы текстом внутри экрана «🏆 Достижения»
(handlers/history.py.build_hall_of_fame_text), приложение будет рисовать те же
цифры нативно через `/v1`, а считаются они одинаково: личные рекорды по
каждому упражнению, общий тоннаж со шуткой-эквивалентом, лучшая серия недель
подряд, самая длинная тренировка, звание. Поэтому сбор живёт здесь, а не в
handlers/history.py, — тем же приёмом, что и dashboard_data.collect
/ progress_data.load_sessions: вторая реализация того же расчёта в REST-слое
разъехалась бы с первой молча, и «личный рекорд» на экране бота и в
приложении рано или поздно назвали бы разные числа.

Текст (шутка-эквивалент тоннажа, названия) собирается уже здесь и уже
локализованным — как и в dashboard_data: строка живёт в locales/*.json одним
экземпляром, а не второй раз внутри старой версии приложения.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

import analytics
import config
import db
import formatting
import timeutil
import view_builder


@dataclass(frozen=True)
class HallOfFame:
    """Всё, из чего строится зал славы — и текст бота, и JSON приложения."""

    total_workouts: int
    tonnage_kg: float  # в единицах пользователя, несмотря на имя (см. formatting.build_hall_of_fame)
    tonnage_equivalent: Optional[str]
    best_week_streak: int
    longest_workout_seconds: float
    #: (имя упражнения, вес лучшего подхода, повторы, e1RM); вес 0 — свой вес
    top_lifts: list[tuple[str, float, int, float]] = field(default_factory=list)
    unit: str = "kg"
    rank: "analytics.Rank | None" = None
    rank_gap: "analytics.RankGap | None" = None


async def _top_lifts(user_id: int, formula: str) -> list[tuple[str, float, int, float]]:
    """Best working set per exercise, strongest first — for the Hall of Fame.

    Every exercise the user has ever logged gets a line, including bodyweight
    ones: those have no load to rank by, so their record is the best set of reps
    and they follow the weighted lifts (weight 0 marks them for the formatter).
    The list isn't capped here — the caller (formatter or JSON endpoint) trims
    whatever doesn't fit.
    """
    weighted: list[tuple[str, float, int, float]] = []
    bodyweight: list[tuple[str, float, int, float]] = []

    # One query for every set the user owns, then grouped here — the per-exercise
    # version cost a round-trip per exercise ever created (see list_all_sets_by_exercise).
    by_exercise: dict[int, tuple[str, list[analytics.SetRow]]] = {}
    for r in await db.list_all_sets_by_exercise(user_id):
        entry = by_exercise.get(r["exercise_id"])
        if entry is None:
            entry = by_exercise[r["exercise_id"]] = (r["display_name"], [])
        entry[1].append(
            analytics.SetRow(
                db.load_of(r), r["reps"], r["workout_id"], r["started_at"], r["rpe"]
            )
        )

    for display_name, set_rows in by_exercise.values():
        sessions = analytics.group_sets_by_session(set_rows)
        for s in sessions:
            s.formula = formula
        pr = analytics.compute_personal_records(sessions)
        if pr.max_e1rm > 0 and pr.best_e1rm_weight > 0:
            weighted.append((display_name, pr.best_e1rm_weight, pr.best_e1rm_reps, pr.max_e1rm))
        elif pr.max_reps_at_weight:
            best_reps = max(pr.max_reps_at_weight.values())
            bodyweight.append((display_name, 0.0, best_reps, 0.0))
    weighted.sort(key=lambda t: t[3], reverse=True)
    bodyweight.sort(key=lambda t: t[2], reverse=True)
    return weighted + bodyweight


async def collect(user_id: int) -> HallOfFame:
    """Зал славы пользователя — по всей истории целиком.

    Пустая история (`total_workouts == 0`) не падает и не идёт отдельной
    веткой: все остальные поля у новичка честно нулевые/пустые, и вызывающий
    (бот или REST) сам решает, как это показать — бот печатает
    `hall.empty` внутри formatting.build_hall_of_fame, а `/v1` отдаёт `null`
    целиком, тем же приёмом, что и dashboard_data.collect.
    """
    user = await db.get_user(user_id)
    formula = user["e1rm_formula"] if user else config.DEFAULT_E1RM_FORMULA
    unit = user["unit"] if user else "kg"
    total_workouts = await db.count_workouts(user_id)
    agg = await db.hall_of_fame_aggregates(user_id)
    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id)]
    best_streak = analytics.max_week_streak(dates)
    top = await _top_lifts(user_id, formula)
    equivalent = formatting.format_tonnage_equivalent(agg["tonnage"], seed=user_id, unit=unit)
    tonnage_kg = formatting.to_kg(agg["tonnage"], unit)
    per_week = analytics.workouts_per_week(dates, timeutil.user_today(user) if user else dt.date.today())
    rank = analytics.rank_for(total_workouts, tonnage_kg, per_week)
    return HallOfFame(
        total_workouts=total_workouts,
        tonnage_kg=agg["tonnage"],
        tonnage_equivalent=equivalent,
        best_week_streak=best_streak,
        longest_workout_seconds=await view_builder.longest_workout_seconds(user_id),
        top_lifts=top,
        unit=unit,
        rank=rank,
        rank_gap=analytics.rank_gap(rank, total_workouts, tonnage_kg, per_week),
    )
