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

import re
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
    kind: str = "weight",
    is_deload: bool = False,
    rep_range: Optional[tuple[int, int]] = None,
) -> Optional[dict[str, Any]]:
    """Что предложить в следующем подходе, готовой строкой и числами.

    `is_deload` — тренировка идёт в неделю разгрузки своей программы
    (db.deload_week_for_workout): цель без прибавки, на ~90 % веса, и строка
    говорит срезать подходы.

    `role` в ответе — "top"/"backoff" у схемы «топ-сет + бэкоффы»
    (analytics.suggest_top_set_backoff), иначе None.

    `kind` — вид нагрузки упражнения (db.exercise_progression_kind): у планки
    цель в секундах, у подтягиваний и скручиваний — без шага в кг из программы.

    `rep_range` — диапазон повторов по умолчанию из настроек атлета
    (analytics.user_rep_range); схема программы в `target` его перекрывает.

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
        kind=kind,
        today_sets=today_sets,
        last_rpes=[rpe for _weight, _reps, rpe in last_session],
        default_range=rep_range,
    )
    if suggestion is None:
        return None
    if is_deload:
        suggestion = analytics.deload_suggestion(suggestion, unit=unit, inferred_step=inferred_step)
    meeting = [
        (weight, reps) for weight, reps in (today_sets or [])
        if weight >= suggestion.target_weight and reps >= suggestion.target_reps
    ]
    # У бэкоффа сам топ-сет тяжелее цели и проходит по числам — он не в счёт:
    # бэкоффы взяты, когда после него набралось столько подходов, сколько
    # просит программа (без числа в правиле — хотя бы один).
    achieved = (
        len(meeting) - 1 >= (suggestion.backoff_sets or 1)
        if suggestion.role == "backoff" else bool(meeting)
    )
    return {
        "text": formatting.format_progression_hint(suggestion, achieved, deload=is_deload),
        "achieved": achieved,
        "role": suggestion.role,
        "is_deload": bool(is_deload),
        "target_weight": round(suggestion.target_weight, 2),
        "target_reps": suggestion.target_reps,
        "is_bodyweight": suggestion.is_bodyweight,
        # Планка и прочее на время: target_reps тогда — секунды, а не повторы.
        "is_timed": suggestion.is_timed,
    }


_SCHEME_REPS = re.compile(r"\d+\s*[x\u00d7\u0445]\s*(\d+)(?:\s*[-–]\s*(\d+))?")


def _scheme_rep_range(target: str) -> Optional[tuple[int, int]]:
    """Диапазон повторов из схемы подходов («3×5–12» → (5, 12)). Одно число
    («3×8») диапазона не даёт — тогда None и берётся диапазон из настройки."""
    match = _SCHEME_REPS.search(target)
    if match is None or match.group(2) is None:
        return None
    low, high = int(match.group(1)), int(match.group(2))
    return (low, high) if low < high else None


def no_history_hint(
    *,
    kind: str = "weight",
    rule: Optional[dict] = None,
    target: Optional[str] = None,
    rep_range: Optional[tuple[int, int]] = None,
    keep_plan: bool = False,
) -> Optional[dict[str, Any]]:
    """«🎯 Цель» для упражнения, в котором у атлета ещё нет ни одного подхода.

    Числа предлагать не от чего, поэтому строка называет не вес, а способ его
    найти: вес, с которым выйдет столько повторов, сколько стоит в настройке
    «Диапазон повторов» (`analytics.user_rep_range`, по умолчанию 5–12).

    `None` там, где у упражнения уже есть своя цель или слово «вес» не к месту:
    - схема подходов в карточке (`target`) и правило прогрессии программы
      (`rule`) — это цель, и они главнее (как в `analytics.suggest_progression`);
    - не "weight" (планка на время, подтягивания и скручивания без отягощения) —
      «вес, с которым сделаешь N раз» там неправда.

    Форма ответа — та же, что у `hint`, плюс `"no_history": True`: старые сборки
    приложения читают `text` и числа, как раньше, и показывают строку в том же
    месте; `target_weight` у неё 0 — клиенты его не используют, а цели в нём нет.

    `keep_plan=True` — для приложения: схема подходов («План: 3×5–12») говорит,
    сколько и по скольку, но не как подобрать вес, а новичку по программе, где
    истории ещё нет, нужна именно эта подсказка (владелец: «где наша подсказка
    про цель?»). Тогда схема и правило её не прячут, а диапазон повторов в
    строке берётся из схемы, если там диапазон («3×5–12» → «5–12 раз»), чтобы
    две строки подряд не называли разные числа. Бот зовёт без флага: у него
    план и так показан отдельной строкой выше.
    """
    if kind != "weight":
        return None
    if not keep_plan and (rule or target):
        return None
    scheme_range = _scheme_rep_range(target) if (keep_plan and target) else None
    low, high = scheme_range or rep_range or (analytics.REP_RANGE_MIN, analytics.REP_RANGE_MAX)
    return {
        "text": i18n.t("progression.goal_no_history", min=low, max=high),
        "achieved": False,
        "role": None,
        "is_deload": False,
        "target_weight": 0.0,
        "target_reps": high,
        "is_bodyweight": False,
        "is_timed": False,
        "no_history": True,
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
    last_workout_id = rows[-1]["workout_id"] if rows else None
    # Подходы ПРОШЛОЙ законченной тренировки с этим упражнением. Если последняя
    # в истории — текущая, отталкиваться надо от предыдущей: сравнивать
    # сегодняшний подход с самим собой бессмысленно.
    if last_workout_id == workout_id:
        earlier = [r for r in rows if r["workout_id"] != workout_id]
        last_workout_id = earlier[-1]["workout_id"] if earlier else None
    if last_workout_id is None:
        # Истории нет. Строка только до первого подхода: записал — цель
        # «вес на N–M раз» уже выполнена самим подходом, и она уходит.
        today = await db.list_sets_for_workout_exercise(workout_id, exercise_id)
        if rows or today:
            return None
        # Схема — из дня программы: подходов сегодня ещё нет, так что
        # workout_exercise_targets (он читает записанные подходы) здесь пуст.
        routine_id = workout["routine_id"]
        planned = {
            r["exercise_id"]: r["target"]
            for r in (await db.list_routine_exercises(routine_id) if routine_id else [])
        }
        with i18n.use_lang(user["lang"]):
            return no_history_hint(
                kind=await db.exercise_progression_kind(exercise_id),
                rule=await db.progression_rule_for_workout(workout_id, exercise_id),
                target=planned.get(exercise_id),
                rep_range=analytics.user_rep_range(user),
                keep_plan=True,
            )
    last_session = [
        (r["weight"], r["reps"], r["rpe"]) for r in rows if r["workout_id"] == last_workout_id
    ]
    step = analytics.infer_weight_step(r["weight"] for r in rows)

    today = await db.list_sets_for_workout_exercise(workout_id, exercise_id)
    today_sets = [(r["weight"], r["reps"]) for r in today]
    targets = await db.workout_exercise_targets(workout_id)
    rule = await db.progression_rule_for_workout(workout_id, exercise_id)
    kind = await db.exercise_progression_kind(exercise_id)
    is_deload = await db.deload_week_for_workout(workout_id)

    with i18n.use_lang(user["lang"]):
        return hint(
            last_session,
            today_sets,
            unit=user["unit"],
            formula=user["e1rm_formula"],
            inferred_step=step,
            rule=rule,
            target=targets.get(exercise_id),
            kind=kind,
            is_deload=is_deload,
            rep_range=analytics.user_rep_range(user),
        )
