"""Карточка прогресса под ответом тренера в приложении.

На готовый вопрос «Как мой прогресс?» тренер раньше отвечал стеной текста:
по строке «e1RM 70→84.6kg (+21%) с 16 окт по 9 нояб. Дальше: 72.5×6» на
каждое движение. Всё верно, но читается как выписка из банка. Теперь цифры
едут рядом с ответом машинной частью (`progress` в ответе `/v1/ai/ask`), а
приложение рисует их таблицей: движение, мини-график e1RM, прирост, цель.
Тренер в это время пишет короче — общий вывод и главный шаг
(`ai_trainer.APP_PROGRESS_CARD_NOTE`).

Цифры — те же, что тренер видит в своём отчёте (`coach_facts.progress_report`):
то же окно `CARD_WINDOW_WEEKS`, тот же e1RM сессии (`_session_e1rm`). Цель —
`progression_data.hint` от последней законченной сессии, тот же расчёт, что
у строки «Цель» в тренировке, только без правила программы и без
сегодняшних подходов: тренировки ещё нет.

Текста здесь нет: имена движений — `display_name` атлета, даты — ISO,
остальное — числа. Подписи рисует приложение на своём языке.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

import analytics
import coach_facts
import db
import progression_data

# Сколько движений показать строками; остальные — счётчиком «ещё N».
MAX_LIFTS = 6


async def _next_target(exercise_id: int, data: coach_facts.CoachData) -> Optional[dict[str, Any]]:
    """Цель на следующую тренировку — или None, если предлагать нечего.

    Выключенный тумблер подсказок (`users.progression_hint_enabled`) прячет и
    её: это та же строка «Цель», что в тренировке, только раньше."""
    if not data.user["progression_hint_enabled"]:
        return None
    rows = await db.list_sets_for_exercise(exercise_id)
    if not rows:
        return None
    last_workout_id = rows[-1]["workout_id"]
    last_session = [
        (r["weight"], r["reps"], r["rpe"]) for r in rows if r["workout_id"] == last_workout_id
    ]
    suggestion = progression_data.hint(
        last_session,
        [],
        unit=data.unit,
        formula=data.formula,
        inferred_step=analytics.infer_weight_step(r["weight"] for r in rows),
        kind=await db.exercise_progression_kind(exercise_id),
        rep_range=data.rep_range,
    )
    # Секунды у планки — не «вес×повторы»; такой цели в таблице не место.
    if suggestion is None or suggestion["is_timed"]:
        return None
    return {
        "weight": suggestion["target_weight"],
        "reps": suggestion["target_reps"],
        "bodyweight": bool(suggestion["is_bodyweight"]),
    }


async def card_json(user_id: int) -> Optional[dict[str, Any]]:
    """`progress` для ответа тренера; None — показывать нечего (за окно ни
    одного движения с весом)."""
    data = await coach_facts.load_context(user_id)
    if data is None:
        return None
    since = (data.today - dt.timedelta(weeks=coach_facts.CARD_HISTORY_WEEKS)).isoformat()
    window_from = data.today - dt.timedelta(weeks=coach_facts.CARD_WINDOW_WEEKS)
    candidates: list[tuple[int, str, coach_facts.Movement, list[tuple[dt.date, float]]]] = []
    for m in coach_facts.movements_from_rows(await db.list_sets_for_coach(user_id, since)):
        points = [
            (s.day, coach_facts._session_e1rm(s, data.formula))
            for s in m.sessions if s.sets and s.day >= window_from
        ]
        points = [(day, value) for day, value in points if value > 0]
        if points:
            candidates.append((len(points), m.name, m, points))
    if not candidates:
        return None
    # Что чаще делается — выше: это и есть «мои упражнения».
    candidates.sort(key=lambda c: (-c[0], c[1]))
    lifts: list[dict[str, Any]] = []
    for _count, _name, m, points in candidates[:MAX_LIFTS]:
        first, last = points[0][1], points[-1][1]
        change = round((last - first) / first * 100) if len(points) >= 2 else None
        lifts.append({
            "exercise_id": m.exercise_id,
            "name": m.name,
            "points": [{"date": day.isoformat(), "e1rm": round(value, 1)} for day, value in points],
            "e1rm": round(last, 1),
            "change_pct": change,
            "next": await _next_target(m.exercise_id, data),
        })
    return {
        "window_weeks": coach_facts.CARD_WINDOW_WEEKS,
        "unit": data.unit,
        "lifts": lifts,
        "more": max(0, len(candidates) - MAX_LIFTS),
    }
