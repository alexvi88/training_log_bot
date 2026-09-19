"""Сводка главного экрана — сбор данных, отдельно от их рисования.

Бот показывает её картинкой (charts.render_menu_dashboard), приложение рисует
нативно, а считается она одинаково: серия, плитки, недельный объём по группам и
рост e1RM в частых движениях. Поэтому сбор живёт здесь, а не в
handlers/workout.py, — иначе у REST появилась бы вторая реализация того же, и
разойтись им было бы нечем, кроме внимательности.

Тексты собираются уже здесь и уже локализованными (i18n.t в formatting.menu_*),
как и в модуле достижений: правка формулировки в locales/ иначе расходилась бы
со старой версией приложения, в которую эту же фразу зашили бы второй раз.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

import analytics
import db
import formatting
import timeutil

# Окно роста e1RM. У истории моложе восьми недель «рост за 8 недель» врёт: вся
# история лежит внутри окна, базы ДО него нет, и плиток не бывает вовсе — не
# потому что роста не было, а потому что не с чем сравнивать. Короткое окно даёт
# свежим аккаунтам шанс увидеть плитку до восьмой недели.
LIFT_WINDOW_WEEKS = 8
LIFT_FALLBACK_WINDOW_WEEKS = 4
# Кандидатов берётся больше, чем плиток: рост считается честно (максимум ДО окна
# против максимума ВНУТРИ), и у многих частых движений он окажется нулевым —
# форматтер их отбросит. Без запаса сводка часто оставалась бы вовсе без плиток.
LIFT_CANDIDATES = 12


@dataclass(frozen=True)
class MenuDashboard:
    """Всё, что рисуется на сводке, уже в готовом к показу виде."""

    headline: str
    rank_name: str
    rank_level: int
    #: (подпись, число) или (подпись, число, приписка)
    tiles: list[tuple] = field(default_factory=list)
    volume_title: str = ""
    #: (НАЗВАНИЕ ГРУППЫ, подходов, статус)
    volume_rows: list[tuple[str, int, str]] = field(default_factory=list)
    #: (движение, «+12%», «227кг vs 220кг»)
    lift_tiles: list[tuple[str, str, str]] = field(default_factory=list)
    lifts_title: str = ""
    lifts_note: str = ""


async def collect(user_id: int) -> Optional[MenuDashboard]:
    """Сводка пользователя или `None`, если законченных тренировок ещё нет.

    `None`, а не пустая сводка: у новичка все до единого виджета пусты, и
    карточка из нулей сообщала бы только то, что она пустая. И бот, и
    приложение в этом случае показывают приглашение начать, а не таблицу.
    """
    user = await db.get_user(user_id)
    if user is None:
        return None
    today = timeutil.user_today(user)
    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id)]
    if not dates:
        return None

    window_start = today - dt.timedelta(days=analytics.VOLUME_WINDOW_DAYS - 1)
    volume_title, volume_rows = formatting.weekly_volume_panel(
        await db.weekly_volume_by_group(user_id, window_start.isoformat(), today.isoformat()),
        await db.list_muscle_groups(user_id),
    )
    formula = user["e1rm_formula"]
    tonnage = sum(
        (await db.daily_tonnage(user_id, window_start.isoformat(), today.isoformat())).values()
    )
    records = await db.e1rm_record_count(user_id, window_start.isoformat(), formula)
    dashboard = analytics.compute_dashboard(dates, today)

    # Движения — самые частые за окно, по числу тренировок. Не «базовые»: типа
    # движения в базе нет, и выбирать жим/присед/тягу пришлось бы по каталожным
    # именам, а у человека со своими названиями список оказался бы пустым.
    history_age_weeks = (today - min(dates)).days / 7
    lift_window_weeks = (
        LIFT_FALLBACK_WINDOW_WEEKS if history_age_weeks < LIFT_WINDOW_WEEKS else LIFT_WINDOW_WEEKS
    )
    lift_start = today - dt.timedelta(weeks=lift_window_weeks)
    growth: list[tuple[str, float, float]] = []
    for row in await db.top_exercises_by_frequency(
        user_id, lift_start.isoformat(), today.isoformat(), limit=LIFT_CANDIDATES
    ):
        before_max, window_max = await db.exercise_e1rm_growth(
            user_id, row["id"], lift_start.isoformat(), formula
        )
        growth.append((row["display_name"], before_max, window_max))

    agg = await db.hall_of_fame_aggregates(user_id)
    rank = analytics.rank_for(
        len(dates),
        formatting.to_kg(agg["tonnage"], user["unit"]),
        analytics.workouts_per_week(dates, today),
    )
    lift_tiles = formatting.menu_lift_tiles(growth, user["unit"])
    return MenuDashboard(
        headline=formatting.menu_headline(dashboard),
        rank_name=rank.name,
        rank_level=rank.level,
        tiles=formatting.menu_tiles(
            dashboard, tonnage, records, user["unit"], total_workouts=len(dates)
        ),
        volume_title=volume_title,
        volume_rows=volume_rows,
        lift_tiles=lift_tiles,
        lifts_title=formatting.menu_lifts_title(lift_window_weeks) if lift_tiles else "",
        lifts_note=formatting.MENU_LIFTS_NOTE,
    )
