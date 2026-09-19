"""Подсказка прогрессии — «🎯 Цель: 82.5×8» под строкой «Прошлый раз».

Один расчёт на бота и на приложение. Бот собирает входные данные из FSM,
где они уже закэшированы (экран записи перерисовывается на каждый подход, и
перечитывать всю историю упражнения каждый раз незачем), приложение — из
базы; но РЕШЕНИЕ, что предложить и как это назвать, живёт здесь одно.

Почему это важно именно тут. Подсказка — не украшение, а то, ради чего
дневник и ведут: она говорит, с чем подходить к снаряду в следующий раз.
Посчитать её на клиенте вторым кодом значило бы завести второе мнение о
весе на штанге — и рано или поздно бот и приложение назвали бы разные
числа одному и тому же человеку.
"""

from __future__ import annotations

from typing import Any, Optional

import analytics
import db
import formatting
import i18n


def hint(
    last_session: list[tuple[float, int, float | None]],
    today_sets: list[tuple[float, int]],
    *,
    unit: str,
    formula: str,
    inferred_step: Optional[float] = None,
    rule: Optional[dict] = None,
    target: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Что предложить в следующем подходе, готовой строкой и числами.

    `None`, когда предлагать нечего: истории нет вовсе или
    `analytics.suggest_progression` не нашла осмысленного шага.

    `achieved` — цель уже взята сегодня: бот в этом случае пишет не «возьми»,
    а «взял», и приложение обязано говорить так же.
    """
    if not last_session:
        return None
    working_sets = [(weight, reps) for weight, reps, _rpe in last_session]
    suggestion = analytics.suggest_progression(
        working_sets,
        unit=unit,
        inferred_step=inferred_step,
        formula=formula,
        rule=rule,
        planned_reps=formatting.planned_rep_range(target),
    )
    if suggestion is None:
        return None
    achieved = any(
        weight >= suggestion.target_weight and reps >= suggestion.target_reps
        for weight, reps in (today_sets or [])
    )
    return {
        "text": formatting.format_progression_hint(suggestion, achieved),
        "achieved": achieved,
        "target_weight": round(suggestion.target_weight, 2),
        "target_reps": suggestion.target_reps,
        "is_bodyweight": suggestion.is_bodyweight,
    }


async def hint_for_workout(
    workout_id: int, exercise_id: int, user
) -> Optional[dict[str, Any]]:
    """То же самое, но входные данные собираются из базы.

    Для приложения: у него нет FSM бота, зато есть те же таблицы. Правила
    отказа — ровно как у бота:

    - выключенный тумблер (`users.progression_hint_enabled`) прячет подсказку
      целиком, а не приглушает её;
    - у занесения задним числом подсказки нет: заднее число — это перенос уже
      случившегося, а не решение, с чем подходить к снаряду.
    """
    if not user["progression_hint_enabled"]:
        return None
    workout = await db.get_workout(workout_id)
    if workout is None or workout["status"] == "backfill":
        return None

    rows = await db.list_sets_for_exercise(exercise_id)
    if not rows:
        return None
    last_workout_id = rows[-1]["workout_id"]
    # Подходы ПРОШЛОЙ законченной тренировки с этим упражнением. Если последняя
    # в истории — текущая, отталкиваться надо от предыдущей: сравнивать
    # сегодняшний подход с самим собой бессмысленно.
    if last_workout_id == workout_id:
        earlier = [r for r in rows if r["workout_id"] != workout_id]
        if not earlier:
            return None
        last_workout_id = earlier[-1]["workout_id"]
    last_session = [
        (r["weight"], r["reps"], r["rpe"]) for r in rows if r["workout_id"] == last_workout_id
    ]
    step = analytics.infer_weight_step(r["weight"] for r in rows)

    today = await db.list_sets_for_workout_exercise(workout_id, exercise_id)
    today_sets = [(r["weight"], r["reps"]) for r in today]
    targets = await db.workout_exercise_targets(workout_id)
    rule = await db.progression_rule_for_workout(workout_id, exercise_id)

    with i18n.use_lang(user["lang"]):
        return hint(
            last_session,
            today_sets,
            unit=user["unit"],
            formula=user["e1rm_formula"],
            inferred_step=step,
            rule=rule,
            target=targets.get(exercise_id),
        )
