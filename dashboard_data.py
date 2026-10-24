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

import bisect
import datetime as dt
import itertools
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

import analytics
import db
import formatting
import seed_data
import timeutil

# Окно роста e1RM. У истории моложе восьми недель «рост за 8 недель» врёт: вся
# история лежит внутри окна, базы ДО него нет, и плиток не бывает вовсе — не
# потому что роста не было, а потому что не с чем сравнивать. Короткое окно даёт
# свежим аккаунтам шанс увидеть плитку до восьмой недели.
LIFT_WINDOW_WEEKS = 8
LIFT_FALLBACK_WINDOW_WEEKS = 4
# Кандидаты в плитки роста — все движения, сделанные хотя бы на стольких
# тренировках окна, без потолка по числу: топ по частоте отрезал редкие, но
# настоящие подъёмы. Порог отсекает шум: упражнение, попробованное раз-другой,
# давало «+40%» с одного захода и вытесняло стабильный рост.
LIFT_MIN_SESSIONS = 4

# Фигура «ТЫ» в сцене зала приложения растёт по группам мышц от настоящего
# объёма: среднее число рабочих подходов в неделю за последние BODY_VOLUME_WEEKS
# недель. Восемь, а не одна: фигура — накопленное, а не «что было на этой
# неделе», и пропущенная неделя не должна сдувать её за ночь. Те же восемь
# недель — у подходов по группам на карточке «ТЫ», которой делятся (`body_sets`).
BODY_VOLUME_WEEKS = 8
# Группы фигуры — машинные ключи встроенных групп (seed_data.muscle_group_slug),
# в этом порядке и всегда все семь. «Другое» и свои группы атлета в фигуру не
# идут: у них нет части тела.
BODY_VOLUME_GROUPS = ("chest", "back", "legs", "shoulders", "biceps", "triceps", "abs")


@dataclass(frozen=True)
class MenuDashboard:
    """Всё, что рисуется на сводке, уже в готовом к показу виде."""

    headline: str
    rank_name: str
    rank_level: int
    # Собственный эмодзи звания (`analytics.Rank.emoji`) — приложение ставит
    # его на плашку звания вместо общей медали, как в лестнице званий.
    rank_emoji: str = ""
    #: Недостача до следующего звания и его имя — те же строки, что отдаёт
    #: GET /hall-of-fame в rank.gap_text / rank.next_name (приложение берёт их
    #: для шапки профиля и больше не тянет ради них весь зал). Оба None на
    #: верхнем звании.
    rank_gap_text: Optional[str] = None
    rank_next_name: Optional[str] = None
    #: (подпись, число) или (подпись, число, приписка)
    tiles: list[tuple] = field(default_factory=list)
    volume_title: str = ""
    #: (НАЗВАНИЕ ГРУППЫ, подходов, статус)
    volume_rows: list[tuple[str, int, str]] = field(default_factory=list)
    #: (движение, «+12%», «227кг vs 220кг», id упражнения)
    lift_tiles: list[tuple[str, str, str, int]] = field(default_factory=list)
    lifts_title: str = ""
    lifts_note: str = ""
    #: (ключ группы, подходов в неделю) по BODY_VOLUME_GROUPS — см. body_volume
    body_volume: tuple[tuple[str, float], ...] = ()
    #: (ключ группы, подходов всего) за те же BODY_VOLUME_WEEKS недель
    body_sets: tuple[tuple[str, int], ...] = ()


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
    # (а движений их — все, что прошли LIFT_MIN_SESSIONS) перечитывал бы users.tz_offset сам.
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

    lift_tiles, lifts_title = await lift_growth(
        user_id, user, dates, today, exclude_workout_ids=excluded
    )

    agg = await db.hall_of_fame_aggregates(user_id, exclude_workout_ids=excluded)
    tonnage_kg = formatting.to_kg(agg["tonnage"], user["unit"])
    per_week = analytics.workouts_per_week(dates, today)
    rank = analytics.rank_for(len(dates), tonnage_kg, per_week)
    gap = analytics.rank_gap(rank, len(dates), tonnage_kg, per_week)
    next_rank = analytics.next_rank(rank) if gap else None
    sets = await body_sets(user_id, today, tz_offset=tz, exclude_workout_ids=excluded)
    return MenuDashboard(
        headline=formatting.menu_headline(dashboard),
        rank_name=rank.name,
        rank_level=rank.level,
        rank_emoji=rank.emoji,
        rank_gap_text=formatting.format_rank_gap(gap, user["unit"]) if gap else None,
        rank_next_name=next_rank.name if next_rank else None,
        tiles=formatting.menu_tiles(
            dashboard, tonnage, records, user["unit"], total_workouts=len(dates)
        ),
        volume_title=volume_title,
        volume_rows=volume_rows,
        lift_tiles=lift_tiles,
        lifts_title=lifts_title,
        lifts_note=formatting.MENU_LIFTS_NOTE,
        body_volume=volume_per_week(sets),
        body_sets=sets,
    )


async def body_sets(
    user_id: int,
    today: dt.date,
    *,
    tz_offset: Optional[int] = None,
    exclude_workout_ids: Iterable[int] = (),
) -> tuple[tuple[str, int], ...]:
    """Рабочих подходов по семи группам фигуры за последние BODY_VOLUME_WEEKS
    недель (по сегодня включительно).

    Тот же счёт подходов, что у панели «ОБЪЁМ ЗА 7 ДНЕЙ» (db.weekly_volume_by_group
    по exercises.primary_group_id), только окно длиннее — иначе фигура и
    панель под ней говорили бы о разных подходах. Группа без подходов — 0, а
    не пропуск: ключи всегда все семь."""
    start = today - dt.timedelta(days=BODY_VOLUME_WEEKS * 7 - 1)
    counts = await db.weekly_volume_by_group(
        user_id, start.isoformat(), today.isoformat(), tz_offset=tz_offset,
        exclude_workout_ids=exclude_workout_ids,
    )
    sets_by_slug: dict[str, int] = {}
    for group in await db.list_muscle_groups(user_id):
        slug = seed_data.muscle_group_slug(group["name"])
        if slug in BODY_VOLUME_GROUPS:
            sets_by_slug[slug] = sets_by_slug.get(slug, 0) + counts.get(group["id"], 0)
    return tuple((slug, sets_by_slug.get(slug, 0)) for slug in BODY_VOLUME_GROUPS)


def volume_per_week(sets: tuple[tuple[str, int], ...]) -> tuple[tuple[str, float], ...]:
    """Подходы за окно → в среднем в неделю, с одним знаком после запятой:
    по ним растёт фигура «ТЫ»."""
    return tuple((slug, round(count / BODY_VOLUME_WEEKS, 1)) for slug, count in sets)


@dataclass(frozen=True)
class TimelinePoint:
    """«ТЫ» на один день истории — кадр таймлапса «Видео: как ты рос» в
    приложении. Всё накопленное к концу дня `date` включительно, тем же
    счётом, что сводка и зал славы сегодня."""

    #: День с первой тренировки, первая — день 1.
    day: int
    date: dt.date
    workouts: int
    #: В единицах атлета (users.unit), как tonnage.value у /hall-of-fame.
    tonnage: float
    #: Дней-рекордов по движениям (db.e1rm_record_days) к этому дню.
    records: int
    rank: "analytics.Rank"
    #: (ключ группы, подходов в неделю) за BODY_VOLUME_WEEKS недель по этот
    #: день — то же, что body_volume сводки в тот день.
    body_volume: tuple[tuple[str, float], ...]


async def you_timeline(user_id: int, user: Any = None) -> Optional[list[TimelinePoint]]:
    """«ТЫ» по дням истории: точка на каждый день с законченной тренировкой
    и последняя — сегодня (если сегодня не тренировался), чтобы конец видео
    совпадал с «ТЫ» в подвале. `None` — тренировок нет.

    Только настоящие данные: точки — дни, когда атлет реально тренировался;
    вехи (неделя, месяц…) из них выбирает приложение и только те, до которых
    история доросла. Звание и тело на каждый день — те же формулы, что
    сегодня (analytics.rank_for, окно BODY_VOLUME_WEEKS), только «сегодня» —
    тот день. Перенесённая история — данные, как и везде в прогрессе."""
    if user is None:
        user = await db.get_user(user_id)
    if user is None:
        return None
    tz = timeutil.offset_hours(user)
    today = timeutil.user_today(user)
    dates = [
        d for d in (dt.date.fromisoformat(s) for s in await db.list_finished_workout_dates(user_id, tz_offset=tz))
        if d <= today
    ]
    if not dates:
        return None
    unit = user["unit"]
    tonnage_by_day = {
        dt.date.fromisoformat(d): t
        for d, t in (await db.daily_tonnage(user_id, "0001-01-01", today.isoformat(), tz_offset=tz)).items()
    }
    record_days = [dt.date.fromisoformat(d) for d in await db.e1rm_record_days(
        user_id, user["e1rm_formula"], tz_offset=tz
    )]
    slug_of: dict[Optional[int], str] = {}
    for group in await db.list_muscle_groups(user_id):
        slug = seed_data.muscle_group_slug(group["name"])
        if slug in BODY_VOLUME_GROUPS:
            slug_of[group["id"]] = slug
    sets_by_day: dict[dt.date, dict[str, int]] = {}
    for d, by_group in (await db.daily_sets_by_group(user_id, tz_offset=tz)).items():
        bucket = sets_by_day.setdefault(dt.date.fromisoformat(d), {})
        for gid, count in by_group.items():
            slug = slug_of.get(gid)
            if slug is not None:
                bucket[slug] = bucket.get(slug, 0) + count

    days = sorted(set(dates))
    if days[-1] < today:
        days.append(today)
    first = dates[0]
    window = dt.timedelta(days=BODY_VOLUME_WEEKS * 7 - 1)
    # Накопленные суммы по дням — точек бывает сотни, пересчитывать всю
    # историю на каждую незачем.
    tonnage_days = sorted(tonnage_by_day)
    tonnage_sum = list(itertools.accumulate(tonnage_by_day[d] for d in tonnage_days))
    set_days = sorted(sets_by_day)
    set_sum = {
        slug: [0, *itertools.accumulate(sets_by_day[d].get(slug, 0) for d in set_days)]
        for slug in BODY_VOLUME_GROUPS
    }
    points: list[TimelinePoint] = []
    for day in days:
        workouts = bisect.bisect_right(dates, day)
        k = bisect.bisect_right(tonnage_days, day)
        tonnage = tonnage_sum[k - 1] if k else 0.0
        lo = bisect.bisect_left(set_days, day - window)
        hi = bisect.bisect_right(set_days, day)
        sets = tuple((slug, set_sum[slug][hi] - set_sum[slug][lo]) for slug in BODY_VOLUME_GROUPS)
        rank = analytics.rank_for(
            workouts, formatting.to_kg(tonnage, unit), analytics.workouts_per_week(dates[:workouts], day)
        )
        points.append(TimelinePoint(
            day=(day - first).days + 1,
            date=day,
            workouts=workouts,
            tonnage=round(tonnage, 1),
            records=bisect.bisect_right(record_days, day),
            rank=rank,
            body_volume=volume_per_week(sets),
        ))
    return points


async def lift_growth(
    user_id: int,
    user: Any,
    dates: list[dt.date],
    today: dt.date,
    *,
    exclude_workout_ids: Iterable[int] = (),
    shown_name: Callable[[str], str] = lambda name: name,
) -> tuple[list[tuple], str]:
    """Плитки роста e1RM и их заголовок («РОСТ E1RM ЗА 8 НЕДЕЛЬ») — блок
    «Прогресса» атлета под разбором тренера. Один расчёт на сводку атлета
    (`collect`) и на «Прогресс клиента» у тренера (coach_service.client_progress):
    приложение рисует оба одним видом, и цифры не должны разъезжаться.

    `dates` — местные дни законченных тренировок (от них зависит, хватает ли
    истории на восемь недель), `shown_name` — как показать имя упражнения:
    тренеру каталожное — на его языке. Роста нет — `([], "")`."""
    if not dates:
        return [], ""
    excluded = tuple(exclude_workout_ids)
    tz = timeutil.offset_hours(user)
    formula = user["e1rm_formula"]
    # Движения — все, что были хотя бы на LIFT_MIN_SESSIONS тренировках окна. Не «базовые»: типа
    # движения в базе нет, и выбирать жим/присед/тягу пришлось бы по каталожным
    # именам, а у человека со своими названиями список оказался бы пустым.
    history_age_weeks = (today - min(dates)).days / 7
    lift_window_weeks = (
        LIFT_FALLBACK_WINDOW_WEEKS if history_age_weeks < LIFT_WINDOW_WEEKS else LIFT_WINDOW_WEEKS
    )
    lift_start = today - dt.timedelta(weeks=lift_window_weeks)
    growth: list[tuple[str, float, float, int]] = []
    for row in await db.top_exercises_by_frequency(
        user_id, lift_start.isoformat(), today.isoformat(), limit=None,
        min_sessions=LIFT_MIN_SESSIONS, tz_offset=tz,
        exclude_workout_ids=excluded,
    ):
        before_max, window_max = await db.exercise_e1rm_growth(
            user_id, row["id"], lift_start.isoformat(), formula, tz_offset=tz,
            exclude_workout_ids=excluded,
        )
        growth.append((shown_name(row["display_name"]), before_max, window_max, row["id"]))
    tiles = formatting.menu_lift_tiles(growth, user["unit"])
    return tiles, formatting.menu_lifts_title(lift_window_weeks) if tiles else ""


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
    # Уровень 0 («Новичок») — стартовая ступень, а не повышение: на первой
    # тренировке «Новое звание: Новичок» звучало как издёвка. Отметка всё равно
    # ставится, а сравнение — с max(seen, 0), так что первое настоящее
    # повышение объявляется как раньше.
    return rank if rank.level > max(seen, 0) else None
