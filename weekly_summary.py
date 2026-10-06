"""«Итог недели» — сбор чисел за одну неделю пн–вс и вердикт тренера по ним.

Экран приложения (GET /v1/weekly-summary, api_v1_weekly.py) открывается из
воскресного пуша и показывает одну неделю целиком: тренировки и тоннаж против
прошлой, рекорды списком, подходы по группам против коридора 6–12, самый
растущий подъём, серию, следующий день программы и разбор AI-тренера, если он
был. Почти всё это уже считается для сводки (dashboard_data.collect), но там
окно скользящее — «последние 7 дней от сегодня». Здесь окно календарное и
закреплённое: неделя, про которую был пуш, остаётся той же неделей и в
понедельник, когда пуш открыли с опозданием.

Окно — ровно семь местных суток, с понедельника по воскресенье включительно.
Воскресный дайджест раньше считал тоннаж через tonnage_since(today − 7) с `>=`,
то есть за восемь суток (прошлое воскресенье входило дважды — в эту неделю и в
прошлую). Здесь граница одна на все агрегаты: `week_start .. week_start + 6`.

Вердикт — чистая функция `verdict_kind` по таблице правил (порядок важен,
срабатывает первое): first → comeback → empty → record → record_fewer → short →
strong → steady. «Обычно» — свой темп атлета: среднее число тренировок за
8 полных недель до этой, но только начиная с недели его первой тренировки. Если
таких недель меньше трёх, сравниваем с прошлой неделей. Подколка — только в
short и empty, и только про пропуск (TONE_OF_VOICE.md, «Подколки»).

Каждая фраза вердикта говорит только то, что подтверждают данные (тот же приём,
что push_texts.pick_text): у шаблона есть плейсхолдеры, и шаблон, которому не
хватает значения, выпадает из выбора. «Вместо обычных трёх» не появится там, где
«обычного» нет, а «больше прошлой» — там, где прошлая неделя была не меньше.

Тексты собираются здесь же и уже локализованными (i18n.t), как у сводки: язык
выставляет вызывающий (`i18n.use_lang(users.lang)` в api_v1_weekly). В модуле нет
ни одной строки, вычисленной на импорте, — см. CLAUDE.md, «Ловушка,
встретившаяся шесть раз».
"""

from __future__ import annotations

import datetime as dt
import re
import zlib
from dataclasses import dataclass, field
from typing import Any, Optional

import analytics
import db
import formatting
import i18n
import seed_data
import timeutil

# Порядок правил вердикта — он же порядок проверки.
KINDS = ("first", "comeback", "empty", "record", "record_fewer", "short", "strong", "steady")

# Перерыв, после которого тренировка — возвращение, а не очередная неделя.
COMEBACK_GAP_DAYS = 14
# Сколько полных недель до этой входят в «обычный» темп атлета.
USUAL_WINDOW_WEEKS = 8
# Меньше этого числа недель истории — «обычного» ещё нет, сравниваем с прошлой.
USUAL_MIN_WEEKS = 3
# Прирост тоннажа к прошлой неделе, после которого неделя «в плюс» даже без
# лишней тренировки.
STRONG_TONNAGE_GAIN = 0.10

# Окно роста e1RM — как у сводки (dashboard_data): 8 недель, у короткой истории 4.
LIFT_WINDOW_WEEKS = 8
LIFT_FALLBACK_WINDOW_WEEKS = 4
LIFT_CANDIDATES = 12


def week_monday(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def default_week(today: dt.date, first_day: Optional[dt.date] = None) -> dt.date:
    """Понедельник последней законченной недели — той, что уже кончилась к
    `today` (в любой день, включая воскресенье, это прошлая неделя).

    `first_day` — день первой законченной тренировки атлета. Если прошлая
    неделя целиком раньше него, атлета в ней ещё не было, и итога у неё нет:
    тогда по умолчанию — текущая неделя, с которой его дневник начался
    (вердикт «first»), а не «неделя мимо» за время, когда его тут не было."""
    last = week_monday(today) - dt.timedelta(days=7)
    if first_day is not None and last + dt.timedelta(days=6) < first_day:
        return week_monday(today)
    return last


def before_history(week_start: dt.date, first_day: Optional[dt.date]) -> bool:
    """Неделя целиком раньше первой тренировки — итога у неё нет."""
    return first_day is not None and week_monday(week_start) + dt.timedelta(days=6) < first_day


async def first_workout_day(user_id: int, user: Any) -> Optional[dt.date]:
    """Местный день первой законченной тренировки, None — их нет."""
    dates = await db.list_finished_workout_dates(user_id, tz_offset=timeutil.offset_hours(user))
    return min(dt.date.fromisoformat(d) for d in dates) if dates else None


@dataclass(frozen=True)
class WeekFacts:
    """Сырьё для вердикта — только числа, без текста (так правило проверяется
    таблицей случаев без базы)."""

    this: int
    last: Optional[int]  # None — до прошлой недели истории нет
    usual: Optional[int]  # None — меньше USUAL_MIN_WEEKS недель истории
    records: int
    tonnage: float
    last_tonnage: float
    is_first: bool
    gap_days: Optional[int]  # перерыв перед первой тренировкой недели


def comparison_base(facts: WeekFacts) -> Optional[int]:
    """С чем сравнивается число тренировок: «обычно», иначе прошлая неделя."""
    return facts.usual if facts.usual is not None else facts.last


def verdict_kind(facts: WeekFacts) -> str:
    """Вердикт недели по правилам из дизайна, сверху вниз — первое сработавшее."""
    if facts.this > 0 and facts.is_first:
        return "first"
    if facts.this > 0 and facts.gap_days is not None and facts.gap_days >= COMEBACK_GAP_DAYS:
        return "comeback"
    if facts.this == 0:
        return "empty"
    base = comparison_base(facts)
    fewer = base is not None and facts.this < base
    if facts.records > 0:
        return "record_fewer" if fewer else "record"
    if fewer:
        return "short"
    more = base is not None and facts.this > base
    tonnage_up = facts.last_tonnage > 0 and facts.tonnage >= facts.last_tonnage * (1 + STRONG_TONNAGE_GAIN)
    if more or tonnage_up:
        return "strong"
    return "steady"


@dataclass(frozen=True)
class Verdict:
    kind: str
    greeting: str
    title: str
    body: str


@dataclass
class WeeklySummary:
    """Всё для экрана «Итог недели», тексты уже на языке атлета."""

    week_start: dt.date
    week_end: dt.date
    closed: bool
    verdict: Verdict
    workouts_this: int
    workouts_last: Optional[int]
    workouts_usual: Optional[int]
    days: list[bool]
    tonnage_this: float
    tonnage_last: float
    tonnage_label: str
    tonnage_last_label: Optional[str]
    tonnage_delta_pct: Optional[int]
    records: list[dict[str, Any]] = field(default_factory=list)
    volume_rows: list[dict[str, Any]] = field(default_factory=list)
    top_lift: Optional[dict[str, Any]] = None
    streak_weeks: int = 0
    hint: Optional[str] = None
    next_day: Optional[dict[str, Any]] = None
    coach_text: Optional[dict[str, Any]] = None


# ---------- тексты ----------

_PLACEHOLDER = re.compile(r"\{(\w+)[,}]")


def _template_vars(template: str) -> set[str]:
    return set(_PLACEHOLDER.findall(template))


def _body_pool(lang: str, prefix: str) -> list[tuple[str, str]]:
    """(ключ, шаблон) вариантов `prefix.<n>` из каталога, по номеру."""
    catalog = i18n._load_catalog(lang)
    items = [
        (int(k[len(prefix):]), k, v)
        for k, v in catalog.items()
        if k.startswith(prefix) and k[len(prefix):].isdigit()
    ]
    return [(k, v) for _, k, v in sorted(items)]


def pick_body(prefix: str, params: dict[str, Any], seed: str) -> str:
    """Один вариант тела вердикта, для которого хватает данных.

    Шаблон с плейсхолдером, которому нет значения (None или нет ключа),
    выпадает: фраза не должна утверждать то, чего данные не показывают.
    Выбор детерминирован (crc32 от атлета, недели и вида), а не случаен:
    один и тот же экран, открытый дважды, не должен говорить по-разному.
    """
    lang = i18n.get_lang()
    known = {k for k, v in params.items() if v is not None}
    pool = [(k, t) for k, t in _body_pool(lang, prefix) if _template_vars(t) <= known]
    if not pool:
        raise KeyError(f"weekly_summary: no {prefix}* variant fits {sorted(known)}")
    # Самые конкретные варианты (больше подтверждённых чисел) — в приоритете:
    # «4 вместо обычных 3» говорит больше, чем «темп не просел».
    best = max(len(_template_vars(t)) for _, t in pool)
    pool = [(k, t) for k, t in pool if len(_template_vars(t)) == best]
    key, _ = pool[zlib.crc32(seed.encode()) % len(pool)]
    return i18n.t(key, **{k: v for k, v in params.items() if v is not None})


def tonnage_label(total: float, unit: str) -> str:
    """«14.2т», «800кг», «7,050lb» — единица вплотную к числу."""
    if unit == "lb":
        return formatting.format_lb_tonnage(total)
    total_kg = formatting.to_kg(total, unit)
    if total_kg >= 1000:
        return f"{total_kg / 1000:.1f}{i18n.t('unit.ton_short')}"
    return f"{total:.0f}{formatting.unit_label(unit)}"


def build_verdict(
    facts: WeekFacts, *, unit: str, streak: int, closed: bool, seed: str
) -> Verdict:
    kind = verdict_kind(facts)
    base_is_usual = facts.usual is not None
    params: dict[str, Any] = {"this": facts.this, "records": facts.records or None}
    if kind in ("first", "steady", "strong"):
        params["tonnage"] = tonnage_label(facts.tonnage, unit) if facts.tonnage > 0 else None
    if kind in ("record", "strong"):
        # «Вместо обычных N» — только когда тренировок правда больше.
        if base_is_usual and facts.this > facts.usual:
            params["usual"] = facts.usual
        elif not base_is_usual and facts.last is not None and facts.this > facts.last:
            params["last"] = facts.last
    if kind in ("record_fewer", "short"):
        if base_is_usual:
            params["usual"] = facts.usual
        else:
            params["last"] = facts.last
    if kind == "steady" and base_is_usual and facts.this == facts.usual:
        params["usual"] = facts.usual
    if kind == "strong" and facts.last_tonnage > 0 and facts.tonnage > facts.last_tonnage:
        params["tonnage_gain"] = tonnage_label(facts.tonnage - facts.last_tonnage, unit)
    if kind == "comeback":
        params["gap_weeks"] = (facts.gap_days or 0) // 7
    prefix = f"weekly.verdict.{kind}.body."
    # Пустая неделя, а воскресенье ещё идёт — серия держится до полуночи
    # (льгота compute_dashboard); неделя закрыта — серию начнём заново.
    if kind == "empty" and not closed and streak > 0:
        prefix = "weekly.verdict.empty_open.body."
        params["streak"] = streak
    body = pick_body(prefix, params, f"{seed}:{kind}")
    return Verdict(
        kind=kind,
        greeting=i18n.t("weekly.greeting"),
        # Заголовок short — «1 из 3»: число и то, с чем сравнили.
        title=i18n.t(f"weekly.verdict.{kind}.title", this=facts.this, base=comparison_base(facts)),
        body=body,
    )


# ---------- сбор ----------

def _usual(dates: list[dt.date], week_start: dt.date) -> Optional[int]:
    """Средний темп за USUAL_WINDOW_WEEKS полных недель до этой — только с
    недели первой тренировки. None, если таких недель меньше USUAL_MIN_WEEKS."""
    if not dates:
        return None
    first_monday = week_monday(min(dates))
    window_start = max(week_start - dt.timedelta(weeks=USUAL_WINDOW_WEEKS), first_monday)
    weeks = (week_start - window_start).days // 7
    if weeks < USUAL_MIN_WEEKS:
        return None
    count = sum(1 for d in dates if window_start <= d < week_start)
    # Округление к ближайшему, половина — вверх (round() в Python банковское).
    return int(count / weeks + 0.5)


def _volume_rows(
    this_counts: dict[Optional[int], int], last_counts: dict[Optional[int], int], groups: list
) -> list[dict[str, Any]]:
    """Подходы по группам за эту неделю и за прошлую — строки для коридора.

    Те же правила, что у панели сводки (formatting.weekly_volume_panel): «Другое»
    не показываем, нули остаются (пропущенная группа — главное, что панель
    сообщает), а если ноль везде — строк нет совсем. Имя группы — обычным
    регистром на языке атлета: на экране оно стоит подписью строки, не тегом."""
    lang = i18n.get_lang()
    rows: list[dict[str, Any]] = []
    for group in groups:
        if group["name"].strip().lower() in formatting.VOLUME_HIDDEN_GROUPS:
            continue
        sets = this_counts.get(group["id"], 0)
        rows.append({
            "group": seed_data.localized_muscle_group_name(group["name"], lang),
            "sets": sets,
            "last": last_counts.get(group["id"], 0),
            "status": analytics.classify_weekly_volume(sets),
        })
    if this_counts.get(None, 0) or last_counts.get(None, 0):
        sets = this_counts.get(None, 0)
        rows.append({
            "group": i18n.t("weekly.volume.ungrouped"),
            "sets": sets,
            "last": last_counts.get(None, 0),
            "status": analytics.classify_weekly_volume(sets),
        })
    if sum(r["sets"] for r in rows) == 0:
        return []
    rows.sort(key=lambda r: (-r["sets"], r["group"]))
    return rows


def _hint(rows: list[dict[str, Any]]) -> Optional[str]:
    """Подсказка из объёма: самая отстающая группа в статусе low, у которой
    подходы есть. Группа на нуле — не «добери», а другая история; нет таких —
    нет и строки."""
    low = [r for r in rows if r["status"] == "low" and r["sets"] > 0]
    if not low:
        return None
    row = min(low, key=lambda r: (r["sets"], r["group"]))
    missing = analytics.WEEKLY_VOLUME_MIN - row["sets"]
    return i18n.t("weekly.hint", group=row["group"], n=missing)


async def _records(
    user_id: int, start: dt.date, end: dt.date, formula: str, unit: str, tz: int
) -> list[dict[str, Any]]:
    u = formatting.unit_label(unit)
    out = []
    for row in await db.e1rm_records_in_window(
        user_id, start.isoformat(), end.isoformat(), formula, tz_offset=tz
    ):
        gain = row["e1rm"] - row["earlier"]
        out.append({
            "exercise_id": row["exercise_id"],
            "exercise": row["display_name"],
            "best": i18n.t(
                "weekly.record.best", weight=f"{formatting.format_weight(row['weight'])}{u}",
                reps=row["reps"],
            ),
            "e1rm": round(row["e1rm"], 1),
            "gain": f"+{formatting.format_weight(round(gain, 1))}{u}",
        })
    return out


async def _top_lift(
    user_id: int, dates: list[dt.date], week_end: dt.date, formula: str, unit: str, tz: int
) -> Optional[dict[str, Any]]:
    """Самый растущий подъём к концу недели — первая плитка роста сводки,
    только окно кончается воскресеньем этой недели, а не сегодняшним днём."""
    history_weeks = (week_end - min(dates)).days / 7
    weeks = LIFT_FALLBACK_WINDOW_WEEKS if history_weeks < LIFT_WINDOW_WEEKS else LIFT_WINDOW_WEEKS
    lift_start = week_end - dt.timedelta(weeks=weeks) + dt.timedelta(days=1)
    best: Optional[tuple[float, int, str, float, float]] = None
    for row in await db.top_exercises_by_frequency(
        user_id, lift_start.isoformat(), week_end.isoformat(), limit=LIFT_CANDIDATES, tz_offset=tz
    ):
        before, window = await db.exercise_e1rm_growth(
            user_id, row["id"], lift_start.isoformat(), formula, tz_offset=tz,
            window_end_date=week_end.isoformat(),
        )
        if before <= 0:
            continue
        pct = round((window - before) / before * 100)
        if pct < 1:
            continue
        ratio = (window - before) / before
        if best is None or ratio > best[0]:
            best = (ratio, row["id"], row["display_name"], before, window)
    if best is None:
        return None
    ratio, exercise_id, name, before, window = best
    u = formatting.unit_label(unit)
    return {
        "exercise_id": exercise_id,
        "exercise": name,
        "growth": f"+{round(ratio * 100)}%",
        "detail": f"{before:.0f} → {window:.0f}{u}",
        "title": formatting.menu_lifts_title(weeks),
        "weeks": weeks,
    }


async def _next_day(user_id: int) -> Optional[dict[str, Any]]:
    """Следующий день программы, по которой атлет тренировался последним."""
    program_id = await db.last_trained_program_id(user_id)
    if program_id is None:
        return None
    program = await db.get_program(program_id)
    day = await db.next_program_day(program_id)
    if program is None or day is None:
        return None
    exercises = await db.list_routine_exercises(day["id"])
    return {
        "program_id": program_id,
        "program_name": program["name"],
        "routine_id": day["id"],
        "name": day["name"],
        "exercises": [e["display_name"] for e in exercises],
    }


async def _coach_text(
    user_id: int, user: Any, week_start: dt.date, week_end: dt.date, tz: int
) -> Optional[dict[str, Any]]:
    """Сохранённый воскресный разбор тренера, если он есть и написан на языке
    атлета. Сменил язык — блок прячем: чужой язык на экране хуже пустого места.
    `stale` — после разбора закрыта ещё тренировка этой недели, и он о ней не знает."""
    row = await db.get_weekly_digest(user_id, week_start.isoformat())
    if row is None or row["lang"] != user["lang"]:
        return None
    later = await db.count_workouts_finished_after(
        user_id, week_start.isoformat(), week_end.isoformat(), row["created_at"], tz_offset=tz
    )
    return {"text": row["text"], "written_at": row["created_at"], "stale": later > 0}


async def collect(user_id: int, week_start: dt.date, user: Any = None) -> Optional[WeeklySummary]:
    """Итог недели, начинающейся `week_start` (понедельник), или None, если
    законченных тренировок у атлета нет вовсе — как у dashboard_data.collect:
    у новичка показывать нечего, и экран зовёт начать, а не рисует нули.
    Тот же None — у недели, которая целиком раньше первой тренировки: атлета в
    ней ещё не было, и «неделя мимо» с «серию начнём заново» были бы неправдой.

    `week_start`, который не понедельник, сдвигается к понедельнику своей недели.
    """
    if user is None:
        user = await db.get_user(user_id)
    if user is None:
        return None
    week_start = week_monday(week_start)
    week_end = week_start + dt.timedelta(days=6)
    last_start = week_start - dt.timedelta(days=7)
    last_end = week_start - dt.timedelta(days=1)
    today = timeutil.user_today(user)
    tz = timeutil.offset_hours(user)
    unit = user["unit"]
    formula = user["e1rm_formula"]

    all_dates = [
        dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id, tz_offset=tz)
    ]
    if not all_dates or before_history(week_start, min(all_dates)):
        return None
    upto = [d for d in all_dates if d <= week_end]
    this_dates = [d for d in upto if d >= week_start]
    before = [d for d in upto if d < week_start]
    # «Прошлая неделя» есть, когда история начинается раньше этой недели: даже
    # пустая прошлая неделя — подтверждённый ноль, а не отсутствие данных.
    has_last = bool(before)
    last_count = sum(1 for d in before if d >= last_start) if has_last else None

    async def tonnage(start: dt.date, end: dt.date) -> float:
        return sum((await db.daily_tonnage(user_id, start.isoformat(), end.isoformat(), tz_offset=tz)).values())

    tonnage_this = await tonnage(week_start, week_end)
    tonnage_last = await tonnage(last_start, last_end) if has_last else 0.0

    records = await _records(user_id, week_start, week_end, formula, unit, tz) if before else []
    gap_days = (min(this_dates) - max(before)).days if this_dates and before else None
    closed = today > week_end
    dashboard = analytics.compute_dashboard(upto, min(today, week_end))
    streak = dashboard.week_streak
    if closed and not this_dates:
        streak = 0  # пустая закрытая неделя серию уже оборвала

    facts = WeekFacts(
        this=len(this_dates),
        last=last_count,
        usual=_usual(all_dates, week_start),
        records=len(records),
        tonnage=tonnage_this,
        last_tonnage=tonnage_last,
        is_first=bool(this_dates) and not before,
        gap_days=gap_days,
    )
    verdict = build_verdict(
        facts, unit=unit, streak=streak, closed=closed, seed=f"{user_id}:{week_start.isoformat()}"
    )

    groups = await db.list_muscle_groups(user_id)
    volume_rows = _volume_rows(
        await db.weekly_volume_by_group(user_id, week_start.isoformat(), week_end.isoformat(), tz_offset=tz),
        await db.weekly_volume_by_group(user_id, last_start.isoformat(), last_end.isoformat(), tz_offset=tz),
        groups,
    )
    days = [False] * 7
    for d in this_dates:
        days[d.weekday()] = True
    delta_pct = (
        round((tonnage_this - tonnage_last) / tonnage_last * 100) if tonnage_last > 0 else None
    )
    return WeeklySummary(
        week_start=week_start,
        week_end=week_end,
        closed=closed,
        verdict=verdict,
        workouts_this=facts.this,
        workouts_last=facts.last,
        workouts_usual=facts.usual,
        days=days,
        tonnage_this=tonnage_this,
        tonnage_last=tonnage_last,
        tonnage_label=tonnage_label(tonnage_this, unit),
        tonnage_last_label=tonnage_label(tonnage_last, unit) if has_last else None,
        tonnage_delta_pct=delta_pct,
        records=records,
        volume_rows=volume_rows,
        top_lift=(
            await _top_lift(user_id, upto, week_end, formula, unit, tz)
            if this_dates and not facts.is_first else None
        ),
        streak_weeks=streak,
        hint=_hint(volume_rows) if this_dates else None,
        next_day=await _next_day(user_id),
        coach_text=await _coach_text(user_id, user, week_start, week_end, tz),
    )
