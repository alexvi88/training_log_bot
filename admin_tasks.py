"""Daily admin job: usage stats + a DB backup, sent to ADMIN_ID via Telegram."""

import asyncio
import datetime as dt
import logging
import os
import tempfile

from aiogram import Bot
from aiogram.types import FSInputFile

import config
import db

logger = logging.getLogger(__name__)


def _seconds_until_next_run(hour: int) -> float:
    now = dt.datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


async def _send_daily_report(bot: Bot) -> None:
    yesterday = dt.date.today() - dt.timedelta(days=1)
    stats = await db.daily_workout_stats(yesterday.isoformat())
    await bot.send_message(
        chat_id=config.ADMIN_ID,
        text=(
            f"📊 Статистика за {yesterday.strftime('%d.%m.%Y')}\n"
            f"Потренировалось пользователей: {stats['users']}\n"
            f"Завершено тренировок: {stats['workouts']}"
        ),
    )

    backup_name = f"training_log_backup_{dt.date.today().isoformat()}.db"
    backup_path = os.path.join(tempfile.gettempdir(), backup_name)
    if os.path.exists(backup_path):
        os.remove(backup_path)
    try:
        await db.backup_to_file(backup_path)
        await bot.send_document(chat_id=config.ADMIN_ID, document=FSInputFile(backup_path, filename=backup_name))
    finally:
        if os.path.exists(backup_path):
            os.remove(backup_path)


async def run_daily_admin_jobs(bot: Bot) -> None:
    if not config.ADMIN_ID:
        return
    while True:
        await asyncio.sleep(_seconds_until_next_run(config.ADMIN_REPORT_HOUR))
        try:
            await _send_daily_report(bot)
        except Exception:
            logger.exception("Daily admin report/backup failed")
