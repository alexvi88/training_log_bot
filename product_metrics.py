"""Продуктовая аналитика: события с постоянными именами и суточная сводка метрик.

Не путать с `analytics.py` — там метрики тренировок атлета (e1RM, тоннаж).
Здесь — как пользуются продуктом.

Зачем отдельно от `activity_log`. Лента `/activity` (db.user_events) — для
чтения глазами: фраза «записал подход: Жим · 100×8», введённый текст, месяц
жизни. По ней не построить график: переименуй фразу — ряд порвётся, а год
назад уже пусто. Здесь наоборот: имя события не меняется никогда, свойства —
короткие машинные значения из белого списка, текста человека нет.

**События** (`db.analytics_events`, `config.ANALYTICS_RETENTION_DAYS`):

- сервер пишет сам: `action` — меняющий запрос приложения (`route` — шаблон
  маршрута, `api_v1_activity`), `tg_message`/`tg_callback` — сообщение и
  нажатие в боте (`activity_log`: вид сообщения или команда, экран по префиксу
  callback_data);
- приложение присылает `POST /v1/events` (`api_v1_events`): открытие и уход в
  фон с длиной сессии, просмотр экрана, тап по пушу — то, чего сервер не видит:
  GET'ы в ленту не пишутся, а тап по баннеру до сервера не доходит вовсе.

**Сводка** (`db.daily_metrics`, хранится всегда): `rollup_day` складывает
сутки в строки (сутки, метрика) — активные (всего, в боте, в приложении), за
7 и 30 дней, новые, тренировавшиеся, подходы, закрытые тренировки, вызовы и
расход AI, пуши, открытия приложения, шаги воронки до входа, удержание когорт
на 1/7/30 день. Свои аккаунты (`config.limit_preview_ids`) в сводку не входят:
владелец, гоняющий бота руками, — это не рост.

`catch_up` досчитывает все пропущенные сутки с первого пользователя: у старых
дней «активные» — только по подходам (лог действий уже вычищен), и это честная
нижняя граница, а не ноль.

Смотреть — админская `/metrics` (handlers/admin.py): таблица за две недели и CSV
всей сводки.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from typing import Any, Optional

import config
import db

logger = logging.getLogger(__name__)

PLATFORM_TG = "tg"
PLATFORM_IOS = "ios"

# События, которые принимает POST /v1/events, и какие свойства у каждого. Новое
# событие приложения — сначала сюда, иначе сервер его молча пропустит.
CLIENT_EVENTS: dict[str, frozenset[str]] = {
    "app_open": frozenset(),
    "app_background": frozenset({"seconds"}),
    "screen_view": frozenset({"screen"}),
    "push_open": frozenset({"category"}),
    # Экран «Итог недели» (GET /v1/weekly-summary): откуда открыли — source
    # push|card — и какой был вердикт (kind из weekly_summary.KINDS). По паре
    # видно, сколько открывают из воскресного пуша и в какой вердикт
    # конвертируется старт тренировки.
    "weekly_summary_opened": frozenset({"source", "verdict"}),
    # Действие на этом экране: action start|records|lift|share|discuss.
    "weekly_summary_action": frozenset({"action", "verdict", "source"}),
    # Предложение плана под итогом тренировки (WorkoutFinishedSheet): action
    # shown|accept|later. По тройке видно, сколько видят предложение, сколько
    # идут к тренеру собирать программу и сколько откладывают на неделю.
    "plan_offer": frozenset({"action"}),
}

_TOKEN = re.compile(r"[a-z0-9_]{1,40}")
_MAX_VERSION_LEN = 32
# Сессия длиннее суток — сломанные часы, а не тренировка.
_MAX_SESSION_SECONDS = 24 * 3600

RETENTION_DAYS = (1, 7, 30)


def clean_token(value: Any) -> Optional[str]:
    """Машинное значение свойства: экран, категория пуша. `support_thread:12`
    → `support_thread` — id человека в аналитике не нужен."""
    if not isinstance(value, str):
        return None
    value = value.split(":", 1)[0].strip().lower()
    return value if _TOKEN.fullmatch(value) else None


def clean_props(event: str, props: Any) -> Optional[dict]:
    """Свойства из белого списка события; всё прочее выкидывается. None —
    событие незнакомое."""
    allowed = CLIENT_EVENTS.get(event)
    if allowed is None:
        return None
    props = props if isinstance(props, dict) else {}
    out: dict[str, Any] = {}
    for key in sorted(allowed):
        value = props.get(key)
        if key == "seconds":
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and 0 <= value <= _MAX_SESSION_SECONDS
            ):
                out[key] = int(value)
        else:
            token = clean_token(value)
            if token:
                out[key] = token
    return out


def clean_version(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()[:_MAX_VERSION_LEN]
    return value or None


def props_json(props: Optional[dict]) -> Optional[str]:
    return json.dumps(props, separators=(",", ":"), sort_keys=True) if props else None


async def track(
    user_id: int,
    event: str,
    props: Optional[dict] = None,
    *,
    platform: str,
    app_version: Optional[str] = None,
) -> None:
    """Одно серверное событие. Не бросает: аналитика — не повод сорвать
    запись подхода. Сбой — в лог (и тревогой админу, ops_alerts)."""
    try:
        await db.log_analytics_events(
            [(user_id, event, props_json(props), platform, clean_version(app_version), None)]
        )
    except Exception:
        logger.exception("product_metrics: не записал событие %s", event)


# --- суточная сводка ------------------------------------------------------------


def _day(value: dt.date) -> str:
    return value.isoformat()


async def _active(
    day: dt.date, own: set[int], cache: Optional[dict[str, dict[int, set[str]]]] = None
) -> dict[int, set[str]]:
    """Активные за сутки без своих аккаунтов. `cache` — на досчёт истории:
    окно в 30 дней у соседних суток почти целиком общее."""
    key = _day(day)
    if cache is not None and key in cache:
        return cache[key]
    active = {
        uid: platforms
        for uid, platforms in (await db.active_users_by_platform(key)).items()
        if uid not in own
    }
    if cache is not None:
        cache[key] = active
    return active


async def rollup_day(
    day: dt.date, cache: Optional[dict[str, dict[int, set[str]]]] = None
) -> dict[str, float]:
    """Сложить сутки `day` в daily_metrics; заодно — удержание когорт, чей
    N-й день пришёлся на эти сутки. Идемпотентно: пересчёт перезаписывает."""
    own = config.limit_preview_ids()
    active = await _active(day, own, cache)
    metrics = await db.day_activity_counts(_day(day), own)
    metrics["active_users"] = len(active)
    metrics["active_tg"] = sum(1 for p in active.values() if PLATFORM_TG in p)
    metrics["active_ios"] = sum(1 for p in active.values() if PLATFORM_IOS in p)
    metrics["new_users"] = len((await db.users_created_on(_day(day))) - own)
    metrics["ai_cost_usd"] = round(await db.get_cost_total_usd(_day(day)), 4)

    window: set[int] = set(active)
    for back in range(1, 30):
        window |= set(await _active(day - dt.timedelta(days=back), own, cache))
        if back == 6:
            metrics["wau"] = len(window)
    metrics["mau"] = len(window)

    await db.upsert_daily_metrics(_day(day), metrics)

    for n in RETENTION_DAYS:
        cohort_day = day - dt.timedelta(days=n)
        cohort = (await db.users_created_on(_day(cohort_day))) - own
        if not cohort:
            continue
        await db.upsert_daily_metrics(
            _day(cohort_day),
            {f"cohort_d{n}": len(cohort), f"retained_d{n}": len(cohort & set(active))},
        )
    return metrics


async def catch_up(today: Optional[dt.date] = None) -> int:
    """Досчитать все сутки до вчерашних, которых в сводке ещё нет. Вчерашние
    пересчитываются всегда: подходы из офлайн-очереди приложения доезжают и
    после полуночи."""
    today = today or dt.date.today()
    first = await db.earliest_user_day()
    if first is None:
        return 0
    done = await db.daily_metrics_days()
    day = dt.date.fromisoformat(first)
    yesterday = today - dt.timedelta(days=1)
    count = 0
    cache: dict[str, dict[int, set[str]]] = {}
    while day <= yesterday:
        if _day(day) not in done or day == yesterday:
            await rollup_day(day, cache)
            count += 1
        day += dt.timedelta(days=1)
    return count


async def run_daily_metrics_job() -> None:
    """Раз в час — досчитать пропущенное. Первый проход после полуночи кладёт
    в сводку только что кончившиеся сутки."""
    while True:
        try:
            await catch_up()
        except Exception:
            logger.exception("product_metrics: суточная сводка не посчиталась")
        await asyncio.sleep(3600)


async def pivot(since: Optional[str] = None) -> tuple[list[str], list[dict[str, Any]]]:
    """Сводка таблицей: (имена метрик, строки по суткам) — для /metrics и CSV."""
    rows = await db.get_daily_metrics(since)
    by_day: dict[str, dict[str, Any]] = {}
    names: set[str] = set()
    for row in rows:
        by_day.setdefault(row["day"], {"day": row["day"]})[row["metric"]] = row["value"]
        names.add(row["metric"])
    return sorted(names), [by_day[d] for d in sorted(by_day)]
