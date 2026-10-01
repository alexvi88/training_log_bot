"""Фраза тренера на заставке приложения — одна на день, по данным атлета.

Заставка iOS-приложения («тренер включает свет») пишет мелом на доске
заголовок и одну короткую фразу про человека. Выбор детерминирован и
бесплатен: никакой модели, только дневник. Фраза говорится, только если
данные её подтверждают (TONE_OF_VOICE, «Пуш обязан быть правдой»); ничего не
подошло — заголовок «ПРИВЕТ АТЛЕТ!» без фразы.

Порядок — от редкого к обычному: годовщина, круглое число тренировок, новое
звание, рекорд, первая тренировка, последний день года → пропуски и перерыв →
неделя и серия → «вчера» → время суток → день недели. Редкое событие
случается раз в месяцы, и если его перекроет «пятница», человек его не
увидит вовсе; обычное повторится через неделю.

Одна фраза на календарный день атлета (его пояс, users.tz_offset): выбранная
запоминается в users.coach_greeting_day/kind и держится, пока остаётся
правдой. Перестала (утро кончилось в 8:00, «три дня тишины» закончились
тренировкой) — выбирается заново: правда важнее постоянства.

Подколки — только за пропуски; перерыв в две недели и больше — уже поддержка
(«Пришёл — уже полдела»), как у пушей win-back. Вчерашнюю фразу два дня
подряд не повторяем, если правдой оказалась другая.

Пасхалку «лампа коротит» сервер не выбирает: она случайная, раз в 50
запусков, и живёт в приложении целиком — вместе с текстом. Фразу новичка без
единой тренировки — тоже: у него `/dashboard` отвечает `null`, и приложение
берёт её из своего каталога.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

import analytics
import db
import formatting
import i18n
import timeutil

# Недельная норма. Своей цели «тренировок в неделю» у атлета в базе нет
# (users.days_per_week больше не пишется, см. api_v1_account), поэтому норма —
# верхнее требование лестницы званий по частоте: 3 в неделю. Это то же число,
# которым звание меряет «ходишь ли ты сейчас».
WEEK_GOAL = math.ceil(analytics.RANKS[-1].min_per_week)

# Сколько дней после тренировки её событие (рекорд, звание, сотая) ещё
# «свежее». Дальше начинаются пропуски (skip_3), и тренер говорит уже о них:
# поздравлять человека, который неделю не заходит, с прошлым рекордом — мимо.
FRESH_DAYS = 2

# Окна времени суток, местные часы: [начало, конец).
MORNING_HOURS = (5, 8)
NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 5


@dataclass(frozen=True)
class Facts:
    """Всё, на чём стоит выбор. Числа и даты — никакого текста: текст
    собирается при показе на языке атлета (CLAUDE.md, «Ловушка…»)."""

    now: dt.datetime  # местное время атлета, наивное
    dates: tuple[dt.date, ...]  # местные дни законченных тренировок, по возрастанию
    dashboard: analytics.Dashboard
    record_last: bool = False  # последняя тренировка побила прежний e1RM
    rank_up_level: Optional[int] = None  # звание, которое дала последняя тренировка

    @property
    def today(self) -> dt.date:
        return self.now.date()

    @property
    def total(self) -> int:
        return len(self.dates)

    @property
    def days_since(self) -> Optional[int]:
        return self.dashboard.days_since_last

    @property
    def fresh(self) -> bool:
        return self.days_since is not None and self.days_since <= FRESH_DAYS

    @property
    def trained_last_week(self) -> bool:
        monday = self.today - dt.timedelta(days=self.today.weekday())
        return any(monday - dt.timedelta(days=7) <= d < monday for d in self.dates)


@dataclass(frozen=True)
class Kind:
    """Одна фраза: условие (возвращает параметры текста или None) и ключи."""

    code: str
    title_key: str
    check: Callable[[Facts], Optional[dict[str, Any]]]
    # Конец окна в местных часах — у фраз про время суток; у остальных фраза
    # живёт до конца местного дня.
    until: Optional[Callable[[Facts], dt.datetime]] = None

    @property
    def text_key(self) -> str:
        return f"coach_greeting.{self.code}.text"


def _anniversary(f: Facts) -> Optional[dict[str, Any]]:
    if not f.dates:
        return None
    first = f.dates[0]
    years = f.today.year - first.year
    if years >= 1 and (first.month, first.day) == (f.today.month, f.today.day):
        return {"years": years}
    return None


def _hundredth(f: Facts) -> Optional[dict[str, Any]]:
    if f.total >= 100 and f.total % 100 == 0 and f.fresh:
        return {"n": f.total}
    return None


def _new_rank(f: Facts) -> Optional[dict[str, Any]]:
    if f.rank_up_level is not None and f.fresh:
        return {"level": f.rank_up_level}
    return None


def _record(f: Facts) -> Optional[dict[str, Any]]:
    return {} if f.record_last and f.fresh else None


def _first_workout(f: Facts) -> Optional[dict[str, Any]]:
    return {} if f.total == 1 else None


def _year_end(f: Facts) -> Optional[dict[str, Any]]:
    # «Закрой его подходом» тому, кто сегодня уже отработал, — мимо.
    if (f.today.month, f.today.day) == (12, 31) and f.days_since != 0:
        return {}
    return None


def _break(lo: int, hi: Optional[int]) -> Callable[[Facts], Optional[dict[str, Any]]]:
    """Пропуск от lo до hi дней (включительно; hi=None — без верха). «А раньше
    ходил» — хотя бы две тренировки: у одной-единственной своя фраза выше."""

    def check(f: Facts) -> Optional[dict[str, Any]]:
        d = f.days_since
        if f.total >= 2 and d is not None and d >= lo and (hi is None or d <= hi):
            return {}
        return None

    return check


def _week_closed(f: Facts) -> Optional[dict[str, Any]]:
    return {"goal": WEEK_GOAL} if f.dashboard.this_week == WEEK_GOAL else None


def _streak(f: Facts) -> Optional[dict[str, Any]]:
    # Серия говорится в ту неделю, когда она выросла (первая тренировка
    # недели уже есть), — иначе у регулярного атлета «N недель подряд»
    # висела бы каждый день и закрыла собой всё остальное.
    n = f.dashboard.week_streak
    if n >= 3 and f.dashboard.this_week == 1:
        return {"n": n}
    return None


def _week_almost(f: Facts) -> Optional[dict[str, Any]]:
    done = f.dashboard.this_week
    if WEEK_GOAL >= 2 and done == WEEK_GOAL - 1:
        return {"done": done, "goal": WEEK_GOAL}
    return None


def _yesterday(f: Facts) -> Optional[dict[str, Any]]:
    return {} if f.days_since == 1 else None


def _morning(f: Facts) -> Optional[dict[str, Any]]:
    return {} if MORNING_HOURS[0] <= f.now.hour < MORNING_HOURS[1] else None


def _morning_until(f: Facts) -> dt.datetime:
    return dt.datetime.combine(f.today, dt.time(MORNING_HOURS[1]))


def _night(f: Facts) -> Optional[dict[str, Any]]:
    return {} if f.now.hour >= NIGHT_START_HOUR or f.now.hour < NIGHT_END_HOUR else None


def _night_until(f: Facts) -> dt.datetime:
    if f.now.hour < NIGHT_END_HOUR:
        return dt.datetime.combine(f.today, dt.time(NIGHT_END_HOUR))
    return _end_of_day(f)


def _monday(f: Facts) -> Optional[dict[str, Any]]:
    # «А ты продолжаешь» — правда, только если на прошлой неделе он ходил.
    return {} if f.today.weekday() == 0 and f.trained_last_week else None


def _friday(f: Facts) -> Optional[dict[str, Any]]:
    return {} if f.today.weekday() == 4 else None


def _end_of_day(f: Facts) -> dt.datetime:
    return dt.datetime.combine(f.today + dt.timedelta(days=1), dt.time(0))


_HEY = "coach_greeting.title.hey"  # ПРИВЕТ АТЛЕТ!
_HEY_CALM = "coach_greeting.title.hey_calm"  # ПРИВЕТ АТЛЕТ.

# Порядок — приоритет (см. модульную докстрингу).
KINDS: tuple[Kind, ...] = (
    Kind("anniversary", _HEY, _anniversary),
    Kind("hundredth", "coach_greeting.hundredth.title", _hundredth),
    Kind("new_rank", "coach_greeting.new_rank.title", _new_rank),
    Kind("record", _HEY, _record),
    Kind("first_workout", _HEY, _first_workout),
    Kind("year_end", _HEY, _year_end),
    Kind("long_break", _HEY, _break(14, None)),
    Kind("skip_7", _HEY_CALM, _break(7, 13)),
    Kind("skip_5", _HEY_CALM, _break(5, 6)),
    Kind("skip_3", _HEY, _break(3, 4)),
    Kind("week_closed", "coach_greeting.week_closed.title", _week_closed),
    Kind("streak", "coach_greeting.streak.title", _streak),
    Kind("week_almost", _HEY, _week_almost),
    Kind("yesterday", _HEY, _yesterday),
    Kind("morning", _HEY, _morning, _morning_until),
    Kind("night", _HEY_CALM, _night, _night_until),
    Kind("monday", _HEY, _monday),
    Kind("friday", _HEY, _friday),
)
_BY_CODE = {k.code: k for k in KINDS}


def pick(
    facts: Facts, kept: Optional[str] = None, yesterday: Optional[str] = None
) -> Optional[tuple[Kind, dict[str, Any]]]:
    """Фраза для этих фактов: `kept` (выбранная сегодня раньше), если она всё
    ещё правда, иначе первая подходящая по приоритету; None — ничего.

    `yesterday` — фраза вчерашнего дня: её не повторяем два дня подряд, если
    правдой оказалась ещё хоть одна («Вчера отработал» у того, кто ходит
    каждый день, иначе висела бы неделями). Подошла только она — говорим её."""
    if kept in _BY_CODE:
        params = _BY_CODE[kept].check(facts)
        if params is not None:
            return _BY_CODE[kept], params
    repeat: Optional[tuple[Kind, dict[str, Any]]] = None
    for kind in KINDS:
        params = kind.check(facts)
        if params is None:
            continue
        if kind.code == yesterday:
            repeat = repeat or (kind, params)
            continue
        return kind, params
    return repeat


def _utc_iso(local: dt.datetime, tz: int) -> str:
    return (local - dt.timedelta(hours=tz)).strftime("%Y-%m-%dT%H:%M:%SZ")


def render(choice: Optional[tuple[Kind, dict[str, Any]]], facts: Facts, tz: int) -> dict[str, Any]:
    """JSON-поле `coach_greeting` на текущем языке.

    `until` — момент (UTC), после которого фраза уже не обещана: конец
    местного дня или окна времени суток. Приложение показывает фразу из
    закэшированной сводки только до него — вчерашняя «Вчера отработал»
    сегодня была бы неправдой."""
    if choice is None:
        return {
            "kind": None,
            "title": i18n.t(_HEY),
            "text": None,
            "until": _utc_iso(_end_of_day(facts), tz),
        }
    kind, params = choice
    text_params = dict(params)
    if kind.code == "new_rank":
        text_params = {"level": params["level"], "rank": analytics.RANKS[params["level"]].name}
    until = kind.until(facts) if kind.until else _end_of_day(facts)
    return {
        "kind": kind.code,
        "title": i18n.t(kind.title_key, **params),
        "text": i18n.t(kind.text_key, **text_params),
        "until": _utc_iso(until, tz),
    }


async def _last_workout_events(
    user_id: int, user: Any, dates: list[dt.date], tz: int, excluded: tuple[int, ...]
) -> tuple[bool, Optional[int]]:
    """(рекорд на последней тренировке, звание, которое она дала).

    Звание «до» — без тренировок последнего дня и на ту же дату, «после» — с
    ними. Со временем звание само только падает (частота за 8 недель), так что
    «после > до» значит ровно одно: подняла его последняя тренировка. И оно
    должно держаться до сих пор — иначе «теперь ты …» уже неправда."""
    last = dates[-1]
    formula = user["e1rm_formula"]
    record = (
        await db.e1rm_record_count(
            user_id, last.isoformat(), formula, tz_offset=tz, exclude_workout_ids=excluded
        )
        > 0
    )
    agg = await db.hall_of_fame_aggregates(user_id, exclude_workout_ids=excluded)
    last_day = sum(
        (
            await db.daily_tonnage(
                user_id, last.isoformat(), last.isoformat(), tz_offset=tz,
                exclude_workout_ids=excluded,
            )
        ).values()
    )
    before_dates = [d for d in dates if d < last]
    unit = user["unit"]
    after = analytics.rank_for(
        len(dates), formatting.to_kg(agg["tonnage"], unit), analytics.workouts_per_week(dates, last)
    )
    before = analytics.rank_for(
        len(before_dates),
        formatting.to_kg(max(agg["tonnage"] - last_day, 0.0), unit),
        analytics.workouts_per_week(before_dates, last),
    )
    now = analytics.rank_for(
        len(dates),
        formatting.to_kg(agg["tonnage"], unit),
        analytics.workouts_per_week(dates, timeutil.user_today(user)),
    )
    promoted = after.level if after.level > before.level and now.level >= after.level else None
    return record, promoted


async def for_user(
    user_id: int, user: Any, *, exclude_workout_ids: Iterable[int] = ()
) -> Optional[dict[str, Any]]:
    """Поле `coach_greeting` для `/dashboard` или None, если тренировок нет.

    Зовётся под i18n.use_lang(users.lang) — текст уже на языке атлета.
    Выбор запоминается на местный день; пишем в базу, только когда он
    поменялся, — у сводки GET частый, и лишняя запись на каждый не нужна."""
    excluded = tuple(exclude_workout_ids)
    tz = timeutil.offset_hours(user)
    now = timeutil.user_now(user)
    today = now.date()
    dates = [
        dt.date.fromisoformat(d)
        for d in await db.list_finished_workout_dates(
            user_id, tz_offset=tz, exclude_workout_ids=excluded
        )
    ]
    if not dates:
        return None
    dashboard = analytics.compute_dashboard(dates, today)
    record, rank_up = False, None
    # Рекорд и звание имеют смысл только у свежей тренировки — у остальных
    # не стоит и ходить за ними в базу.
    if dashboard.days_since_last is not None and dashboard.days_since_last <= FRESH_DAYS:
        record, rank_up = await _last_workout_events(user_id, user, dates, tz, excluded)
    facts = Facts(now, tuple(dates), dashboard, record, rank_up)

    stored_day, stored_kind = user["coach_greeting_day"], user["coach_greeting_kind"]
    kept = stored_kind if stored_day == today.isoformat() else None
    yesterday = (
        stored_kind if stored_day == (today - dt.timedelta(days=1)).isoformat() else None
    )
    choice = pick(facts, kept, yesterday)
    code = choice[0].code if choice else None
    if user["coach_greeting_day"] != today.isoformat() or user["coach_greeting_kind"] != code:
        await db.update_user(
            user_id, coach_greeting_day=today.isoformat(), coach_greeting_kind=code
        )
    return render(choice, facts, tz)
