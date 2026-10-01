"""Главный факт только что законченной тренировки — заголовок листа итогов.

Лист итогов в приложении раньше показывал крупно тоннаж («1.0т») и штангу, а
что человек сделал и как это против прошлого раза — нигде. Момент «я молодец»
не случался (разбор UI, A-04). Теперь заголовок — один факт, первый по списку,
который подтверждают данные:

1. рекорд в упражнении (e1RM или повторы своим весом);
2. прибавка к прошлому разу в том же упражнении — вес или повторы;
3. серия недель продлилась этой тренировкой;
4. возвращение после перерыва;
5. первая тренировка — вообще или во всех упражнениях сразу;
6. ничего из этого — главное упражнение и его лучший подход.

Решение, какой факт главный, живёт здесь, а не в приложении: «рекорд» и
«прошлый раз» сервер уже считает для карточки бота (view_builder с
`mark_records`), и второе мнение о них на клиенте рано или поздно назвало бы
человеку другое число. Тексты — из locales/*.json, уже на языке атлета.

Тренировку легче прошлой не ругаем: подколки — только за пропуски
(TONE_OF_VOICE.md), поэтому у слабее прошлого раза — поддержка.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Optional

import analytics
import db
import formatting
import i18n
import timeutil
from formatting import ExerciseBlockView

# Сколько дней без тренировки — уже перерыв. Тот же порог, что у первого
# «пропал на неделю» пуша (skip_7): раньше тренер про перерыв не говорит.
COMEBACK_GAP_DAYS = 7

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class _Gain:
    block: ExerciseBlockView
    kind: str  # "weight" | "reps"
    delta: float
    now: tuple[float, int]
    prev: tuple[float, int]


def _top_set(sets: list[tuple[float, int]]) -> tuple[float, int]:
    """Самый тяжёлый подход, при равном весе — с большим числом повторов."""
    return max(sets, key=lambda s: (s[0], s[1]))


def _main_order(blocks: list[ExerciseBlockView]) -> list[ExerciseBlockView]:
    """Главное упражнение — где подходов больше всего; при равенстве — что
    записано раньше. Стабильная сортировка держит порядок тренировки."""
    return sorted((b for b in blocks if b.sets), key=lambda b: -len(b.sets))


def better_set_indexes(block: ExerciseBlockView) -> list[int]:
    """Подходы, которые строго лучше подхода с тем же номером в прошлый раз:
    вес не меньше и повторов больше, или вес больше и повторов не меньше.

    Только строгое превосходство: «70×5 против 60×10» — не «лучше» без
    расчёта e1RM, а подсветка обязана быть правдой без оговорок.
    """
    if not block.prev_sets:
        return []
    out = []
    for i, (w, r) in enumerate(block.sets):
        if i >= len(block.prev_sets):
            break
        pw, pr = block.prev_sets[i]
        if (w >= pw and r > pr) or (w > pw and r >= pr):
            out.append(i)
    return out


def _gain(block: ExerciseBlockView) -> Optional[_Gain]:
    if not block.sets or not block.prev_sets:
        return None
    now = _top_set(block.sets)
    prev = _top_set(block.prev_sets)
    if block.is_bodyweight or all(w == 0 for w, _ in block.prev_sets):
        if block.is_bodyweight and now[1] > prev[1]:
            return _Gain(block, "reps", now[1] - prev[1], now, prev)
        return None
    if now[0] > prev[0] and now[1] >= prev[1]:
        return _Gain(block, "weight", now[0] - prev[0], now, prev)
    if now[0] == prev[0] and now[1] > prev[1]:
        return _Gain(block, "reps", now[1] - prev[1], now, prev)
    return None


def _set_caps(weight: float, reps: int) -> str:
    """«75 НА 5» — подход словами зала, для заголовка капсом."""
    if weight == 0:
        return i18n.t("finish.summary.reps_only", reps=reps, n=reps)
    return i18n.t("finish.summary.set", weight=formatting.format_weight(weight), reps=reps)


def _record_set(block: ExerciseBlockView) -> tuple[float, int]:
    """Подход, которым поставлен рекорд: у рекорда повторов — с наибольшим
    числом повторов, у рекорда e1RM — с наибольшим e1RM по нагрузке."""
    if block.record_reps is not None:
        return max(block.sets, key=lambda s: s[1])
    best = max(
        range(len(block.sets)),
        key=lambda i: analytics.e1rm(
            block.load_for(i), block.sets[i][1], block.formula, block.rpe_for(i)
        ),
    )
    return block.sets[best]


def headline(
    blocks: list[ExerciseBlockView],
    *,
    unit: str,
    show_extra: bool,
    total_finished: int,
    gap_days: Optional[int],
    this_week_count: int,
    week_streak: int,
    backfill: bool,
) -> dict[str, Any]:
    """{kind, kicker, title, text} — уже на текущем языке (i18n.use_lang снаружи).

    `gap_days` — дней между этой тренировкой и прошлой законченной (None —
    прошлой нет). Серия и возвращение у занесения задним числом не
    считаются: заднее число — перенос уже случившегося, а не «сейчас».
    """
    ordered = _main_order(blocks)
    u = formatting.unit_label(unit)

    def name(block: ExerciseBlockView) -> str:
        return block.exercise_name.upper()

    for block in ordered:
        if block.record_reps is not None or (block.record_e1rm_delta is not None and show_extra):
            w, r = _record_set(block)
            return {
                "kind": "record",
                "kicker": name(block),
                "title": i18n.t("finish.summary.record.title", set=_set_caps(w, r)).upper(),
                "text": i18n.t("finish.summary.record.text"),
            }

    for block in ordered:
        gain = _gain(block)
        if gain is None:
            continue
        now = formatting.format_set(*gain.now)
        prev = formatting.format_set(*gain.prev)
        if gain.kind == "weight":
            title = i18n.t(
                "finish.summary.gain_weight.title", delta=formatting.format_weight(gain.delta), u=u
            )
        else:
            n = int(gain.delta)
            title = i18n.t("finish.summary.gain_reps.title", delta=n, n=n)
        return {
            "kind": "gain",
            "kicker": name(block),
            "title": title.upper(),
            "text": i18n.t("finish.summary.gain.text", now=now, prev=prev),
        }

    if not backfill and this_week_count == 1 and week_streak >= 2:
        return {
            "kind": "streak",
            "kicker": i18n.t("finish.summary.streak.kicker"),
            "title": i18n.t("finish.summary.streak.title", weeks=week_streak, n=week_streak).upper(),
            "text": i18n.t("finish.summary.streak.text"),
        }

    if not backfill and gap_days is not None and gap_days >= COMEBACK_GAP_DAYS:
        return {
            "kind": "comeback",
            "kicker": i18n.t("finish.summary.comeback.kicker"),
            "title": i18n.t("finish.summary.comeback.title").upper(),
            "text": i18n.t("finish.summary.comeback.text", days=gap_days, n=gap_days),
        }

    if total_finished == 1 or (ordered and not any(b.prev_sets for b in ordered)):
        return {
            "kind": "first",
            "kicker": (
                i18n.t("finish.summary.first.kicker") if total_finished == 1
                else (name(ordered[0]) if ordered else None)
            ),
            "title": i18n.t("finish.summary.first.title").upper(),
            "text": i18n.t("finish.summary.first.text"),
        }

    if not ordered:
        return {"kind": "plain", "kicker": None, "title": "", "text": None}
    main = ordered[0]
    w, r = _top_set(main.sets)
    text = None
    if main.prev_sets:
        if main.top_e1rm + 0.05 < main.prev_top_e1rm:
            text = i18n.t("finish.summary.plain.text_lighter")
        elif abs(main.top_e1rm - main.prev_top_e1rm) <= 0.05:
            text = i18n.t("finish.summary.plain.text_same")
    return {
        "kind": "plain",
        "kicker": name(main),
        "title": i18n.t("finish.summary.plain.title", set=_set_caps(w, r)).upper(),
        "text": text or i18n.t("finish.summary.plain.text"),
    }


def week_strip(dates: list[dt.date], today: dt.date, week_streak: int) -> dict[str, Any]:
    """Полоса недели: семь дней с понедельника, где была тренировка, и
    подпись «2 тренировки на этой неделе · 6 недель подряд». Цели «N в
    неделю» в продукте нет, поэтому полоса считает только то, что было, а не
    «2 из 3»: доли от выдуманной цели данные не подтверждают."""
    monday = today - dt.timedelta(days=today.weekday())
    trained = set(dates)
    days = []
    for i, code in enumerate(_WEEKDAYS):
        day = monday + dt.timedelta(days=i)
        days.append({
            "label": i18n.t("date.weekday_short", wd=code),
            "done": day in trained,
            "today": day == today,
        })
    count = sum(1 for d in dates if monday <= d <= monday + dt.timedelta(days=6))
    text = i18n.t("finish.summary.week.count", count=count, n=count)
    if week_streak >= 2:
        text += " · " + i18n.t("finish.summary.week.streak", weeks=week_streak, n=week_streak)
    return {"days": days, "text": text}


async def _next_target(workout_id: int, block: ExerciseBlockView, user) -> Optional[str]:
    """«Жим лёжа — в следующий раз 62.5×8»: та же двойная прогрессия, что у
    «🎯 Цель» на экране записи (analytics.suggest_progression), только от
    сегодняшних подходов. Молчит при выключенной подсказке прогрессии — тот
    же тумблер, что у бота."""
    if not user["progression_hint_enabled"] or block.exercise_id is None:
        return None
    rows = await db.list_sets_for_exercise(block.exercise_id)
    step = analytics.infer_weight_step(r["weight"] for r in rows)
    targets = await db.workout_exercise_targets(workout_id)
    rule = await db.progression_rule_for_workout(workout_id, block.exercise_id)
    suggestion = analytics.suggest_progression(
        list(block.sets),
        unit=user["unit"],
        inferred_step=step,
        formula=user["e1rm_formula"],
        rule=rule,
        planned_reps=formatting.planned_rep_range(targets.get(block.exercise_id)),
    )
    if suggestion is None:
        return None
    if suggestion.is_bodyweight:
        goal = i18n.t("progression.goal_reps", n=suggestion.target_reps)
    else:
        goal = formatting.format_set(suggestion.target_weight, suggestion.target_reps)
    return i18n.t("finish.summary.next", exercise=block.exercise_name, goal=goal)


async def collect(
    workout, user, blocks: list[ExerciseBlockView], *, was_backfill: bool
) -> dict[str, Any]:
    """Поле `rewards.summary` ответа finish. Язык выставляет вызывающий.

    `blocks` — из view_builder.build_block_views с `previous_before` и
    `mark_records`: прошлый раз и рекорд у каждого упражнения уже посчитаны.
    """
    tz = timeutil.offset_hours(user)
    dates = [
        dt.date.fromisoformat(d)
        for d in await db.list_finished_workout_dates(workout["user_id"], tz_offset=tz)
    ]
    today = timeutil.user_today(user)
    started = dt.datetime.fromisoformat(workout["started_at"])
    this_day = timeutil.to_user_local(started, user).date()
    earlier = list(dates)
    if this_day in earlier:
        earlier.remove(this_day)
    previous = [d for d in earlier if d <= this_day]
    gap_days = (this_day - max(previous)).days if previous else None
    dashboard = analytics.compute_dashboard(dates, today)
    head = headline(
        blocks,
        unit=user["unit"],
        show_extra=bool(user["show_extra_stats"]),
        total_finished=len(dates),
        gap_days=gap_days,
        this_week_count=dashboard.this_week,
        week_streak=dashboard.week_streak,
        backfill=was_backfill,
    )
    ordered = _main_order(blocks)
    next_target = None
    if ordered and not was_backfill:
        next_target = await _next_target(workout["id"], ordered[0], user)
    return {
        **head,
        "better_sets": [
            {"exercise_id": b.exercise_id, "indexes": idx}
            for b in blocks
            if b.exercise_id is not None and (idx := better_set_indexes(b))
        ],
        "week": None if was_backfill else week_strip(dates, today, dashboard.week_streak),
        "next_target": next_target,
    }
