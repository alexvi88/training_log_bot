"""Забытая тренировка в приложении: напомнить, потом закрыть самим.

Зачем. По воронке iOS за октябрь 2026 подход записала четверть новичков, а
закрыла первую тренировку только половина из них: человек ушёл из зала, не
нажав «Завершить», и тренировка висела открытой. Для всех счётчиков — серии,
звания, значков, «закрыл первую тренировку» — её не было. В боте на такой
случай есть предупреждение при следующем сообщении (handlers.workout,
`STALE_WORKOUT_HOURS`), в приложении — ничего.

Что делает (`tick`, раз в `CHECK_SECONDS`), только для тех, у кого есть
приложение (`db.is_app_user`) — у бота свой путь с кнопками на предупреждении:

1. Подходов нет `config.WORKOUT_REMIND_IDLE_MINUTES` — баннер «Закрыть
   тренировку?» с числом подходов, один на тренировку
   (`db.claim_workout_finish_reminder`). Ночью по местному времени — нет
   (если пояс известен, см. engagement.is_quiet_hour): будить незачем,
   закрытие ниже всё равно случится.
2. Подходов нет `config.WORKOUT_AUTO_FINISH_IDLE_HOURS` — тренировка
   закрывается: концом ставится время последнего подхода (тренировка правда
   была, а «сейчас» растянуло бы её на часы), значки и живой тренер — как у
   обычного «Завершить» в /v1. Запись во «Уведомлениях» без баннера: итоги
   человек увидит, когда откроет приложение.

События жизненного цикла (`workout_finished`) пишутся от клиента `ios`
(`lifecycle_events.using`): иначе фоновая задача их бы не писала, и закрытые
так тренировки не попали бы в воронку, ради которой это и сделано.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any, Optional

import config
import db
import i18n
import lifecycle_events

logger = logging.getLogger(__name__)

CHECK_SECONDS = 600
CATEGORY_REMINDER = "finish_reminder"
CATEGORY_AUTO_FINISHED = "auto_finished"
ROUTE_WORKOUT = {"screen": "workout"}


def _utc_now() -> dt.datetime:
    """Naive UTC, как sets.created_at — вынесено, чтобы тест мог прибить часы."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _parse(value: str) -> dt.datetime:
    moment = dt.datetime.fromisoformat(value)
    return moment.astimezone(dt.timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


async def run_job() -> None:
    while True:
        await asyncio.sleep(CHECK_SECONDS)
        try:
            await tick()
        except Exception:
            logger.exception("workout autoclose tick failed")


async def tick(now: Optional[dt.datetime] = None) -> tuple[int, int]:
    """Один проход. Возвращает (сколько напомнили, сколько закрыли)."""
    now = now or _utc_now()
    remind_after = dt.timedelta(minutes=config.WORKOUT_REMIND_IDLE_MINUTES)
    finish_after = dt.timedelta(hours=config.WORKOUT_AUTO_FINISH_IDLE_HOURS)
    reminded = finished = 0
    for row in await db.list_idle_active_workouts((now - remind_after).isoformat(timespec="seconds")):
        if not await db.is_app_user(row["user_id"]):
            continue
        idle = now - _parse(row["last_set_at"])
        try:
            if idle >= finish_after:
                finished += await _auto_finish(row)
            elif await _remind(row, now):
                reminded += 1
        except Exception:
            logger.exception("workout autoclose failed for workout %s", row["id"])
    return reminded, finished


async def _user_lang(user: Any) -> str:
    return user["lang"] if user is not None and user["lang"] else i18n.DEFAULT_LANG


async def _remind(row: Any, now: dt.datetime) -> bool:
    import apns
    import engagement

    user = await db.get_user(row["user_id"])
    if user is None:
        return False
    tz = user["tz_offset"] or 0
    if engagement.tz_is_known(tz) and engagement.is_quiet_hour((now + dt.timedelta(hours=tz)).hour):
        return False
    if not await db.claim_workout_finish_reminder(row["id"]):
        return False
    lang = await _user_lang(user)
    with i18n.use_lang(lang):
        title = i18n.t("push.ios.finish_reminder.title")
        body = i18n.t("push.ios.finish_reminder.body", sets=int(row["sets"]))
    if not apns.is_configured():
        return True
    for token in await db.get_push_tokens(row["user_id"], "ios"):
        await apns.send_alert(row["user_id"], token, title, body, category=CATEGORY_REMINDER, route=ROUTE_WORKOUT)
    logger.info("workout autoclose: reminded user %s about workout %s", row["user_id"], row["id"])
    return True


async def _auto_finish(row: Any) -> int:
    import achievement_sync
    import coach_notify
    import inbox
    import view_builder

    workout_id, user_id = row["id"], row["user_id"]
    workout = await db.get_workout(workout_id)
    if workout is None or workout["status"] != "active":
        return 0
    await db.delete_empty_blocks(workout_id)
    with lifecycle_events.using(lifecycle_events.PLATFORM_IOS):
        if not await db.finish_workout(workout_id, note=workout["note"], finished_at=row["last_set_at"]):
            return 0
    await coach_notify.notify_client_workout_finished(user_id, workout_id)
    duration = await view_builder.workout_duration_seconds(await db.get_workout(workout_id))
    await achievement_sync.evaluate_after_finish(
        user_id, workout_id, dt.datetime.fromisoformat(workout["started_at"]), duration
    )
    user = await db.get_user(user_id)
    lang = await _user_lang(user)
    with i18n.use_lang(lang):
        title = i18n.t("push.ios.auto_finished.title")
        body = i18n.t("push.ios.auto_finished.body", sets=int(row["sets"]))
    await inbox.deliver(user_id, CATEGORY_AUTO_FINISHED, title, body, lang=lang, route=ROUTE_WORKOUT, push=False)
    logger.info("workout autoclose: finished workout %s of user %s (%s sets)", workout_id, user_id, row["sets"])
    return 1
