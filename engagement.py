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

A separate track, `build_newbie_push`, walks a disjoint pool: users who signed
up but never finished a single workout. Since these users have no last-workout
date, none of the six signals above apply to them (they all key off workout
history) — they get their own periodic nudge timed off `users.created_at` instead.
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
import formatting
import keyboards
import push_texts

logger = logging.getLogger(__name__)

WIN_BACK_START_DAY = 21
WIN_BACK_REPEAT_DAYS = 10
TIMING_MIN_HISTORY = 10
PLATEAU_MIN_REPS = 12
PLATEAU_SESSIONS = 3
DIGEST_LOOKBACK_DAYS = 30
NEWBIE_START_DAY = 1
NEWBIE_REPEAT_DAYS = 5
NEWBIE_STOP_DAY = 30


@dataclass
class PushDecision:
    category: str
    text: str
    with_cta: bool = True


# ---------- pure signal detectors (no I/O, easy to unit test) ----------

def is_streak_at_risk(dashboard: analytics.Dashboard, today: dt.date) -> bool:
    return today.weekday() >= 5 and dashboard.week_streak >= 2 and dashboard.this_week == 0


def skip_milestone(days_since_last: Optional[int]) -> Optional[int]:
    if days_since_last in push_texts.SKIP_MILESTONE_DAYS:
        return days_since_last
    return None


def is_win_back_day(days_since_last: Optional[int]) -> bool:
    if days_since_last is None or days_since_last < WIN_BACK_START_DAY:
        return False
    return (days_since_last - WIN_BACK_START_DAY) % WIN_BACK_REPEAT_DAYS == 0


def is_newbie_nudge_day(days_since_signup: int) -> bool:
    """First nudge a day after signup, then every NEWBIE_REPEAT_DAYS, capped at NEWBIE_STOP_DAY.

    The cap matters: a user who never starts isn't nagged forever, just for a month.
    """
    if days_since_signup < NEWBIE_START_DAY or days_since_signup > NEWBIE_STOP_DAY:
        return False
    return (days_since_signup - NEWBIE_START_DAY) % NEWBIE_REPEAT_DAYS == 0


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

    milestone_day = skip_milestone(dashboard.days_since_last)
    if milestone_day is not None:
        category = push_texts.SKIP_CATEGORY_BY_DAY[milestone_day]
        text = await push_texts.pick_text(telegram_id, category)
        return PushDecision(category, text)

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
            week_word = formatting.plural_ru(dashboard.this_week, ("тренировка", "тренировки", "тренировок"))
            text = await push_texts.pick_text(
                telegram_id, push_texts.WEEKLY_DIGEST,
                tonnage=format_tonnage(tonnage), week_count=f"{dashboard.this_week} {week_word}",
            )
            return PushDecision(push_texts.WEEKLY_DIGEST, text, with_cta=False)

    return None


async def build_newbie_push(telegram_id: int, created_at: str, today: dt.date) -> Optional[PushDecision]:
    if await db.has_push_today(telegram_id, today.isoformat()):
        return None
    signup_date = dt.date.fromisoformat(created_at[:10])
    days_since_signup = (today - signup_date).days
    if not is_newbie_nudge_day(days_since_signup):
        return None
    text = await push_texts.pick_text(telegram_id, push_texts.NEWBIE_NUDGE)
    return PushDecision(push_texts.NEWBIE_NUDGE, text)


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
    for telegram_id in await db.list_engagement_eligible_user_ids():
        try:
            decision = await build_daily_push(telegram_id, today)
        except Exception:
            logger.exception("Failed to build push for user %s", telegram_id)
            continue
        if decision is not None:
            await _deliver(bot, telegram_id, decision)

    for telegram_id, created_at in await db.list_newbie_user_ids():
        try:
            decision = await build_newbie_push(telegram_id, created_at, today)
        except Exception:
            logger.exception("Failed to build newbie push for user %s", telegram_id)
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
