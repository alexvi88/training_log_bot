"""Экран прогресса упражнения — сбор данных, отдельно от их рисования.

Бот показывает его текстом и картинкой (handlers/history.py,
charts.render_metric_over_sessions), приложение рисует график нативно, а
считаться это обязано одинаково: точка на ТРЕНИРОВКУ (не на подход) и по оси
e1RM (не сырой вес снаряда). Пока расчёт жил внутри aiogram-хендлера,
приложению оставалось считать его у себя заново — и оно считало неправильно
все три года: пилило линию внутри одной тренировки и рисовало вес блина
вместо оценки максимума. Поэтому сбор переехал сюда, как раньше переехала
сводка главного экрана (dashboard_data.py): один расчёт на обоих
потребителей, разойтись им нечем.

Текста модуль не держит вовсе — только числа и сессии. Подписи (метрика,
единица, строка дельты) собирают formatting/i18n на стороне того, кто
показывает, уже внутри i18n.use_lang(users.lang).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import analytics
import db


async def load_sessions(exercise_id: int, formula: str) -> list[analytics.SessionStats]:
    """Вся история упражнения по законченным тренировкам, старые сначала,
    сгруппированная в сессии.

    `formula` (users.e1rm_formula) проставляется каждой сессии здесь, а не в
    месте показа: без неё SessionStats.top_e1rm посчитается по умолчанию
    (Эпли) — и атлет, выбравший Бжицкого, увидел бы на графике чужие числа.

    Арифметика идёт по db.load_of, а не по `weight`: у подходов со своим весом
    и с резиной записанный вес и реальная нагрузка — разные величины.
    """
    rows = await db.list_sets_for_exercise(exercise_id)
    set_rows = [
        analytics.SetRow(db.load_of(r), r["reps"], r["workout_id"], r["started_at"], r["rpe"])
        for r in rows
    ]
    sessions = analytics.group_sets_by_session(set_rows)
    for s in sessions:
        s.formula = formula
    return sessions


@dataclass(frozen=True)
class ChartSeries:
    """Ряд для графика: одна точка на тренировку, все точки — об одной величине."""

    #: True — метрика «повторы» (упражнение своим весом), False — «e1RM в кг/lb»
    is_bodyweight: bool
    #: сессии, попавшие в ряд, старые сначала — по ним подписываются точки
    sessions: list[analytics.SessionStats]
    #: (когда, значение) — ровно то, что уезжает в analytics.linear_trend и в charts
    points: list[tuple[dt.datetime, float]]


def chart_series(sessions: list[analytics.SessionStats]) -> ChartSeries:
    """Точки графика по истории упражнения — ОДНА на тренировку.

    Значение точки — лучший подход тренировки: e1RM (или максимум повторов у
    упражнения своим весом), а не вес снаряда. Точка на каждый подход
    превращала бы линию в пилу внутри одного дня — три подхода одной
    тренировки это не три шага прогресса, а один.

    У упражнения, сменившего режим (подтягивания с весом → своим весом), в
    истории живут две несопоставимые величины: килограммы e1RM и голые
    повторы. На одной оси они читаются как обвал силы — 110 и 12 рядом.
    Поэтому в ряд попадают только сессии того же режима, что последняя:
    график остаётся про одну величину. Рекорды обоих режимов при этом никуда
    не деваются — их показывают текстом (formatting.format_progress_screen).
    """
    is_bw = sessions[-1].is_bodyweight_mode if sessions else False
    plotted = [s for s in sessions if s.is_bodyweight_mode == is_bw]
    points = [
        (
            dt.datetime.fromisoformat(s.started_at),
            float(s.max_reps_in_set if is_bw else s.top_e1rm),
        )
        for s in plotted
    ]
    return ChartSeries(is_bodyweight=is_bw, sessions=plotted, points=points)
