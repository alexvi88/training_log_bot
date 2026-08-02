"""Daily engagement pushes: signal detection + delivery, in the 'Привет Атлет' voice.

See PUSH_IDEAS.md for the full rationale. Short version: a push only earns its
slot when the signal fires while the user isn't in the app — so this module
never touches anything that's already visible in a just-finished workout
screen (that's `handlers/workout.py`'s job). Priority order below (first
match wins, at most one push per user per day):

  1. Серия на кону   — weekend only, a running week-streak about to break
  2. Пропуск         — exact day-since-last-workout milestones (jabs live here)
  3. Возвращение     — 21+ days gone, then every 10 days
  4. Плато           — Sundays only, weight stuck despite 12+ reps
  5. Аналитика       — Sundays only, weekly digest

Every push is delivered as a photo (the same fixed "coach" image) with the
push text as its caption.

A separate track, `build_newbie_push`, walks a disjoint pool: users who signed
up but never finished a single workout. Since these users have no last-workout
date, none of the five signals above apply to them (they all key off workout
history) — they get their own periodic nudge timed off `users.created_at` instead.
"""

import asyncio
import datetime as dt
import logging
import os
from dataclasses import dataclass
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import FSInputFile

import ai_trainer
import analytics
import config
import db
import formatting
import keyboards
import push_texts

logger = logging.getLogger(__name__)

PUSH_IMAGE_PATH = os.path.join(os.path.dirname(__file__), "media", "push", "coach_incoming_call.jpg")
# Telegram hands back a file_id on first upload; re-sending that id costs no
# upload at all, and every push uses the same image.
_push_image_file_id: str | None = None

WIN_BACK_START_DAY = 21
WIN_BACK_REPEAT_DAYS = 10
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
        return f"{kg / 1000:.1f}т"
    return f"{kg:.0f}кг"


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

    if today.weekday() == 6:  # Sunday
        exercise_name = await _find_plateau_exercise(telegram_id)
        if exercise_name:
            text = await push_texts.pick_text(telegram_id, push_texts.PLATEAU, exercise=exercise_name)
            return PushDecision(push_texts.PLATEAU, text)

        since = (today - dt.timedelta(days=DIGEST_LOOKBACK_DAYS)).isoformat()
        tonnage = await db.tonnage_since(telegram_id, since)
        if tonnage > 0:
            ai_text = await _ai_weekly_digest_text(telegram_id)
            if ai_text:
                return PushDecision(push_texts.AI_WEEKLY, ai_text)
            week_word = formatting.plural_ru(dashboard.this_week, ("тренировка", "тренировки", "тренировок"))
            # None when no weekday clearly stands out — pick_text then drops the
            # variant that would have claimed one, instead of asserting a habit
            # the history doesn't show.
            best_weekday = analytics.most_frequent_weekday(dates)
            text = await push_texts.pick_text(
                telegram_id, push_texts.WEEKLY_DIGEST,
                tonnage=format_tonnage(tonnage), week_count=f"{dashboard.this_week} {week_word}",
                best_day=(
                    formatting.WEEKDAY_NAMES_RU[best_weekday] if best_weekday is not None else None
                ),
            )
            return PushDecision(push_texts.WEEKLY_DIGEST, text, with_cta=False)

    return None


async def _ai_weekly_digest_text(telegram_id: int) -> Optional[str]:
    """Personalized AI weekly digest, or None to fall back to the static rotation copy."""
    if not config.AI_WEEKLY_DIGEST_ENABLED or not ai_trainer.is_configured():
        return None
    try:
        return await ai_trainer.weekly_digest(telegram_id)
    except Exception:
        logger.exception("AI weekly digest failed for user %s", telegram_id)
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


def _push_image() -> FSInputFile | str:
    """Cached file_id after the first send, else the file itself."""
    return _push_image_file_id or FSInputFile(PUSH_IMAGE_PATH)


# Telegram's hard limit on a photo caption. Every rotation-pool push text is
# well under this, but the AI weekly digest is a free-form model completion
# and needs the safety net.
CAPTION_LIMIT = 1024


def _as_caption(text: str) -> str:
    if len(text) <= CAPTION_LIMIT:
        return text
    return text[: CAPTION_LIMIT - 1].rstrip() + "…"


async def _deliver(
    bot: Bot, telegram_id: int, decision: PushDecision, local_date: dt.date
) -> None:
    """Send the push and log it against the recipient's own date — that date is
    what `has_push_today` dedupes on.

    Every Telegram failure stays contained here. This runs inside a loop over
    every due user, so an exception escaping it (a deleted chat, a network
    blip, a 429 from sending without pause) aborted the whole tick: everyone
    after the failing recipient got nothing, and by the next tick their send
    hour had passed. /broadcast learned this already — same treatment here.
    """
    global _push_image_file_id
    kb = keyboards.push_cta_keyboard() if decision.with_cta else None
    try:
        message = await _send_push_photo(bot, telegram_id, decision, kb)
    except TelegramForbiddenError:
        logger.info("User %s blocked the bot, skipping push", telegram_id)
        return
    except TelegramAPIError:
        logger.exception("Failed to deliver push to user %s", telegram_id)
        return
    if _push_image_file_id is None:
        _push_image_file_id = message.photo[-1].file_id
    await db.record_push(telegram_id, decision.category, decision.text, local_date.isoformat())


async def _send_push_photo(bot: Bot, telegram_id: int, decision: PushDecision, kb):
    """One send, retried once if Telegram asks us to wait."""
    try:
        return await bot.send_photo(
            chat_id=telegram_id,
            photo=_push_image(),
            caption=_as_caption(decision.text),
            reply_markup=kb,
            disable_notification=False,
        )
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return await bot.send_photo(
            chat_id=telegram_id,
            photo=_push_image(),
            caption=_as_caption(decision.text),
            reply_markup=kb,
            disable_notification=False,
        )


# Pause between sends, so a tick with many due users stays under Telegram's
# ~30 messages/second cap. Same value /broadcast uses.
SEND_DELAY = 0.05


def _utc_now() -> dt.datetime:
    """Naive UTC — split out so tests can pin the clock."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _local_now(tz_offset: int) -> dt.datetime:
    """Wall clock for a user at `tz_offset`, matching timeutil's UTC-based model."""
    return _utc_now() + dt.timedelta(hours=tz_offset or 0)


def is_send_hour(tz_offset: int, hour: int) -> bool:
    return _local_now(tz_offset).hour == hour


async def _send_daily_pushes(bot: Bot) -> None:
    """One hourly tick: send to whoever it's ENGAGEMENT_HOUR for right now.

    Everyone used to be pushed at the server's ENGAGEMENT_HOUR, so a user five
    zones over got their "обычно в это время ты под грифом" nudge in the middle
    of the afternoon — even though they'd set their timezone in Settings and
    everything else in the bot already respected it. build_daily_push's own
    has_push_today guard is keyed on the date it's given, so passing each user's
    local date also keeps the one-per-day promise per user.
    """
    hour = config.ENGAGEMENT_HOUR

    # Decide who is due *before* sending anything. Building a push can be slow —
    # Sunday's digest makes an LLM call per user — so a big list can take the
    # tick past the hour it started in. Re-checking the clock per user as the
    # loop crawled meant everyone near the end fell out of their own send hour
    # and got skipped; by the next tick their hour had passed, so the push was
    # lost for the day rather than merely late.
    due = [
        (telegram_id, _local_now(tz_offset).date())
        for telegram_id, tz_offset in await db.list_engagement_eligible_user_ids()
        if is_send_hour(tz_offset, hour)
    ]
    due_newbies = [
        (telegram_id, created_at, _local_now(tz_offset).date())
        for telegram_id, created_at, tz_offset in await db.list_newbie_user_ids()
        if is_send_hour(tz_offset, hour)
    ]

    for telegram_id, local_date in due:
        try:
            decision = await build_daily_push(telegram_id, local_date)
        except Exception:
            logger.exception("Failed to build push for user %s", telegram_id)
            continue
        if decision is not None:
            await _deliver(bot, telegram_id, decision, local_date)
            await asyncio.sleep(SEND_DELAY)

    for telegram_id, created_at, local_date in due_newbies:
        try:
            decision = await build_newbie_push(telegram_id, created_at, local_date)
        except Exception:
            logger.exception("Failed to build newbie push for user %s", telegram_id)
            continue
        if decision is not None:
            await _deliver(bot, telegram_id, decision, local_date)
            await asyncio.sleep(SEND_DELAY)


def _seconds_until_next_hour() -> float:
    now = dt.datetime.now()
    nxt = (now + dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return (nxt - now).total_seconds()


async def run_daily_engagement_job(bot: Bot) -> None:
    if not config.ENGAGEMENT_ENABLED:
        return
    while True:
        await asyncio.sleep(_seconds_until_next_hour())
        try:
            await _send_daily_pushes(bot)
        except Exception:
            logger.exception("Daily engagement job failed")
