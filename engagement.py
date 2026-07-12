"""Daily engagement pushes: signal detection + delivery, in the 'Привет Атлет' voice.

See PUSH_IDEAS.md for the full rationale. Short version: a push only earns its
slot when the signal fires while the user isn't in the app — so this module
never touches anything that's already visible in a just-finished workout
screen (that's `handlers/workout.py`'s job). Priority order below (first
match wins, at most one push per user per day):

  1. Серия на кону   — weekend only, a running week-streak about to break
  2. Пропуск         — exact day-since-last-workout milestones (jabs live here)
  3. Возвращение     — 21+ days gone, then every 10 days
  4. Тайминг         — today matches the user's usual training weekday
  5. Плато           — Sundays only, weight stuck despite 12+ reps
  6. Аналитика       — Sundays only, weekly digest
  7. Челлендж        — Mondays (kickoff) or Thursdays if behind pace (progress nudge)

The transactional post-workout followup (hydration/protein reminder, 2h
after finishing) is handled separately by run_followup_job — it doesn't
compete for the one-a-day slot.
"""

import asyncio
import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

import analytics
import config
import db
import keyboards
import push_texts

logger = logging.getLogger(__name__)

SKIP_MILESTONE_DAYS = (3, 5, 7, 10, 14)
WIN_BACK_START_DAY = 21
WIN_BACK_REPEAT_DAYS = 10
TIMING_MIN_HISTORY = 10
PLATEAU_MIN_REPS = 12
PLATEAU_SESSIONS = 3
DIGEST_LOOKBACK_DAYS = 30
CHALLENGE_ON_PACE_WORKOUTS = 2  # Thursday nudge fires if fewer than this many workouts logged this week


@dataclass
class PushDecision:
    category: str
    text: str
    with_cta: bool = True


# ---------- pure signal detectors (no I/O, easy to unit test) ----------

def is_streak_at_risk(dashboard: analytics.Dashboard, today: dt.date) -> bool:
    return today.weekday() >= 5 and dashboard.week_streak >= 2 and dashboard.this_week == 0


def skip_milestone(days_since_last: Optional[int]) -> Optional[int]:
    if days_since_last in SKIP_MILESTONE_DAYS:
        return days_since_last
    return None


def is_win_back_day(days_since_last: Optional[int]) -> bool:
    if days_since_last is None or days_since_last < WIN_BACK_START_DAY:
        return False
    return (days_since_last - WIN_BACK_START_DAY) % WIN_BACK_REPEAT_DAYS == 0


def usual_weekday(workout_dates: list[dt.date]) -> Optional[int]:
    if len(workout_dates) < TIMING_MIN_HISTORY:
        return None
    counts = Counter(d.weekday() for d in workout_dates)
    weekday, _ = counts.most_common(1)[0]
    return weekday


def _session_top_weight_and_min_reps(session: analytics.SessionStats) -> tuple[float, int]:
    top_weight = max((s.weight for s in session.sets), default=0.0)
    reps_at_top = [s.reps for s in session.sets if s.weight == top_weight]
    return top_weight, min(reps_at_top, default=0)


def is_plateau(sessions: list[analytics.SessionStats]) -> bool:
    """Same working weight for the last 3 sessions, each with 12+ reps.

    This is deliberately NOT "stuck, back off" — 12+ reps means the athlete
    is nowhere near failure, so the fix is adding weight, not deloading.
    """
    if len(sessions) < PLATEAU_SESSIONS:
        return False
    last = sessions[-PLATEAU_SESSIONS:]
    stats = [_session_top_weight_and_min_reps(s) for s in last]
    weights = {w for w, _ in stats}
    if len(weights) != 1:
        return False
    (weight,) = weights
    if weight <= 0:
        return False
    return all(reps >= PLATEAU_MIN_REPS for _, reps in stats)


def format_tonnage(kg: float) -> str:
    if kg >= 1000:
        return f"{kg / 1000:.1f} т"
    return f"{kg:.0f} кг"


# ---------- orchestration (I/O) ----------

async def _find_plateau_exercise(telegram_id: int) -> Optional[str]:
    for ex in await db.list_user_exercises(telegram_id):
        rows = await db.list_sets_for_exercise(ex["id"])
        if len(rows) < PLATEAU_SESSIONS:
            continue
        set_rows = [
            analytics.SetRow(r["weight"], r["reps"], r["workout_id"], r["started_at"]) for r in rows
        ]
        sessions = analytics.group_sets_by_session(set_rows)
        if is_plateau(sessions):
            return ex["display_name"]
    return None


async def build_daily_push(telegram_id: int, today: dt.date) -> Optional[PushDecision]:
    if await db.has_push_today(telegram_id, today.isoformat()):
        return None

    date_strings = await db.list_finished_workout_dates(telegram_id)
    if not date_strings:
        return None
    dates = [dt.date.fromisoformat(s) for s in date_strings]
    dashboard = analytics.compute_dashboard(dates, today)

    if is_streak_at_risk(dashboard, today):
        text = await push_texts.pick_text(
            telegram_id,
            push_texts.STREAK_AT_RISK,
            weeks=dashboard.week_streak,
            days_left="сегодня и завтра" if today.weekday() == 5 else "последний день",
        )
        return PushDecision(push_texts.STREAK_AT_RISK, text)

    if skip_milestone(dashboard.days_since_last) is not None:
        text = await push_texts.pick_text(telegram_id, push_texts.SKIP)
        return PushDecision(push_texts.SKIP, text)

    if is_win_back_day(dashboard.days_since_last):
        text = await push_texts.pick_text(telegram_id, push_texts.WIN_BACK)
        return PushDecision(push_texts.WIN_BACK, text)

    if dates[-1] != today and usual_weekday(dates) == today.weekday():
        text = await push_texts.pick_text(telegram_id, push_texts.TIMING)
        return PushDecision(push_texts.TIMING, text)

    if today.weekday() == 6:  # Sunday
        exercise_name = await _find_plateau_exercise(telegram_id)
        if exercise_name:
            text = await push_texts.pick_text(telegram_id, push_texts.PLATEAU, exercise=exercise_name)
            return PushDecision(push_texts.PLATEAU, text)

        since = (today - dt.timedelta(days=DIGEST_LOOKBACK_DAYS)).isoformat()
        tonnage = await db.tonnage_since(telegram_id, since)
        if tonnage > 0:
            text = await push_texts.pick_text(
                telegram_id, push_texts.WEEKLY_DIGEST,
                tonnage=format_tonnage(tonnage), week_count=dashboard.this_week,
            )
            return PushDecision(push_texts.WEEKLY_DIGEST, text, with_cta=False)

    if today.weekday() == 0:  # Monday kickoff
        text = await push_texts.pick_text(telegram_id, push_texts.CHALLENGE)
        return PushDecision(push_texts.CHALLENGE, text)

    if today.weekday() == 3 and dashboard.this_week < CHALLENGE_ON_PACE_WORKOUTS:  # Thursday progress check
        text = await push_texts.pick_text(telegram_id, push_texts.CHALLENGE)
        return PushDecision(push_texts.CHALLENGE, text)

    return None


async def _deliver(bot: Bot, telegram_id: int, decision: PushDecision) -> None:
    kb = keyboards.push_cta_keyboard() if decision.with_cta else None
    try:
        await bot.send_message(chat_id=telegram_id, text=decision.text, reply_markup=kb)
    except TelegramForbiddenError:
        logger.info("User %s blocked the bot, skipping push", telegram_id)
        return
    await db.record_push(telegram_id, decision.category, decision.text)


async def _send_daily_pushes(bot: Bot) -> None:
    today = dt.date.today()
    for telegram_id in await db.list_user_ids_with_workouts():
        try:
            decision = await build_daily_push(telegram_id, today)
        except Exception:
            logger.exception("Failed to build push for user %s", telegram_id)
            continue
        if decision is not None:
            await _deliver(bot, telegram_id, decision)


def _seconds_until_next_run(hour: int) -> float:
    now = dt.datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


async def run_daily_engagement_job(bot: Bot) -> None:
    if not config.ENGAGEMENT_ENABLED:
        return
    while True:
        await asyncio.sleep(_seconds_until_next_run(config.ENGAGEMENT_HOUR))
        try:
            await _send_daily_pushes(bot)
        except Exception:
            logger.exception("Daily engagement job failed")


async def _send_due_followups(bot: Bot) -> None:
    for workout in await db.list_due_followups(db.now_iso()):
        text = await push_texts.pick_text(workout["user_id"], push_texts.FOLLOWUP)
        try:
            await bot.send_message(chat_id=workout["user_id"], text=text)
        except TelegramForbiddenError:
            logger.info("User %s blocked the bot, skipping followup", workout["user_id"])
        else:
            await db.record_push(workout["user_id"], push_texts.FOLLOWUP, text)
        await db.mark_followup_sent(workout["id"])


async def run_followup_job(bot: Bot) -> None:
    if not config.ENGAGEMENT_ENABLED:
        return
    while True:
        await asyncio.sleep(config.FOLLOWUP_POLL_MINUTES * 60)
        try:
            await _send_due_followups(bot)
        except Exception:
            logger.exception("Followup job failed")
