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
на 1/7/30 день.

Пуши в сводке. `pushes_sent` — строки `pushes` (решения «пушу быть», как
было); по каналам — `pushes_sent_tg` (сообщение в Telegram реально ушло,
`pushes.channel`) и `pushes_sent_ios` (Apple принял баннер — серверное событие
`push_sent` из `apns.send_alert`, по одному на устройство), `push_failed_ios`
(Apple не принял, мёртвые токены тоже). `push_users` — разных людей с хотя бы
одной доставкой, `pushes_per_user` — (tg + ios) / push_users. `push_ctr_ios` —
тапы `push_open` / доставленные iOS (тап в Telegram до сервера не доходит).
`push_to_workout_24h` — доля получивших пуш, кто начал или закончил
тренировку в 24 часа после него; пишется на сутки позже, когда окно закрыто.
По категориям — `pushes_sent:<категория>` и `push_opens:<категория>` (только в
CSV). Сводку пушей за вчера утренний отчёт админу (`admin_tasks`) показывает
тем же расчётом (`push_stats`). Свои аккаунты (`config.limit_preview_ids`) в сводку не входят:
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

# Серверное событие: баннер APNs ушёл на одно устройство (apns.send_alert),
# props category и ok (принял ли Apple). Приложение его не присылает.
PUSH_SENT_EVENT = "push_sent"
# Категория, не похожая на машинный токен, — в общий ящик, а не в отдельный ряд.
OTHER_CATEGORY = "other"

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
    # Сервис для тренеров: тренер отправил код приглашения (action shared|copied).
    # Сама связка, назначение и комментарий видны и так — меняющими запросами
    # (`action` с шаблоном маршрута /coach*).
    "coach_invite": frozenset({"action"}),
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


def push_category(value: Any) -> str:
    """Категория пуша для сводки: короткий токен или `other`."""
    return clean_token(value) or OTHER_CATEGORY


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


def _moment(value: Optional[str]) -> Optional[dt.datetime]:
    """Метка времени из базы → naive UTC. С поясом (время от телефона) —
    переводится; без пояса — уже UTC, как now_iso."""
    if not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return moment


async def push_stats(day: dt.date, own: set[int]) -> dict[str, Any]:
    """Пуши суток без своих аккаунтов — и для сводки, и для утреннего отчёта
    админу (admin_tasks). `categories` — {категория: {sent, ios, opens}}:
    sent — доставлено по обоим каналам, ios — из них на iOS, opens — тапы."""
    rows = await db.push_deliveries(_day(day), own)
    sent_tg = sent_ios = failed_ios = 0
    users: set[int] = set()
    categories: dict[str, dict[str, int]] = {}
    for uid, channel, category, _, ok in rows:
        if not ok:
            failed_ios += 1
            continue
        users.add(uid)
        cat = categories.setdefault(push_category(category), {"sent": 0, "ios": 0, "opens": 0})
        cat["sent"] += 1
        if channel == PLATFORM_IOS:
            sent_ios += 1
            cat["ios"] += 1
        else:
            sent_tg += 1
    opens = await db.push_open_categories(_day(day), own)
    for category in opens:
        cat = categories.setdefault(push_category(category), {"sent": 0, "ios": 0, "opens": 0})
        cat["opens"] += 1
    return {
        "sent_tg": sent_tg,
        "sent_ios": sent_ios,
        "failed_ios": failed_ios,
        "users": len(users),
        "opens": len(opens),
        "categories": categories,
    }


def push_metrics(stats: dict[str, Any]) -> dict[str, float]:
    """Строки сводки из push_stats. Доли с пустым знаменателем не пишутся
    вовсе: «0% CTR» в день без iOS-пушей — неправда, а пустая клетка — нет."""
    total = stats["sent_tg"] + stats["sent_ios"]
    out: dict[str, float] = {
        "pushes_sent_tg": stats["sent_tg"],
        "pushes_sent_ios": stats["sent_ios"],
        "push_failed_ios": stats["failed_ios"],
        "push_users": stats["users"],
    }
    if stats["users"]:
        out["pushes_per_user"] = round(total / stats["users"], 2)
    if stats["sent_ios"]:
        out["push_ctr_ios"] = round(stats["opens"] / stats["sent_ios"], 4)
    for category, counts in stats["categories"].items():
        out[f"pushes_sent:{category}"] = counts["sent"]
        out[f"push_opens:{category}"] = counts["opens"]
    return out


async def push_to_workout(day: dt.date, own: set[int]) -> tuple[int, int]:
    """(сколько начали или закончили тренировку в 24 часа после любого своего
    пуша суток `day`, сколько вообще получили пуш в эти сутки).

    Окно пуша, отправленного в 23:00, кончается в 23:00 следующих суток —
    поэтому досчитывать это можно только когда кончились и они: rollup_day(D)
    пишет значение в строку D−1 (см. там)."""
    pushed: dict[int, list[dt.datetime]] = {}
    for uid, _, _, sent_at, ok in await db.push_deliveries(_day(day), own):
        moment = _moment(sent_at)
        if ok and moment is not None:
            pushed.setdefault(uid, []).append(moment)
    if not pushed:
        return 0, 0
    converted: set[int] = set()
    window_end = _day(day + dt.timedelta(days=2))
    for uid, value in await db.workout_moments(set(pushed), _day(day), window_end):
        moment = _moment(value)
        if moment is None or uid in converted:
            continue
        if any(p <= moment <= p + dt.timedelta(hours=24) for p in pushed[uid]):
            converted.add(uid)
    return len(converted), len(pushed)


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

    metrics.update(push_metrics(await push_stats(day, own)))

    await db.upsert_daily_metrics(_day(day), metrics)

    # Пуш → тренировка за 24 часа — с отставанием на сутки и в строку D−1.
    # Окно пуша суток D−1 кончается не позже конца суток D, а rollup_day(D)
    # идёт уже после них (catch_up считает только кончившиеся сутки) — значит,
    # окно закрыто целиком. Считать это для самих суток D значило бы писать
    # заниженную долю, которую потом молча переписывают. У последних суток
    # сводки клетка пустая, пока не кончатся следующие; досчёт истории
    # заполняет каждую строку, когда проходит по следующей за ней.
    prev = day - dt.timedelta(days=1)
    converted, pushed = await push_to_workout(prev, own)
    if pushed:
        await db.upsert_daily_metrics(
            _day(prev), {"push_to_workout_24h": round(converted / pushed, 4)}
        )

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
