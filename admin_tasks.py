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


def _llm_cost(llm_breakdown: dict[str, dict[str, int]]) -> tuple[float, int, int]:
    total_cost = 0.0
    total_calls = 0
    total_tokens = 0
    for model, stats in llm_breakdown.items():
        inp, out = config.LLM_PRICES_USD_PER_1K.get(model, config.DEFAULT_LLM_PRICE_USD_PER_1K)
        total_cost += stats["prompt_tokens"] / 1000 * inp + stats["completion_tokens"] / 1000 * out
        total_calls += stats["calls"]
        total_tokens += stats["prompt_tokens"] + stats["completion_tokens"]
    return total_cost, total_calls, total_tokens


async def _build_cost_report(date_str: str) -> str:
    """LLM cost breakdown for the given calendar day — real per-call token usage
    (db.cost_events, logged from ai_trainer.py) priced against
    config.LLM_PRICES_USD_PER_1K, same pattern as github.com/alexvi88/fun_bot's
    analytics.build_report."""
    llm_breakdown = await db.get_llm_cost_breakdown(date_str)
    transcriptions = await db.get_transcription_count(date_str)

    llm_cost, llm_calls, llm_tokens = _llm_cost(llm_breakdown)
    transcription_cost = transcriptions * config.TRANSCRIPTION_PRICE_USD_PER_CALL
    total_cost = llm_cost + transcription_cost

    lines = [
        "",
        "🤖 AI-тренер",
        f"LLM-вызовов: {llm_calls} (~${llm_cost:.2f}, {llm_tokens:,} ток.)".replace(",", " "),
    ]
    for model, stats in sorted(llm_breakdown.items(), key=lambda x: -x[1]["calls"]):
        tok = stats["prompt_tokens"] + stats["completion_tokens"]
        lines.append(f"  └ {model}: {stats['calls']} ({tok:,} ток.)".replace(",", " "))
    if transcriptions:
        lines.append(f"Голосовых распознано: {transcriptions} (~${transcription_cost:.2f})")
    lines.append(f"💸 Итого расходы: ~${total_cost:.2f} (~${total_cost * 30:.0f}/мес)")
    return "\n".join(lines)


async def _send_daily_report(bot: Bot) -> None:
    yesterday = dt.date.today() - dt.timedelta(days=1)
    yesterday_str = yesterday.isoformat()
    stats = await db.daily_workout_stats(yesterday_str)
    cost_report = await _build_cost_report(yesterday_str)
    await bot.send_message(
        chat_id=config.ADMIN_ID,
        text=(
            f"📊 Статистика за {yesterday.strftime('%d.%m.%Y')}\n"
            f"Потренировалось пользователей: {stats['users']}\n"
            f"Завершено тренировок: {stats['workouts']}"
            f"{cost_report}"
        ),
    )
    await db.prune_old_cost_events(config.COST_EVENTS_RETENTION_DAYS)

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
