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
from typing import Any, Iterable, Optional

import analytics
import db
import formatting
import i18n
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
    # Собственный эмодзи звания (`analytics.Rank.emoji`) — приложение ставит
    # его на плашку звания вместо общей медали, как в лестнице званий.
    rank_emoji: str = ""
    #: (подпись, число) или (подпись, число, приписка)
    tiles: list[tuple] = field(default_factory=list)
    volume_title: str = ""
    #: (НАЗВАНИЕ ГРУППЫ, подходов, статус)
    volume_rows: list[tuple[str, int, str]] = field(default_factory=list)
    #: (движение, «+12%», «227кг vs 220кг», id упражнения)
    lift_tiles: list[tuple[str, str, str, int]] = field(default_factory=list)
    lifts_title: str = ""
    lifts_note: str = ""
    #: Строка живых данных под плитками «Меню» приложения: раздел → текст или
    #: None (данных нет — строки нет, а не «0»). См. menu_lines ниже.
    menu_lines: dict[str, Optional[str]] = field(default_factory=dict)


async def collect(
    user_id: int, user: Any = None, *, exclude_workout_ids: Iterable[int] = ()
) -> Optional[MenuDashboard]:
    """Сводка пользователя или `None`, если законченных тренировок ещё нет.

    `None`, а не пустая сводка: у новичка все до единого виджета пусты, и
    карточка из нулей сообщала бы только то, что она пустая. И бот, и
    приложение в этом случае показывают приглашение начать, а не таблицу.

    `exclude_workout_ids` — тренировки, которых для сводки уже нет: приложение
    удаляет с окном «Вернуть», и пока `DELETE` ждёт, главная не должна
    считать удаляемую (было 12 тренировок — после удаления 13). Исключение
    проходит через КАЖДЫЙ агрегат ниже, иначе плитки разошлись бы между собой.
    """
    excluded = tuple(exclude_workout_ids)
    if user is None:
        user = await db.get_user(user_id)
    if user is None:
        return None
    today = timeutil.user_today(user)
    # Смещение — из уже прочитанной строки: без него каждый из агрегатов ниже
    # (а движений их до LIFT_CANDIDATES штук) перечитывал бы users.tz_offset сам.
    tz = timeutil.offset_hours(user)
    dates = [
        dt.date.fromisoformat(d)
        for d in await db.list_finished_workout_dates(
            user_id, tz_offset=tz, exclude_workout_ids=excluded
        )
    ]
    if not dates:
        return None

    window_start = today - dt.timedelta(days=analytics.VOLUME_WINDOW_DAYS - 1)
    volume_title, volume_rows = formatting.weekly_volume_panel(
        await db.weekly_volume_by_group(
            user_id, window_start.isoformat(), today.isoformat(), tz_offset=tz,
            exclude_workout_ids=excluded,
        ),
        await db.list_muscle_groups(user_id),
    )
    formula = user["e1rm_formula"]
    tonnage = sum(
        (
            await db.daily_tonnage(
                user_id, window_start.isoformat(), today.isoformat(), tz_offset=tz,
                exclude_workout_ids=excluded,
            )
        ).values()
    )
    records = await db.e1rm_record_count(
        user_id, window_start.isoformat(), formula, tz_offset=tz, exclude_workout_ids=excluded
    )
    dashboard = analytics.compute_dashboard(dates, today)

    # Движения — самые частые за окно, по числу тренировок. Не «базовые»: типа
    # движения в базе нет, и выбирать жим/присед/тягу пришлось бы по каталожным
    # именам, а у человека со своими названиями список оказался бы пустым.
    history_age_weeks = (today - min(dates)).days / 7
    lift_window_weeks = (
        LIFT_FALLBACK_WINDOW_WEEKS if history_age_weeks < LIFT_WINDOW_WEEKS else LIFT_WINDOW_WEEKS
    )
    lift_start = today - dt.timedelta(weeks=lift_window_weeks)
    growth: list[tuple[str, float, float, int]] = []
    for row in await db.top_exercises_by_frequency(
        user_id, lift_start.isoformat(), today.isoformat(), limit=LIFT_CANDIDATES, tz_offset=tz,
        exclude_workout_ids=excluded,
    ):
        before_max, window_max = await db.exercise_e1rm_growth(
            user_id, row["id"], lift_start.isoformat(), formula, tz_offset=tz,
            exclude_workout_ids=excluded,
        )
        growth.append((row["display_name"], before_max, window_max, row["id"]))

    agg = await db.hall_of_fame_aggregates(user_id, exclude_workout_ids=excluded)
    rank = analytics.rank_for(
        len(dates),
        formatting.to_kg(agg["tonnage"], user["unit"]),
        analytics.workouts_per_week(dates, today),
    )
    lift_tiles = formatting.menu_lift_tiles(growth, user["unit"])
    lines = await menu_lines(
        user_id, rank,
        total_workouts=len(dates),
        tonnage_kg=formatting.to_kg(agg["tonnage"], user["unit"]),
        per_week=analytics.workouts_per_week(dates, today),
        growth=growth, lift_tiles=lift_tiles, lift_window_weeks=lift_window_weeks, unit=user["unit"],
    )
    return MenuDashboard(
        headline=formatting.menu_headline(dashboard),
        rank_name=rank.name,
        rank_level=rank.level,
        rank_emoji=rank.emoji,
        tiles=formatting.menu_tiles(
            dashboard, tonnage, records, user["unit"], total_workouts=len(dates)
        ),
        volume_title=volume_title,
        volume_rows=volume_rows,
        lift_tiles=lift_tiles,
        lifts_title=formatting.menu_lifts_title(lift_window_weeks) if lift_tiles else "",
        lifts_note=formatting.MENU_LIFTS_NOTE,
        menu_lines=lines,
    )


async def menu_lines(
    user_id: int,
    rank: "analytics.Rank",
    *,
    total_workouts: int,
    tonnage_kg: float,
    per_week: float,
    growth: list[tuple[str, float, float, int]],
    lift_tiles: list[tuple],
    lift_window_weeks: int,
    unit: str = "kg",
) -> dict[str, Optional[str]]:
    """По строке живых данных на плитки «Меню» приложения.

    Плитки без данных выглядели одинаково и не подсказывали, куда идти
    (разбор UI, B-03). Здесь — только то, что подтверждают данные атлета
    (TONE_OF_VOICE.md, «Пуш обязан быть правдой»): нет роста — нет строки
    прогресса, а не «+0%»; до звания называется недостача, только когда
    отстаёт одна ось, — иначе «ещё 3 тренировки» было бы неправдой, ведь
    после них звание всё равно не дадут.

    История и дневник веса сюда не входят: последнюю тренировку и последний
    вес приложение и так держит у себя (`/workouts`, `/bodyweight`).
    """
    lines: dict[str, Optional[str]] = {
        "progress": None, "exercises": None, "programs": None, "achievements": None,
    }
    if lift_tiles:
        top_id = lift_tiles[0][3]
        name = next((n for n, _b, _w, ex_id in growth if ex_id == top_id), None)
        if name:
            lines["progress"] = i18n.t(
                "menu.line.progress", exercise=name, growth=lift_tiles[0][1],
                weeks=lift_window_weeks,
            )
    exercises = await db.count_user_exercises(user_id)
    if exercises:
        lines["exercises"] = i18n.t("menu.line.exercises", n=exercises)
    programs = await db.list_programs(user_id)
    # Первая — та, по которой тренировался последней (порядок list_programs):
    # рабочая программа, а не первая заведённая.
    lines["programs"] = programs[0]["name"] if programs else i18n.t("menu.line.programs_catalog")
    nxt = analytics.next_rank(rank)
    if nxt is not None:
        lagging = sum((
            total_workouts < nxt.min_workouts,
            tonnage_kg < nxt.min_tonnage_kg,
            per_week < nxt.min_per_week,
        ))
        gap = analytics.rank_gap(rank, total_workouts, tonnage_kg, per_week) if lagging == 1 else None
        if gap is not None:
            lines["achievements"] = i18n.t(
                "menu.line.rank_gap", name=nxt.name, gap=formatting.format_rank_gap(gap, unit)
            )
    if lines["achievements"] is None:
        lines["achievements"] = i18n.t("menu.line.rank", name=rank.name)
    return lines


async def rank_promotion(user_id: int, user) -> "analytics.Rank | None":
    """Звание, если оно только что выросло, иначе None.

    Само звание считается на лету (analytics.rank_for), поэтому «объявлено ли
    оно уже» приходится помнить отдельно — users.rank_level_seen. Понижение
    (перерыв стоит одной ступени) молча опускает и отметку: вернувшись к темпу,
    человек получит объявление снова — это возвращение, и оно того стоит.

    Живёт здесь, а не в handlers/workout.py, по той же причине, что и collect:
    объявление повышения нужно обоим потребителям — карточке завершения в боте
    и ответу finish в REST, — а вторая копия этих семи строк разъезжалась бы с
    первой молча. Вызов ОДНОРАЗОВЫЙ по смыслу: он же и ставит отметку
    «объявлено», поэтому второй вызов подряд вернёт None, и звать его на чтение
    экрана нельзя — повышение будет съедено и человек его не увидит.
    """
    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id)]
    agg = await db.hall_of_fame_aggregates(user_id)
    rank = analytics.rank_for(
        len(dates),
        formatting.to_kg(agg["tonnage"], user["unit"]),
        analytics.workouts_per_week(dates, timeutil.user_today(user)),
    )
    seen = user["rank_level_seen"]
    if rank.level == seen:
        return None
    await db.update_user(user_id, rank_level_seen=rank.level)
    return rank if rank.level > seen else None
