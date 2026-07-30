"""Workout lifecycle: start, add exercises, switch between them, log sets, finish."""

import asyncio
import datetime as dt
import logging
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from html import escape
from typing import Callable

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    ReactionTypeEmoji,
)

import achievements
import ai_trainer
import analytics
import charts
import chat_bottom
import config
import db
import exercise_descriptions
import exercise_media
import formatting
import keyboards
import timeutil
import ui
import view_builder
import voice_parse
from fsm import WorkoutFlow
from parser import ParsedSet, ParseError, parse_ru_date, parse_set_edit, parse_sets_line

router = Router(name="workout")

logger = logging.getLogger(__name__)


# ---------- helpers ----------

async def _attach_ai_comment(
    bot, chat_id: int, message_id: int, user_id: int, workout_id: int, base_text: str
) -> None:
    """Generate the AI-trainer comment in the background and append it to the
    already-sent summary message, so finishing a workout isn't blocked on the LLM call.
    """
    try:
        comment = await ai_trainer.comment_on_workout(user_id, workout_id)
    except Exception:
        logger.exception("AI trainer workout comment failed for workout %s", workout_id)
        with suppress(TelegramBadRequest):
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id,
                reply_markup=keyboards.workout_card_keyboard(workout_id, show_ai_button=True),
            )
        return
    await db.set_workout_ai_comment(workout_id, comment)
    new_text = base_text + "\n" + formatting.build_ai_comment_block(comment)
    with suppress(TelegramBadRequest):
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=new_text, parse_mode="HTML",
            reply_markup=keyboards.workout_card_keyboard(workout_id, show_ai_button=False),
        )


async def _ensure_user(telegram_id: int, username: str | None):
    return await db.get_or_create_user(telegram_id, username)


def _move_open_exercises_last(
    blocks: list[formatting.BlockView], open_exercises: list[int], active_id: int | None
) -> list[formatting.BlockView]:
    """Push still-open exercises to the bottom, active one last, closest to the input hint."""
    open_set = set(open_exercises)
    closed = [
        b for b in blocks
        if not (isinstance(b, formatting.ExerciseBlockView) and b.exercise_id in open_set)
    ]
    open_map = {
        b.exercise_id: b for b in blocks
        if isinstance(b, formatting.ExerciseBlockView) and b.exercise_id in open_set
    }
    order = [eid for eid in open_exercises if eid != active_id]
    if active_id in open_map:
        order.append(active_id)
    return closed + [open_map[eid] for eid in order if eid in open_map]


async def _refresh_live(bot, state: FSMContext, user, workout_id: int, hint, keyboard):
    """Redraw the live tracker so it always sits at the bottom of the chat.

    Telegram doesn't let a bot move an edited message down past newer messages,
    so the tracker can only be edited in place while it's still the last message
    in the chat — the usual case, since a typed set is deleted before we redraw.
    When something stayed below it (a record kept with its 🔥, a voice note and
    its reply, a parse-error hint), editing would strand the buttons above it,
    so we delete and resend instead. chat_bottom is what tells the two apart.
    """
    data = await state.get_data()
    chat_id = data["live_chat_id"]
    message_id = data["live_message_id"]
    blocks = await view_builder.build_block_views(workout_id, user["e1rm_formula"])
    active = data.get("active_exercise_id")
    blocks = _move_open_exercises_last(blocks, data.get("open_exercises") or [], active)
    text = formatting.build_live_session_text(blocks, hint, active_exercise_id=active)
    if data.get("is_backfill") and data.get("bf_date"):
        date = dt.date.fromisoformat(data["bf_date"])
        text = f"📅 {formatting.format_date_ru(date)}\n\n{text}"
    if chat_bottom.is_at_bottom(chat_id, message_id):
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
                reply_markup=keyboard, parse_mode="HTML",
            )
            return
        except TelegramBadRequest as e:
            # Nothing changed (e.g. a double tap) — the screen is already right.
            if "message is not modified" in str(e).lower():
                return
    with suppress(TelegramBadRequest):
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(live_message_id=sent.message_id)


def _sticky_photo_caption(ex) -> str:
    """Exercise name plus its technique steps, if we have them — the same text the
    ℹ️ card shows, minus the equipment/attachment metadata that isn't useful mid-set."""
    caption = f"<b>{escape(ex['display_name'])}</b>"
    description = exercise_descriptions.effective_description(ex)
    if description:
        caption += f"\n\n{escape(description)}"
    return caption


async def _send_sticky_photo(bot, chat_id: int, ex) -> list[int]:
    """Send the active exercise's reference photo(s); returns the sent message ids
    ([] when the exercise has no photo at all)."""
    caption = _sticky_photo_caption(ex)
    if ex["custom_photo_file_id"]:
        sent = await bot.send_photo(
            chat_id=chat_id, photo=ex["custom_photo_file_id"], caption=caption, parse_mode="HTML"
        )
        return [sent.message_id]
    images = exercise_media.get_images(ex["name"])
    if not images:
        return []
    media = [
        InputMediaPhoto(
            media=exercise_media.cached_file_id(path) or FSInputFile(path),
            caption=caption if i == 0 else None,
            parse_mode="HTML" if i == 0 else None,
        )
        for i, path in enumerate(images)
    ]
    sent_group = await bot.send_media_group(chat_id=chat_id, media=media)
    for path, msg in zip(images, sent_group, strict=False):
        photos = getattr(msg, "photo", None)
        if photos:
            exercise_media.remember_file_id(path, photos[-1].file_id)
    return [m.message_id for m in sent_group]


async def _clear_sticky_photo(bot, state: FSMContext) -> None:
    data = await state.get_data()
    for mid in data.get("sticky_photo_msg_ids") or []:
        with suppress(TelegramBadRequest):
            await bot.delete_message(chat_id=data.get("live_chat_id"), message_id=mid)
    await state.update_data(sticky_photo_msg_ids=None, sticky_photo_ex_id=None)


async def _sync_sticky_photo(bot, state: FSMContext, ex_id: int | None) -> None:
    """Keep a photo of the active exercise pinned right above the live tracker.

    It only needs sending when the active exercise changes: the tracker itself is
    deleted and re-sent on every refresh (see _refresh_live), so it always lands
    *below* this photo, and the user's own messages are deleted as they're logged.
    """
    data = await state.get_data()
    chat_id = data.get("live_chat_id")
    if chat_id is None or data.get("sticky_photo_ex_id") == ex_id:
        return
    await _clear_sticky_photo(bot, state)
    if ex_id is None:
        return
    ex = await db.get_exercise(ex_id)
    if ex is None:
        return
    msg_ids = await _send_sticky_photo(bot, chat_id, ex)
    # ex_id is remembered even with no photo, so an exercise without one doesn't
    # re-hit the DB and the media lookup on every single set.
    await state.update_data(sticky_photo_msg_ids=msg_ids, sticky_photo_ex_id=ex_id)


async def _suggested_next_exercise(user_id: int, last_finished_id: int | None, done_ids: tuple[int, ...] = ()):
    """What the user did right after `last_finished_id` last time, for a one-tap suggestion.

    Skips a suggestion the user already logged this workout — recommending it
    again is just noise once it's already on today's list.
    """
    if last_finished_id is None:
        return None
    workout_id = await db.find_last_finished_workout_with_exercise(user_id, last_finished_id)
    if workout_id is None:
        return None
    nxt = await db.get_next_exercise_in_workout(workout_id, last_finished_id)
    if nxt is None or nxt["exercise_id"] == last_finished_id or nxt["exercise_id"] in done_ids:
        return None
    ex = await db.get_exercise(nxt["exercise_id"])
    if ex is None or ex["is_archived"]:
        return None
    return ex["id"], ex["display_name"]


_IDLE_RECENT_EXERCISES = 2


async def _idle_view(
    data: dict, user_id: int, is_empty: bool = False, done_ids: tuple[int, ...] = ()
) -> tuple[str | None, InlineKeyboardMarkup]:
    has_planned = bool(data.get("planned_blocks"))
    suggested = None if has_planned else await _suggested_next_exercise(
        user_id, data.get("last_finished_exercise_id"), done_ids,
    )
    # The suggestion's own button names the exercise now, so the hint would just
    # repeat it — keep it only when the button had to shorten the name.
    hint = None
    if suggested and keyboards.suggest_button_label(suggested[1]) != suggested[1]:
        hint = f"💡 В прошлый раз дальше было: <b>{escape(suggested[1])}</b>"
    recent: list[tuple[int, str]] = []
    if not has_planned:
        # Skip the already-offered "suggested" exercise and anything already
        # logged this workout so the shortcuts never repeat today's own list.
        exclude = done_ids + ((suggested[0],) if suggested else ())
        rows = await db.list_recent_exercises(user_id, limit=_IDLE_RECENT_EXERCISES, exclude_ids=exclude)
        recent = [(r["id"], r["display_name"]) for r in rows]
    kb = keyboards.exercise_picker_entry_keyboard(
        has_planned=has_planned, suggested=suggested, is_empty=is_empty, recent=recent,
    )
    return hint, kb


async def _enter_idle_screen(bot, state: FSMContext, user, workout_id: int):
    data = await state.get_data()
    await _clear_sticky_photo(bot, state)  # no active exercise → nothing to illustrate
    done_ids = tuple(await db.list_exercise_ids_for_workout(workout_id))
    is_empty = not done_ids
    hint, kb = await _idle_view(data, user["telegram_id"], is_empty=is_empty, done_ids=done_ids)
    await _refresh_live(bot, state, user, workout_id, hint, kb)


async def _delete_message(message: Message):
    with suppress(TelegramBadRequest):
        await message.delete()


# How long a record-setting message lingers (with its 🔥 reaction) before being
# tidied away — long enough to notice and screenshot, short enough not to clutter
# the chat like a normal weight message that's deleted immediately.
_RECORD_MESSAGE_LIFETIME_SECONDS = 60


async def _delete_message_later(bot, chat_id: int, message_id: int, delay: float) -> None:
    await asyncio.sleep(delay)
    with suppress(TelegramBadRequest):
        await bot.delete_message(chat_id=chat_id, message_id=message_id)


async def _log_one(block_id: int, exercise_id: int, weight: float, reps: int, rpe: float | None = None):
    await db.append_set(block_id, exercise_id, 0, weight, reps, rpe)


# Below this fraction (or above this multiple) of last session's heaviest set,
# a just-logged weight is flagged as a likely typo rather than a deliberate
# change — a real backoff set rarely goes below the fraction, and nobody
# doubles their working weight between sessions.
_SUSPICIOUS_WEIGHT_LOW_FRACTION = 0.4
_SUSPICIOUS_WEIGHT_HIGH_MULTIPLE = 3.0


def _suspicious_weight_warning(
    last_session: list[tuple[float, int, float | None]] | None,
    today_sets: list[tuple[float, int]] | None,
    unit: str = "kg",
) -> str | None:
    """A soft nudge — never blocks logging, unlike parser.MAX_WEIGHT's hard
    ceiling — when the just-logged set's weight looks like a typo next to what
    this exercise was loaded with last time, in *either* direction: a dropped
    digit ("1 1" meant "140 6") reads the same as an extra one ("1400" meant
    "140") relative to history, even though only the low end used to be
    checked here. Bodyweight exercises (0 kg either time) are exempt: a light
    set there is normal, not a typo.
    """
    if not last_session or not today_sets:
        return None
    last_weight, _last_reps = today_sets[-1]
    if last_weight <= 0:
        return None
    prev_max_weight = max((w for w, _r, _rpe in last_session), default=0)
    if prev_max_weight <= 0:
        return None
    too_low = last_weight < _SUSPICIOUS_WEIGHT_LOW_FRACTION * prev_max_weight
    too_high = last_weight > _SUSPICIOUS_WEIGHT_HIGH_MULTIPLE * prev_max_weight
    if not (too_low or too_high):
        return None
    u = formatting.UNIT_LABELS.get(unit, "кг")
    return (
        f"⚠️ {formatting.format_weight(last_weight)}{u}? "
        f"в прошлый раз {formatting.format_weight(prev_max_weight)}{u}"
    )


def _logging_hint(
    last_session: list[tuple[float, int, float | None]] | None,
    has_sets: bool,
    unit: str = "kg",
    show_progression: bool = True,
    today_sets: list[tuple[float, int]] | None = None,
    note: str | None = None,
    show_instruction: bool = True,
    inferred_step: float | None = None,
    confirmed_weight: float | None = None,
    formula: str = config.DEFAULT_E1RM_FORMULA,
) -> str:
    base = None
    if show_instruction:
        base = "Вес и повторы через пробел, например «100 8»"
        if has_sets:
            base += " (можно только повторы — вес возьмётся с последнего подхода)"
    note_line = f"📝 <i>{escape(note)}</i>\n" if note else ""
    warning = _suspicious_weight_warning(last_session, today_sets, unit)
    if warning and confirmed_weight is not None and today_sets and today_sets[-1][0] == confirmed_weight:
        # Already answered "да, записать" for exactly this weight — repeating the
        # warning under the set would be nagging about a settled question. Sets
        # that arrive by other routes ("N: 100 8" edits) still get the nudge.
        warning = None
    warning_line = f"{warning}\n" if warning else ""
    if last_session:
        sets_str = ", ".join(formatting.format_set(w, r, rpe) for w, r, rpe in last_session)
        line = f"💡 В прошлый раз: {sets_str}"
        if show_progression:
            wr_only = [(w, r) for w, r, _ in last_session]
            suggestion = analytics.suggest_progression(
                wr_only, unit=unit, inferred_step=inferred_step, formula=formula
            )
            if suggestion is not None:
                achieved = any(
                    w >= suggestion.target_weight and r >= suggestion.target_reps
                    for w, r in (today_sets or [])
                )
                line += f"\n{formatting.format_progression_hint(suggestion, unit, achieved)}"
        return f"{warning_line}{note_line}<i>{line}</i>\n\n{base}" if base else f"{warning_line}{note_line}<i>{line}</i>"
    if note_line or warning_line:
        head = f"{warning_line}{note_line}"
        return f"{head}\n{base}" if base else head.rstrip("\n")
    return base or ""


async def _sets_beat_record(
    ex_id: int, workout_id: int, logged: list[tuple[float, int]], formula: str
) -> bool:
    """True if any of the sets just logged is a genuine all-time record for this
    exercise — a new best e1RM or a new heaviest weight (or, for bodyweight moves,
    the most reps in a set).

    Deliberately stricter than the completion-card highlights: reps at a
    never-before-used weight are *not* treated as a record here, or almost every
    set at a fresh weight would trigger the 🔥. Compared against every prior
    finished session, so the current workout's own earlier sets are excluded.
    """
    workout = await db.get_workout(workout_id)
    if workout is None:
        return False
    started = workout["started_at"]
    history_rows = await db.list_sets_for_exercise(ex_id, exclude_workout_id=workout_id)
    history_set_rows = [
        analytics.SetRow(r["weight"], r["reps"], r["workout_id"], r["started_at"])
        for r in history_rows
        if r["started_at"] < started
    ]
    prior_sessions = analytics.group_sets_by_session(history_set_rows)
    for s in prior_sessions:
        s.formula = formula
    if not prior_sessions:
        return False  # first-ever session with this exercise — nothing to beat yet
    prior = analytics.compute_personal_records(prior_sessions)
    is_bodyweight = all(w == 0 for w, _ in logged)
    if is_bodyweight:
        prior_best_reps = max(prior.max_reps_at_weight.values(), default=0)
        return any(r > prior_best_reps for w, r in logged)
    for weight, reps in logged:
        if weight > prior.max_weight:
            return True
        if analytics.e1rm(weight, reps, formula) > prior.max_e1rm:
            return True
    return False


async def _evaluate_achievements(
    user_id: int, workout_id: int, started_at: dt.datetime, duration_seconds: float | None
) -> list[str]:
    """Award any achievements the user just unlocked and return the new codes.

    Called after the workout is marked finished, so lifetime aggregates already
    include it. Never raises into the finish flow — a badge is a bonus, not a
    reason to break saving the workout.
    """
    try:
        ctx = achievements.AchievementContext(
            total_workouts=await db.count_workouts(user_id),
            lifetime_tonnage_kg=(await db.hall_of_fame_aggregates(user_id))["tonnage"],
            best_week_streak=analytics.max_week_streak(
                [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id)]
            ),
            max_weight_kg=await db.max_weight_ever(user_id),
            distinct_exercises=await db.count_distinct_exercises_used(user_id),
            workout_start_hour=started_at.hour,
            workout_date=started_at.date(),
            workout_duration_seconds=duration_seconds,
        )
        return await db.award_achievements(user_id, achievements.earned_codes(ctx))
    except Exception:
        logger.exception("Achievement evaluation failed for workout %s", workout_id)
        return []


async def _exercise_history(
    ex_id: int,
) -> tuple[list[tuple[float, int, float | None]], float | None]:
    """Two things off one history query, both cached in the FSM so the logging
    screen doesn't rescan an exercise's whole history on every render:

    - working sets (weight, reps, rpe) from its most recent finished workout;
    - the increment this exercise is actually loaded in (analytics.infer_weight_step).
    """
    rows = await db.list_sets_for_exercise(ex_id)
    if not rows:
        return [], None
    last_workout_id = rows[-1]["workout_id"]
    last_session = [
        (r["weight"], r["reps"], r["rpe"]) for r in rows if r["workout_id"] == last_workout_id
    ]
    return last_session, analytics.infer_weight_step(r["weight"] for r in rows)


async def _render_logging_screen(bot, state: FSMContext, user):
    data = await state.get_data()
    open_ids: list[int] = data.get("open_exercises") or []
    active = data.get("active_exercise_id")
    last_session_sets = data.get("last_session_sets") or {}
    weight_steps = data.get("weight_steps") or {}

    names: dict[int, str] = {}
    active_note: str | None = None
    for ex_id in open_ids:
        ex = await db.get_exercise(ex_id)
        names[ex_id] = ex["display_name"]
        if ex_id == active:
            active_note = ex["notes"]

    open_items = [(ex_id, names[ex_id]) for ex_id in open_ids]
    active_block_id = (data.get("open_blocks") or {}).get(active)
    active_block_sets = await db.list_sets_for_block(active_block_id) if active_block_id else []
    has_sets = bool(active_block_sets)
    today_sets = [(r["weight"], r["reps"]) for r in active_block_sets]
    recent_dates = [
        dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user["telegram_id"])
    ]
    show_instruction = not analytics.is_seasoned(recent_dates, timeutil.user_today(user))
    hint = _logging_hint(
        last_session_sets.get(active),
        has_sets,
        user["unit"],
        bool(user["progression_hint_enabled"]),
        today_sets,
        note=active_note,
        show_instruction=show_instruction,
        inferred_step=weight_steps.get(active),
        confirmed_weight=(data.get("confirmed_weights") or {}).get(active),
        formula=user["e1rm_formula"],
    )
    kb = keyboards.logging_keyboard(open_items, active, has_sets)
    await _sync_sticky_photo(bot, state, active)
    await _refresh_live(bot, state, user, data["workout_id"], hint, kb)


async def _back_after_cancel(bot, state: FSMContext, user):
    data = await state.get_data()
    if data.get("open_exercises"):
        await state.set_state(WorkoutFlow.logging_set)
        await _render_logging_screen(bot, state, user)
    else:
        await state.set_state(WorkoutFlow.idle)
        await _enter_idle_screen(bot, state, user, data["workout_id"])


# ---------- main menu ----------

_GREETING = "<b>ПРИВЕТ, АТЛЕТ. НАЧНЁМ ТРЕНИРОВКУ?</b>"

# Shown on the main menu until the first workout is logged — a quick "here's how
# it works" so a brand-new user isn't dropped onto the same screen as a veteran.
_ONBOARDING = (
    "<b>ПРИВЕТ, АТЛЕТ! 💪</b>\n\n"
    "Я — твой дневник силовых тренировок. Работает просто:\n"
    "1️⃣ Жми «🏋️ НАЧАТЬ ТРЕНИРОВКУ»\n"
    "2️⃣ Выбирай группу мышц и упражнение\n"
    "3️⃣ Пиши вес и повторы, например «100 8» (или «8» для своего веса)\n\n"
    "Дальше я сам посчитаю рекорды и прогресс. Погнали? 👇"
)


# The heatmap picture only depends on `today` plus the set of finished-workout
# dates, and changes at most once a day (a new workout, or the daily rollover
# of "days since last"/"last 30 days"/the grid's today-column). Keyed by
# (today, workout count, last workout date) so a render is skipped on every
# menu view except the first one after something actually changed — matplotlib
# is the expensive part of _menu_view, not the DB lookups above it.
_heatmap_cache: dict[int, tuple[tuple, bytes]] = {}


async def _menu_view(user_id: int) -> tuple[str, bytes | None]:
    """Greeting, plus a year heatmap image (with the streak/this-week/30-day
    dashboard stats drawn into it) once the user has any finished workouts.
    """
    today = timeutil.user_today(await db.get_user(user_id))
    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id)]
    if not dates:
        return _ONBOARDING, None
    cache_key = (today, len(dates), max(dates))
    cached = _heatmap_cache.get(user_id)
    if cached is not None and cached[0] == cache_key:
        return _GREETING, cached[1]
    dashboard = analytics.compute_dashboard(dates, today)
    this_monday = today - dt.timedelta(days=today.weekday())
    year_ago = this_monday - dt.timedelta(weeks=52)
    first_monday = min(dates) - dt.timedelta(days=min(dates).weekday())
    heatmap_start = max(first_monday, year_ago)
    stat_lines = formatting.dashboard_stat_lines(dashboard)
    png = await asyncio.to_thread(charts.render_year_heatmap, Counter(dates), today, heatmap_start, stat_lines)
    _heatmap_cache[user_id] = (cache_key, png)
    return _GREETING, png


async def _send_menu(message: Message, text: str, png: bytes | None, keyboard) -> Message:
    if png is None:
        return await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    return await message.answer_photo(
        BufferedInputFile(png, filename="year.png"),
        caption=text, reply_markup=keyboard, parse_mode="HTML",
    )


async def _main_menu_kb(user_id: int, active) -> InlineKeyboardMarkup:
    return keyboards.main_menu(bool(active))


_WORKOUT_SCAFFOLD_KEYS = (
    "workout_id", "open_exercises", "open_blocks", "active_exercise_id",
    "last_by_exercise", "last_session_sets", "weight_steps",
    # Not workout scaffolding, but the same reasoning: stepping out to the menu
    # and back shouldn't make the AI-тренер forget the conversation in progress.
    "ai_history",
)


async def _clear_state_keep_workout(state: FSMContext) -> None:
    """Reset the FSM flow, but keep the in-progress workout's open-exercise
    scaffolding intact. Leaving to the menu (or elsewhere) doesn't lose any
    data — it's still sitting untouched in memory — so wiping it on every
    menu tap would force a lossy DB-only reconstruction (which can only ever
    recover the single most-recently-touched exercise, see _reopen_exercises)
    for no reason. Preserved keys let _enter_live restore the exact tabs/
    weights the user had open before they navigated away."""
    data = await state.get_data()
    preserved = {k: data[k] for k in _WORKOUT_SCAFFOLD_KEYS if k in data}
    await state.clear()
    if preserved:
        await state.update_data(**preserved)


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await _clear_state_keep_workout(state)
    await _ensure_user(message.from_user.id, message.from_user.username)
    active = await db.get_active_workout(message.from_user.id)
    text, png = await _menu_view(message.from_user.id)
    await _send_menu(message, text, png, await _main_menu_kb(message.from_user.id, active))
    if active:
        started = dt.datetime.fromisoformat(active["started_at"])
        if (dt.datetime.now() - started).total_seconds() > config.STALE_WORKOUT_HOURS * 3600:
            warning = (
                f"⚠️ У тебя висит тренировка с {formatting.format_date_ru(started)} — "
                f"забыл закрыть?"
            )
            await message.answer(warning, reply_markup=keyboards.stale_workout_keyboard(active["id"]))


_HELP_TEXT = (
    "🆘 <b>СПРАВКА ПО ВВОДУ</b>\n\n"
    "<b>Подход — вес и повторы через пробел или «x»:</b>\n"
    "• <code>100 8</code> или <code>100x8</code> — 100кг × 8 повторов\n"
    "• <code>100x8x3</code> или <code>100 8 3</code> — то же самое, но сразу 3 подхода\n"
    "• <code>8</code> — только повторы, вес возьмётся с последнего подхода "
    "(для своего веса — подтягивания, отжимания и т.п.)\n"
    "• <code>+20 8</code> — то же, что <code>20 8</code>; «+» просто для себя, "
    "если считаешь довеском к своему весу\n"
    "• <code>100x8@9</code> или <code>100 8 @8.5</code> — с RPE (сложность подхода, 1–10)\n\n"
    "<b>Несколько подходов одним сообщением</b> — через запятую, точку с запятой "
    "или с новой строки:\n<code>100 8, 100 7, 95 8</code>\n\n"
    "<b>Быстрые команды</b> (пока открыто упражнение):\n"
    "• <code>-</code> — удалить последний подход\n"
    "• <code>=</code> — повторить последний подход\n"
    "• <code>!текст</code> — заметка к упражнению, например «!болит плечо»\n"
    "• <code>2: 100 8</code> — исправить 2-й уже залогированный подход\n"
    "• <code>?</code> или /help — эта справка\n\n"
    "🎙 Можно и голосом: запиши войс «сто на восемь»."
)


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    await message.answer(_HELP_TEXT, parse_mode="HTML")


@router.callback_query(F.data.startswith("stale:finish:"))
async def stale_finish_workout(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[2])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id or workout["status"] != "active":
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    exercise_ids = await db.list_exercise_ids_for_workout(workout_id)
    if not exercise_ids:
        await db.discard_workout(workout_id)
        await ui.safe_edit(callback, "Тренировка была пустая — удалил её.")
        await callback.answer()
        return
    await db.finish_workout(workout_id, finished_at=workout["started_at"])
    await ui.safe_edit(callback, "✅ Тренировка завершена задним числом.")
    await callback.answer()


@router.callback_query(F.data.startswith("stale:delete:"))
async def stale_delete_confirm(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[2])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id or workout["status"] != "active":
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    kb = keyboards.yes_no_keyboard(
        yes_cb=f"stale:delyes:{workout_id}",
        no_cb="stale:delno",
        yes_text="🗑 Удалить",
        no_text="❌ Отмена",
    )
    await ui.safe_edit(callback, "Удалить эту тренировку? Это действие нельзя отменить.", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("stale:delyes:"))
async def stale_delete(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[2])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    await db.discard_workout(workout_id)
    await ui.safe_edit(callback, "Тренировка удалена.")
    await callback.answer()


@router.callback_query(F.data == "stale:delno")
async def stale_delete_cancel(callback: CallbackQuery, state: FSMContext):
    await ui.safe_edit(callback, "Хорошо, оставил как есть.")
    await callback.answer()


async def _show_main_menu(callback: CallbackQuery, state: FSMContext, delete_current: bool = True):
    # delete_current=False when reached from the AI-trainer chat's "⬅️ Меню"
    # button — that message is part of the user's conversation with the
    # AI-тренер, not a disposable menu screen, so it should stay in the chat
    # instead of being deleted (same reasoning as _enter_live's delete_message).
    await _clear_state_keep_workout(state)
    active = await db.get_active_workout(callback.from_user.id)
    text, png = await _menu_view(callback.from_user.id)
    kb = await _main_menu_kb(callback.from_user.id, active)
    if png is None:
        await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML", delete=delete_current)
    else:
        await ui.safe_edit_photo(
            callback, png, "year.png", text, reply_markup=kb, parse_mode="HTML", delete=delete_current
        )


@router.callback_query(F.data == "live:back_to_menu")
async def live_back_to_menu(callback: CallbackQuery, state: FSMContext):
    """"⬅️ В меню" on the just-finished workout card — see _finalize_workout,
    which stopped auto-sending the menu so the card isn't buried under it."""
    await _show_main_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == "menu:progress")
async def menu_progress(callback: CallbackQuery, state: FSMContext):
    from handlers.history import show_progress_entry
    await show_progress_entry(callback, state)


@router.callback_query(F.data == "menu:history")
async def menu_history(callback: CallbackQuery, state: FSMContext):
    from handlers.history import show_history_list
    await show_history_list(callback, state, page=0)


@router.callback_query(F.data == "menu:exercises")
async def menu_exercises(callback: CallbackQuery, state: FSMContext):
    from handlers.exercises import show_exercise_groups
    await show_exercise_groups(callback, state)


@router.callback_query(F.data == "menu:settings")
async def menu_settings(callback: CallbackQuery, state: FSMContext):
    from handlers.settings import show_settings
    await show_settings(callback, state)


# ---------- start / resume workout ----------

@router.callback_query(F.data == "menu:start_workout")
async def start_workout(callback: CallbackQuery, state: FSMContext):
    # Answered up front: neither branch below has anything to tell Telegram about
    # the tap itself, and _enter_live doesn't answer internally — left this way,
    # the active-workout branch used to leave the button spinning until Telegram
    # gave up on its own, ~10s later.
    await callback.answer()
    await _ensure_user(callback.from_user.id, callback.from_user.username)
    active = await db.get_active_workout(callback.from_user.id)
    if active:
        await _enter_live(callback, state, active["id"])
        return
    workout_id = await db.create_workout(callback.from_user.id)
    await _delete_message(callback.message)
    sent = await callback.message.answer("🏋️ Тренировка начата")
    await state.update_data(
        workout_id=workout_id, live_chat_id=sent.chat.id, live_message_id=sent.message_id,
        last_by_exercise={},
    )
    await state.set_state(WorkoutFlow.picking_group)
    await _picker_screen_groups(callback, state, show_program_button=True)


REPEAT_PAGE_SIZE = 6


async def _repeat_summary(workout) -> tuple[str, list[tuple[str, str]]]:
    """Date (short — safe as a button label) and this workout's exercises as
    (name, muscle group) pairs, in block order, for one past workout in the
    repeat list."""
    started = dt.datetime.fromisoformat(workout["started_at"])
    date = formatting.format_date_ru(started)
    plan = await db.workout_plan(workout["id"])
    exercises: list[tuple[str, str]] = []
    for block in plan:
        ex = await db.get_exercise(block["exercise_ids"][0])
        if ex is None:
            continue
        group = await db.get_muscle_group(ex["primary_group_id"]) if ex["primary_group_id"] else None
        exercises.append((ex["display_name"], group["name"] if group else ""))
    return date, exercises


def _repeat_workout_block(i: int, date: str, exercises: list[tuple[str, str]]) -> str:
    """Bold number+date title, then every exercise as its own bulleted line,
    muscle group in brackets — same "Name [GROUP]" convention the live tracker
    uses (see formatting._render_single_block).

    A comma-joined summary still read as a wall of text even truncated — one
    bullet per exercise is longer overall but each line is short and the eye
    can actually scan it, so nothing gets cut this time.
    """
    header = f"<b>{i} · {date}</b>"
    if not exercises:
        return f"{header}\nнет упражнений"
    bullets = "\n".join(
        f"• {escape(name)} [{escape(group.upper())}]" if group else f"• {escape(name)}"
        for name, group in exercises
    )
    return f"{header}\n{bullets}"


async def _repeat_list_screen(callback: CallbackQuery, state: FSMContext, page: int):
    """List of the user's recent finished workouts to pick one to repeat, rendered
    into the live tracker message like the rest of the picker.

    Exercise names don't fit into a button label, so buttons only carry a number
    + date; the message text above them spells out each workout's full exercise
    list under its own bold number+date title.
    """
    user = await db.get_user(callback.from_user.id)
    data = await state.get_data()
    total = await db.count_workouts(callback.from_user.id)
    workouts = await db.list_workouts(
        callback.from_user.id, limit=REPEAT_PAGE_SIZE, offset=page * REPEAT_PAGE_SIZE
    )
    items = []
    blocks = []
    for i, w in enumerate(workouts, start=1 + page * REPEAT_PAGE_SIZE):
        date, exercises = await _repeat_summary(w)
        items.append({"id": w["id"], "label": f"{i} - {date}"})
        blocks.append(_repeat_workout_block(i, date, exercises))
    has_next = (page + 1) * REPEAT_PAGE_SIZE < total
    kb = keyboards.repeat_list_keyboard(items, page, has_next)
    hint = "🔁 Выбери тренировку, чтобы повторить её план:\n\n" + "\n\n".join(blocks)
    await state.update_data(repeat_page=page)
    await _refresh_live(callback.bot, state, user, data["workout_id"], hint, kb)


@router.callback_query(StateFilter(WorkoutFlow.picking_group), F.data == "pick:repeat")
async def pick_repeat_last(callback: CallbackQuery, state: FSMContext):
    """Open the list of past workouts to repeat one of them. Reached from the first
    picker screen of a fresh (already-started) workout."""
    if await db.count_workouts(callback.from_user.id) == 0:
        await callback.answer("Нет прошлой тренировки для повтора", show_alert=True)
        return
    await _repeat_list_screen(callback, state, page=0)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_group), F.data.startswith("pick:rep:page:"))
async def pick_repeat_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[3])
    await _repeat_list_screen(callback, state, page)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_group), F.data == "pick:rep:list")
async def pick_repeat_back_to_list(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await _repeat_list_screen(callback, state, data.get("repeat_page", 0))
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_group), F.data == "pick:rep:cancel")
async def pick_repeat_cancel(callback: CallbackQuery, state: FSMContext):
    """Back from the repeat list to the fresh-workout picker's first screen."""
    await _picker_screen_groups(callback, state, show_program_button=True)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_group), F.data.startswith("pick:rep:show:"))
async def pick_repeat_show(callback: CallbackQuery, state: FSMContext):
    """Preview a past workout — what was done, with a repeat/back choice."""
    workout_id = int(callback.data.split(":")[3])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    user = await db.get_user(callback.from_user.id)
    data = await state.get_data()
    blocks = await view_builder.build_block_views(workout_id, user["e1rm_formula"])
    started = dt.datetime.fromisoformat(workout["started_at"])
    duration_seconds = await view_builder.workout_duration_seconds(workout)
    summary = formatting.build_workout_preview(
        started, blocks, workout["note"], duration_seconds=duration_seconds,
    )
    hint = "🔁 <b>Повторить эту тренировку?</b>\n\n" + summary
    kb = keyboards.repeat_preview_keyboard(workout_id)
    await _refresh_live(callback.bot, state, user, data["workout_id"], hint, kb)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_group), F.data.startswith("pick:rep:use:"))
async def pick_repeat_use(callback: CallbackQuery, state: FSMContext):
    """Pre-load the current (already-started) workout with the exercises (and
    supersets) of the chosen past one — the same planned-blocks machinery a saved
    program uses."""
    workout_id = int(callback.data.split(":")[3])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    plan = await db.workout_plan(workout_id)
    if not plan:
        await callback.answer("В этой тренировке нет упражнений", show_alert=True)
        return
    await state.update_data(planned_blocks=plan)
    await _load_next_planned_block(callback, state)
    await callback.answer()


@router.callback_query(F.data == "menu:resume_workout")
async def resume_workout(callback: CallbackQuery, state: FSMContext):
    active = await db.get_active_workout(callback.from_user.id)
    if not active:
        await callback.answer("Нет активной тренировки")
        await _show_main_menu(callback, state)
        return
    await callback.answer()  # _enter_live doesn't answer internally
    await _enter_live(callback, state, active["id"])


async def _reopen_exercises(
    workout_id: int,
) -> tuple[list[int], dict[int, int], dict[int, list], dict[int, tuple], dict[int, float]]:
    """Rebuild which exercise is still "open" for a workout from the DB.

    The FSM is the only place that tracks "finished" vs "open" exercises, so when we
    re-enter a workout (resume, or bot restart) after losing that in-memory state, we
    can't tell which earlier exercises the user already finished. Reopening all of
    them would wrongly resurrect the superset switch-tabs/controls for exercises
    that are actually done, so we only reopen the most recently logged block and
    treat everything before it as finished.
    """
    open_exercises: list[int] = []
    open_blocks: dict[int, int] = {}
    blocks = await db.list_blocks_for_workout(workout_id)
    if blocks:
        last_block = blocks[-1]
        for be in await db.get_block_exercises(last_block["id"]):
            ex_id = be["exercise_id"]
            if ex_id not in open_exercises:
                open_exercises.append(ex_id)
            open_blocks[ex_id] = last_block["id"]
    last_session_sets: dict[int, list] = {}
    weight_steps: dict[int, float] = {}
    for ex_id in open_exercises:
        last_session_sets[ex_id], step = await _exercise_history(ex_id)
        if step is not None:
            weight_steps[ex_id] = step
    last_by_exercise: dict[int, tuple] = {}
    for ex_id in open_exercises:
        current_sets = await db.list_sets_for_workout_exercise(workout_id, ex_id)
        if current_sets:
            last = current_sets[-1]
            last_by_exercise[ex_id] = (last["weight"], last["reps"])
        else:
            history = await db.list_sets_for_exercise(ex_id)
            if history:
                last = history[-1]
                last_by_exercise[ex_id] = (last["weight"], last["reps"])
    return open_exercises, open_blocks, last_session_sets, last_by_exercise, weight_steps


async def _enter_live(
    callback: CallbackQuery, state: FSMContext, workout_id: int, delete_message: bool = True
):
    # delete_message=False when entering from the AI-trainer chat (its "К тренировке"
    # button) — that message is part of the user's chat history with the AI-тренер,
    # not a disposable menu screen, so it should stay instead of being deleted.
    user = await _ensure_user(callback.from_user.id, callback.from_user.username)
    data = await state.get_data()
    if data.get("workout_id") == workout_id and data.get("open_exercises"):
        # The FSM already knows exactly which exercises/tabs were open (e.g. the
        # user just detoured through the menu/history and nothing was actually
        # lost) — trust it instead of _reopen_exercises's lossy DB-only guess,
        # which can only ever recover the single most-recently-touched exercise.
        open_exercises = data["open_exercises"]
        open_blocks = data.get("open_blocks") or {}
        last_session_sets = data.get("last_session_sets") or {}
        last_by_exercise = data.get("last_by_exercise") or {}
        weight_steps = data.get("weight_steps") or {}
        active_exercise_id = data.get("active_exercise_id")
        if active_exercise_id not in open_exercises:
            active_exercise_id = open_exercises[-1]
    else:
        (
            open_exercises, open_blocks, last_session_sets, last_by_exercise, weight_steps,
        ) = await _reopen_exercises(workout_id)
        active_exercise_id = open_exercises[-1] if open_exercises else None
    if delete_message:
        await _delete_message(callback.message)
    sent = await callback.message.answer("🏋️ Тренировка")
    await state.set_state(WorkoutFlow.logging_set if open_exercises else WorkoutFlow.idle)
    await state.update_data(
        workout_id=workout_id, live_chat_id=sent.chat.id, live_message_id=sent.message_id,
        last_by_exercise=last_by_exercise, open_exercises=open_exercises, open_blocks=open_blocks,
        active_exercise_id=active_exercise_id, last_session_sets=last_session_sets,
        weight_steps=weight_steps,
    )
    if open_exercises:
        await _render_logging_screen(callback.bot, state, user)
    else:
        await _enter_idle_screen(callback.bot, state, user, workout_id)


# ---------- picker: add an exercise (either to start, or alongside what's already open) ----------

async def _picker_screen_groups(callback: CallbackQuery, state: FSMContext, show_program_button: bool = False):
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    # Mid-workout, adding an exercise is time pressure — the groups the user
    # actually trains most should be first, not alphabetical/catalog order.
    groups = await db.list_muscle_groups(callback.from_user.id, order_by_usage=True)
    hint = "Выбери группу мышц или найди упражнение по названию:"
    open_ids = data.get("open_exercises") or []
    if open_ids:
        names = [escape((await db.get_exercise(eid))["display_name"]) for eid in open_ids]
        hint = "Открыто сейчас: " + ", ".join(names) + "\n" + hint
    extra = []
    if show_program_button:
        # Offered only on the very first picker screen of a fresh workout: pick
        # any past session to re-run for people who train A/B without a saved
        # program, plus the shortcut into saved programs.
        if await db.count_workouts(callback.from_user.id) > 0:
            extra.append(("🔁 Повторить тренировку", "pick:repeat"))
        extra.append(("🗂 Выбрать программу", "rt:manage"))
    # Not a "cancel the workout" — pick:cancel just returns to whatever screen was
    # open before (see _back_after_cancel), so it reads as "⬅️ Назад", not "❌ Отмена".
    extra.append(("⬅️ Назад", "pick:cancel"))
    kb = keyboards.groups_keyboard(groups, prefix="pick", extra_buttons=extra, show_all=True)
    await state.update_data(picker_stage="groups")
    await _refresh_live(callback.bot, state, user, data["workout_id"], hint, kb)


@router.callback_query(StateFilter(WorkoutFlow.idle, WorkoutFlow.logging_set), F.data == "live:add_exercise")
async def live_add_exercise(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutFlow.picking_group)
    await _picker_screen_groups(callback, state)
    await callback.answer()


@router.callback_query(
    StateFilter(WorkoutFlow.picking_group, WorkoutFlow.picking_exercise, WorkoutFlow.creating_exercise_name),
    F.data == "pick:cancel",
)
async def pick_cancel(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await _back_after_cancel(callback.bot, state, user)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_group), F.data.startswith("pick:grp:"))
async def pick_group(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.split(":")[2]
    group_id = None if raw == "all" else int(raw)
    await state.update_data(pending_group_id=group_id, pick_page=0)
    await state.set_state(WorkoutFlow.picking_exercise)
    await _picker_screen_exercises(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_exercise), F.data.startswith("pick:page:"))
async def pick_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[2])
    await state.update_data(pick_page=page)
    await _picker_screen_exercises(callback, state)
    await callback.answer()


async def _picker_screen_exercises(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    group_id = data["pending_group_id"]
    page = data.get("pick_page", 0)
    offset = page * config.RECENT_EXERCISES_LIMIT
    if group_id is None:
        exercises = await db.list_user_exercises(
            callback.from_user.id, limit=config.RECENT_EXERCISES_LIMIT, offset=offset
        )
        total = await db.count_user_exercises(callback.from_user.id)
    else:
        exercises = await db.list_user_exercises_in_group(
            callback.from_user.id, group_id, limit=config.RECENT_EXERCISES_LIMIT, offset=offset
        )
        total = await db.count_user_exercises_in_group(callback.from_user.id, group_id)
    has_next = offset + len(exercises) < total
    kb = keyboards.exercises_keyboard(
        exercises, prefix="pick", back_cb="back", show_new_button=group_id is not None,
        page=page, has_next=has_next,
    )
    if exercises:
        hint = "Выбери упражнение или напиши название для поиска:"
    else:
        hint = "У тебя пока нет своих упражнений здесь — добавь новое или напиши название для поиска:"
    await state.update_data(picker_stage="exercises")
    await _refresh_live(callback.bot, state, user, data["workout_id"], hint, kb)


@router.callback_query(StateFilter(WorkoutFlow.picking_exercise), F.data == "pick:back")
async def pick_back_to_groups(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutFlow.picking_group)
    await _picker_screen_groups(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_exercise), F.data.startswith("pick:ex:"))
async def pick_existing_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    await _on_exercise_chosen(callback, state, ex_id)


@router.message(StateFilter(WorkoutFlow.picking_group, WorkoutFlow.picking_exercise))
async def pick_exercise_search(message: Message, state: FSMContext):
    """Typing while picking a group or an exercise searches instead of being silently
    dropped — so the user can jump straight to an exercise by name without first
    drilling into its muscle group."""
    query = message.text.strip()
    await _delete_message(message)
    if not query:
        return
    data = await state.get_data()
    user = await db.get_user(message.from_user.id)
    group_id = data.get("pending_group_id")
    # Searching from the group screen jumps into exercise-picking so a tap on a
    # result (pick:ex:*) and the "back" button both resolve correctly.
    await state.set_state(WorkoutFlow.picking_exercise)
    results = await db.search_exercises(message.from_user.id, query)
    templates = await db.search_exercise_templates(message.from_user.id, query)
    kb = keyboards.exercises_keyboard(
        results, prefix="pick", back_cb="back", show_new_button=group_id is not None, templates=templates,
    )
    if results or templates:
        hint = f"Результаты поиска «{escape(query)}»:"
    else:
        hint = f"Ничего не нашлось по «{escape(query)}»."
        if group_id is not None:
            hint += " Можно создать новое:"
    await state.update_data(picker_stage="exercises")
    await _refresh_live(message.bot, state, user, data["workout_id"], hint, kb)


async def _new_exercise_entry_screen(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    await _refresh_live(
        callback.bot, state, user, data["workout_id"],
        "Напиши название нового упражнения или выбери из шаблонов:",
        keyboards.new_exercise_entry_keyboard("pick"),
    )


@router.callback_query(StateFilter(WorkoutFlow.picking_exercise), F.data == "pick:new")
async def pick_new_exercise(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutFlow.creating_exercise_name)
    await _new_exercise_entry_screen(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.creating_exercise_name), F.data == "pick:templates")
async def pick_templates(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    templates = await db.list_templates_in_group(data["pending_group_id"])
    kb = keyboards.templates_keyboard(templates, prefix="pick", back_cb="newback")
    hint = "Шаблоны — выбери подходящий:"
    await _refresh_live(callback.bot, state, user, data["workout_id"], hint, kb)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.creating_exercise_name), F.data == "pick:newback")
async def pick_back_from_templates(callback: CallbackQuery, state: FSMContext):
    await _new_exercise_entry_screen(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.creating_exercise_name), F.data.startswith("pick:tpl:"))
async def pick_template_preview(callback: CallbackQuery, state: FSMContext):
    """Preview a template (photo + info, same as the ⚙️ Упражнения flow) before
    adding it — the user may just want a look before deciding to add it."""
    from handlers.exercises import _exercise_info_text

    template_id = int(callback.data.split(":")[2])
    template = await db.get_exercise(template_id)
    if template is None:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    text = _exercise_info_text(template, with_created=False)
    kb = keyboards.template_preview_keyboard(template_id, prefix="pick")
    images = exercise_media.get_images(template["name"])
    if images:
        await callback.message.answer_photo(
            FSInputFile(images[0]), caption=text, reply_markup=kb, parse_mode="HTML"
        )
    else:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(
    StateFilter(WorkoutFlow.creating_exercise_name, WorkoutFlow.picking_group, WorkoutFlow.picking_exercise),
    F.data.startswith("pick:tpladd:"),
)
async def pick_template_add(callback: CallbackQuery, state: FSMContext):
    """Reached both from the "📋 Выбрать из шаблонов" preview (a disposable
    message of its own) and, since search results can include templates too
    (see keyboards.exercises_keyboard's `templates` param), directly from a
    search-results screen that *is* the live tracker. Either way the delete
    below is safe: _refresh_live's stale-message fallback (via chat_bottom)
    already handles the tracker's own message having just been deleted.
    """
    template_id = int(callback.data.split(":")[2])
    ex_id = await db.fork_exercise_from_template(callback.from_user.id, template_id)
    with suppress(TelegramBadRequest):
        await callback.message.delete()
    # No photo send here: the exercise becomes active right away, so the sticky
    # photo above the tracker shows the same shots (and the same technique text).
    await _on_exercise_chosen(callback, state, ex_id)


def _suspicious_exercise_name_reason(name: str) -> str | None:
    """None if `name` looks like a plausible exercise name; otherwise a short
    Russian phrase for the "are you sure?" prompt explaining why it doesn't —
    either a stray message (too long) or something with no letters at all
    ("50 12", a logged set typed while the bot was waiting for a name instead)."""
    if len(name) > config.MAX_EXERCISE_NAME_LENGTH:
        return f"длинновато для упражнения ({len(name)} символов)"
    if not any(ch.isalpha() for ch in name):
        return "в названии нет ни одной буквы — не похоже на упражнение"
    return None


@router.message(StateFilter(WorkoutFlow.creating_exercise_name))
async def new_exercise_name_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.reply("Название не может быть пустым")
        return
    await _delete_message(message)
    reason = _suspicious_exercise_name_reason(name)
    if reason:
        # A stray message typed while the bot happened to be waiting for a name
        # doesn't look like an exercise — but it might genuinely be intended,
        # so ask instead of silently blocking it.
        await state.update_data(pending_long_exercise_name=name)
        data = await state.get_data()
        user = await db.get_user(message.from_user.id)
        kb = keyboards.yes_no_keyboard(
            yes_cb="pick:longname:yes", no_cb="pick:longname:no",
            yes_text="✅ Да, создать", no_text="✏️ Написать заново",
        )
        hint = f"«{escape(name)}» — {reason}. Всё верно, создать такое?"
        await _refresh_live(message.bot, state, user, data["workout_id"], hint, kb)
        return
    data = await state.get_data()
    ex_id = await db.create_exercise(message.from_user.id, name, data["pending_group_id"])
    await _on_exercise_chosen(message, state, ex_id)


@router.callback_query(StateFilter(WorkoutFlow.creating_exercise_name), F.data == "pick:longname:yes")
async def pick_longname_confirmed(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    name = data.get("pending_long_exercise_name")
    if not name:
        await callback.answer("Название потерялось, напиши заново", show_alert=True)
        return
    await state.update_data(pending_long_exercise_name=None)
    ex_id = await db.create_exercise(callback.from_user.id, name, data["pending_group_id"])
    await _on_exercise_chosen(callback, state, ex_id)


@router.callback_query(StateFilter(WorkoutFlow.creating_exercise_name), F.data == "pick:longname:no")
async def pick_longname_declined(callback: CallbackQuery, state: FSMContext):
    await state.update_data(pending_long_exercise_name=None)
    await _new_exercise_entry_screen(callback, state)
    await callback.answer()


async def _seed_last_value(data: dict, ex_id: int) -> dict:
    history = await db.list_sets_for_exercise(ex_id)
    last_by = dict(data.get("last_by_exercise") or {})
    if history:
        last = history[-1]
        last_by[ex_id] = (last["weight"], last["reps"])
    return last_by


async def _on_exercise_chosen(event, state: FSMContext, ex_id: int):
    data = await state.get_data()
    await db.touch_exercise_last_used(ex_id)

    open_exercises = list(data.get("open_exercises") or [])
    open_blocks = dict(data.get("open_blocks") or {})

    if ex_id not in open_exercises:
        block_id = await db.create_block(data["workout_id"], "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        open_exercises.append(ex_id)
        open_blocks[ex_id] = block_id
    last_by = await _seed_last_value(data, ex_id)
    last_session_sets = dict(data.get("last_session_sets") or {})
    weight_steps = dict(data.get("weight_steps") or {})
    last_session_sets[ex_id], step = await _exercise_history(ex_id)
    if step is not None:
        weight_steps[ex_id] = step

    await state.update_data(
        open_exercises=open_exercises, open_blocks=open_blocks,
        active_exercise_id=ex_id, last_by_exercise=last_by, last_session_sets=last_session_sets,
        weight_steps=weight_steps,
    )
    await state.set_state(WorkoutFlow.logging_set)
    user = await db.get_user(event.from_user.id)
    await _render_logging_screen(event.bot, state, user)
    if isinstance(event, CallbackQuery):
        await event.answer()


# ---------- logging sets: type "weight reps", switch between open exercises freely ----------

@router.callback_query(StateFilter(WorkoutFlow.logging_set), F.data.startswith("live:switch:"))
async def live_switch_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    data = await state.get_data()
    if ex_id not in (data.get("open_exercises") or []):
        await callback.answer()
        return
    await state.update_data(active_exercise_id=ex_id)
    await callback.answer()
    user = await db.get_user(callback.from_user.id)
    await _render_logging_screen(callback.bot, state, user)


@router.callback_query(StateFilter(WorkoutFlow.logging_set), F.data.startswith("live:note:"))
async def live_note_prompt(callback: CallbackQuery, state: FSMContext):
    """Ask for a free-text note tied to the active exercise (technique cue, injury flag).
    It resurfaces above "в прошлый раз" on every later session with this exercise."""
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    await state.set_state(WorkoutFlow.logging_exercise_note)
    current = f"\n\nСейчас: <i>{escape(ex['notes'])}</i>" if ex["notes"] else ""
    hint = "\n\nПришли «-», чтобы убрать заметку." if ex["notes"] else ""
    user = await db.get_user(callback.from_user.id)
    await _refresh_live(
        callback.bot, state, user, (await state.get_data())["workout_id"],
        f"📝 Заметка к «{escape(ex['display_name'])}» — напиши текст (например «побаливает правое плечо»)."
        f"{current}{hint}",
        keyboards.cancel_keyboard("live:note_cancel"),
    )
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.logging_exercise_note), F.data == "live:note_cancel")
async def live_note_cancel(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutFlow.logging_set)
    user = await db.get_user(callback.from_user.id)
    await _render_logging_screen(callback.bot, state, user)
    await callback.answer()


@router.message(StateFilter(WorkoutFlow.logging_exercise_note), F.text)
async def live_note_entered(message: Message, state: FSMContext):
    data = await state.get_data()
    active = data.get("active_exercise_id")
    text = message.text.strip()
    await db.set_exercise_notes(active, None if text == "-" else text)
    await _delete_message(message)
    await state.set_state(WorkoutFlow.logging_set)
    user = await db.get_user(message.from_user.id)
    await _render_logging_screen(message.bot, state, user)


async def _store_parsed_sets(state: FSMContext, data: dict, active: int, parsed) -> list[tuple[float, int]]:
    """Write the parsed sets to the active block, carrying weight forward for bare
    reps, and update last_by_exercise. Returns the (weight, reps) actually logged."""
    block_id = (data.get("open_blocks") or {}).get(active)
    last_by = dict(data.get("last_by_exercise") or {})
    prev_weight, _ = last_by.get(active) or (0.0, 0)
    logged: list[tuple[float, int]] = []
    for ps in parsed:
        weight = prev_weight if (ps.weight_omitted and prev_weight) else ps.weight
        await _log_one(block_id, active, weight, ps.reps, ps.rpe)
        logged.append((weight, ps.reps))
        prev_weight = weight
    last_by[active] = (prev_weight, parsed[-1].reps)
    await state.update_data(last_by_exercise=last_by)
    return logged


def _resolve_parsed_weights(data: dict, active: int, parsed: list[ParsedSet]) -> list[ParsedSet]:
    """The sets `_store_parsed_sets` would actually write, with bare-reps input
    ("8") already filled in from the previous set's weight — so the typo check
    below sees the same numbers that would land in the DB."""
    last_by = data.get("last_by_exercise") or {}
    prev_weight, _ = last_by.get(active) or (0.0, 0)
    resolved: list[ParsedSet] = []
    for ps in parsed:
        weight = prev_weight if (ps.weight_omitted and prev_weight) else ps.weight
        resolved.append(ParsedSet(weight=weight, reps=ps.reps, rpe=ps.rpe))
        prev_weight = weight
    return resolved


def _weight_confirm_prompt(
    data: dict, active: int, resolved: list[ParsedSet], unit: str = "kg"
) -> str | None:
    """"555кг? В прошлый раз 66кг" — the question asked *before* a suspicious set
    is written, or None when nothing looks off.

    Asking up front rather than flagging after the fact is what a typo in the
    heavy direction needs: once written, an over-large set is the exercise's
    all-time record, counts toward lifetime tonnage and unlocks weight-club
    achievements that are never revoked (see parser.MAX_WEIGHT), so a nudge
    under an already-saved set comes too late to prevent any of it.
    """
    last_session = (data.get("last_session_sets") or {}).get(active)
    for ps in resolved:
        warning = _suspicious_weight_warning(last_session, [(ps.weight, ps.reps)], unit)
        if warning:
            return warning
    return None


async def _finalize_logged_sets(bot, state: FSMContext, user, data: dict, active: int,
                                logged: list[tuple[float, int]], chat_id: int, message_id: int,
                                message: Message | None = None) -> None:
    """Shared tail of logging typed sets: celebrate a record on the message that
    carried it, otherwise tidy the message away, then redraw the tracker.

    `message` is the input itself when it is still in hand; the confirmation
    path (`live_weight_confirm`) only has its chat/message ids to work from.
    """
    is_record = await _sets_beat_record(active, data["workout_id"], logged, user["e1rm_formula"])
    if is_record:
        # A record-setting message keeps its place in the chat with a 🔥 reaction —
        # instant, wordless celebration — instead of being tidied away like a normal set.
        with suppress(TelegramBadRequest):
            await bot.set_message_reaction(
                chat_id=chat_id, message_id=message_id, reaction=[ReactionTypeEmoji(emoji="🔥")],
            )
        asyncio.create_task(
            _delete_message_later(bot, chat_id, message_id, _RECORD_MESSAGE_LIFETIME_SECONDS)
        )
    elif message is not None:
        await _delete_message(message)
    else:
        with suppress(TelegramBadRequest):
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
    await _render_logging_screen(bot, state, user)


async def _finalize_voice_sets(bot, state: FSMContext, user, data: dict, active: int,
                               logged: list[tuple[float, int]], chat_id: int, message_id: int,
                               message: Message | None = None) -> None:
    """Same tail as `_finalize_logged_sets`, for voice input: the voice message
    stays in the chat (there is nothing to re-read in it) and gets a spoken-back
    "записал" so a misheard number is still catchable after the fact."""
    sets_str = ", ".join(formatting.format_set(w, r) for w, r in logged)
    text = f"🎙 Записал: {sets_str}"
    if message is not None:
        await message.reply(text)
    else:
        await bot.send_message(chat_id=chat_id, text=text, reply_to_message_id=message_id)
    if await _sets_beat_record(active, data["workout_id"], logged, user["e1rm_formula"]):
        with suppress(TelegramBadRequest):
            await bot.set_message_reaction(
                chat_id=chat_id, message_id=message_id, reaction=[ReactionTypeEmoji(emoji="🔥")],
            )
    await _render_logging_screen(bot, state, user)


async def _ask_weight_confirmation(
    message: Message, state: FSMContext, active: int, resolved: list[ParsedSet], prompt: str,
    source: str = "text",
) -> None:
    """Park the parsed sets and ask before writing them. Nothing reaches the DB
    until the answer comes back, and the user's own message is left in place so
    the numbers they typed are still on screen next to the question."""
    sent = await message.reply(
        f"{prompt}\nЗаписываем?", reply_markup=keyboards.weight_confirm_keyboard()
    )
    await state.update_data(
        pending_weight_confirm={
            "exercise_id": active,
            "sets": [[ps.weight, ps.reps, ps.rpe] for ps in resolved],
            "source": source,
            "chat_id": message.chat.id,
            "message_id": message.message_id,
            "prompt_message_id": sent.message_id,
        }
    )


async def _take_pending_weight_confirmation(bot, state: FSMContext, data: dict) -> dict | None:
    """Pop the parked confirmation (if any) and take its question off screen."""
    pending = data.get("pending_weight_confirm")
    if not pending:
        return None
    await state.update_data(pending_weight_confirm=None)
    with suppress(TelegramBadRequest):
        await bot.delete_message(
            chat_id=pending["chat_id"], message_id=pending["prompt_message_id"]
        )
    return pending


async def _discard_superseded_confirmation(bot, state: FSMContext, data: dict) -> dict:
    """New input instead of an answer means the parked set is no longer wanted:
    take the question down along with the message that raised it, and hand back
    fresh state data. A no-op when nothing is pending."""
    pending = await _take_pending_weight_confirmation(bot, state, data)
    if pending is None:
        return data
    with suppress(TelegramBadRequest):
        await bot.delete_message(chat_id=pending["chat_id"], message_id=pending["message_id"])
    return await state.get_data()


async def _apply_set_edit(state: FSMContext, data: dict, active: int, index: int, new_set: ParsedSet) -> None:
    """Overwrite the `index`-th (1-based) already-logged set of the active
    exercise, in the same order the tracker lists them. Raises ParseError if
    that index doesn't exist — caller replies it back to the user same as any
    other bad input, rather than silently doing nothing."""
    block_id = (data.get("open_blocks") or {}).get(active)
    sets = await db.list_sets_for_block(block_id) if block_id else []
    if not (1 <= index <= len(sets)):
        if not sets:
            raise ParseError("Пока нет ни одного подхода — нечего править.")
        raise ParseError(f"Нет подхода №{index} — сейчас залогировано {len(sets)}.")
    row = sets[index - 1]
    weight = row["weight"] if new_set.weight_omitted else new_set.weight
    await db.update_set(row["id"], weight, new_set.reps, new_set.rpe)
    if index == len(sets):
        # The edited set is the newest one — keep the "carry weight forward on
        # a bare-reps follow-up" pointer in sync with what it now actually is.
        last_by = dict(data.get("last_by_exercise") or {})
        last_by[active] = (weight, new_set.reps)
        await state.update_data(last_by_exercise=last_by)


@dataclass
class _UndoResult:
    removed: tuple[float, int] | None  # None means there was nothing to undo


async def _undo_last_set(bot, state: FSMContext, user, data: dict) -> _UndoResult:
    """Core of "delete the active exercise's last set", shared by the ↩️ button
    and the "-" text command. The exercise stays open even if that was its
    only logged set — same as a freshly opened exercise with nothing logged
    yet — so undoing never closes the exercise out from under the user.
    """
    active = data.get("active_exercise_id")
    block_id = (data.get("open_blocks") or {}).get(active)
    row = await db.delete_last_set_in_block(block_id)
    if row is None:
        return _UndoResult(removed=None)

    await _render_logging_screen(bot, state, user)
    return _UndoResult(removed=(row["weight"], row["reps"]))


async def _repeat_last_set(
    bot, state: FSMContext, user, data: dict
) -> tuple[float, int, float | None] | None:
    """Core of "log a copy of the active exercise's last set", shared by the
    (currently button-less, see #164) repeat action and the "=" text command.
    Returns the (weight, reps, rpe) that was logged, or None if there was
    nothing yet to repeat."""
    active = data.get("active_exercise_id")
    block_id = (data.get("open_blocks") or {}).get(active)
    sets = await db.list_sets_for_block(block_id) if block_id else []
    if not sets:
        return None
    last = sets[-1]
    await _log_one(block_id, active, last["weight"], last["reps"], last["rpe"])
    last_by = dict(data.get("last_by_exercise") or {})
    last_by[active] = (last["weight"], last["reps"])
    await state.update_data(last_by_exercise=last_by)
    await _render_logging_screen(bot, state, user)
    return last["weight"], last["reps"], last["rpe"]


@router.message(StateFilter(WorkoutFlow.logging_set), F.text)
async def log_set_text(message: Message, state: FSMContext):
    """Typed input in an active exercise. Beyond plain "weight reps" sets
    (parse_sets_line), a handful of one-line commands cover what would
    otherwise need dedicated keyboard buttons — cheaper on screen space than
    a row each, and the input field is already open with chalky hands on it:

      -          delete the last logged set
      =          repeat the last logged set
      !text      set a note on the active exercise
      N: 100 8   overwrite the Nth already-logged set (fix a typo mid-session)
      ?          show the /help reference without leaving the keyboard
    """
    text = message.text.strip()
    data = await state.get_data()
    data = await _discard_superseded_confirmation(message.bot, state, data)
    active = data.get("active_exercise_id")

    if text == "?":
        await message.reply(_HELP_TEXT, parse_mode="HTML")
        return

    if text == "-":
        user = await db.get_user(message.from_user.id)
        result = await _undo_last_set(message.bot, state, user, data)
        if result.removed is None:
            await message.reply("Нет подходов для удаления")
        else:
            await _delete_message(message)
        return

    if text == "=":
        user = await db.get_user(message.from_user.id)
        result = await _repeat_last_set(message.bot, state, user, data)
        if result is None:
            await message.reply("Нет подхода для повтора")
        else:
            await _delete_message(message)
        return

    if text.startswith("!"):
        note = text[1:].strip()
        if not note:
            await message.reply("Напиши текст после «!», например «!болит плечо — следи за локтями»")
            return
        await db.set_exercise_notes(active, note)
        await _delete_message(message)
        user = await db.get_user(message.from_user.id)
        await _render_logging_screen(message.bot, state, user)
        return

    try:
        edit = parse_set_edit(text)
    except ParseError as e:
        await message.reply(e.message)
        return
    if edit is not None:
        index, new_set = edit
        try:
            await _apply_set_edit(state, data, active, index, new_set)
        except ParseError as e:
            await message.reply(e.message)
            return
        await _delete_message(message)
        user = await db.get_user(message.from_user.id)
        await _render_logging_screen(message.bot, state, user)
        return

    try:
        parsed = parse_sets_line(text)
    except ParseError as e:
        await message.reply(e.message)
        return

    user = await db.get_user(message.from_user.id)
    resolved = _resolve_parsed_weights(data, active, parsed)
    prompt = _weight_confirm_prompt(data, active, resolved, user["unit"])
    if prompt is not None:
        await _ask_weight_confirmation(message, state, active, resolved, prompt)
        return

    logged = await _store_parsed_sets(state, data, active, parsed)
    await _finalize_logged_sets(
        message.bot, state, user, data, active, logged,
        message.chat.id, message.message_id, message=message,
    )


@router.message(StateFilter(WorkoutFlow.logging_set), F.voice)
async def log_set_voice(message: Message, state: FSMContext):
    """Log a set by voice ("сто на восемь") — hands are chalky, typing is slow.
    Reuses the AI-trainer's transcription, then the same number parser as text."""
    if not ai_trainer.is_voice_configured():
        await message.reply("Голосовой ввод пока не настроен, напиши подход текстом.")
        return
    try:
        buf = await message.bot.download(message.voice)
        buf.name = "voice.ogg"
        transcript = await ai_trainer.transcribe_voice(buf, message.from_user.id)
    except Exception:
        logger.exception("Voice set transcription failed for user %s", message.from_user.id)
        await message.reply("⚠️ Не разобрал голосовое, попробуй ещё раз или напиши текстом.")
        return

    line = voice_parse.transcript_to_sets_line(transcript or "")
    try:
        parsed = parse_sets_line(line) if line else None
    except ParseError:
        parsed = None
    if not parsed:
        heard = f" (услышал: «{escape(transcript)}»)" if transcript else ""
        await message.reply(f"Не понял вес и повторы из голосового{heard}. Скажи, например, «сто на восемь».")
        return

    data = await state.get_data()
    data = await _discard_superseded_confirmation(message.bot, state, data)
    active = data.get("active_exercise_id")
    user = await db.get_user(message.from_user.id)
    resolved = _resolve_parsed_weights(data, active, parsed)
    # Mishearing a number is at least as likely as mistyping one, so voice gets
    # the same "точно?" gate as text.
    prompt = _weight_confirm_prompt(data, active, resolved, user["unit"])
    if prompt is not None:
        await _ask_weight_confirmation(message, state, active, resolved, prompt, source="voice")
        return

    logged = await _store_parsed_sets(state, data, active, parsed)
    await _finalize_voice_sets(
        message.bot, state, user, data, active, logged,
        message.chat.id, message.message_id, message=message,
    )


@router.callback_query(StateFilter(WorkoutFlow.logging_set), F.data.startswith("live:wconf:"))
async def live_weight_confirm(callback: CallbackQuery, state: FSMContext):
    """Answer to "555кг? Записываем?" — see `_weight_confirm_prompt`."""
    data = await state.get_data()
    pending = await _take_pending_weight_confirmation(callback.bot, state, data)
    if pending is None:
        # Stale keyboard (restart, or the input was already superseded).
        await callback.answer()
        return

    if callback.data.endswith(":no"):
        # Drop the input entirely: retyping the set is one message, and leaving
        # the wrong numbers on screen would only invite tapping "да" later.
        with suppress(TelegramBadRequest):
            await callback.bot.delete_message(
                chat_id=pending["chat_id"], message_id=pending["message_id"]
            )
        await callback.answer("Не записал — набери подход заново")
        return

    active = pending["exercise_id"]
    parsed = [ParsedSet(weight=w, reps=r, rpe=rpe) for w, r, rpe in pending["sets"]]
    logged = await _store_parsed_sets(state, data, active, parsed)
    confirmed = dict(data.get("confirmed_weights") or {})
    confirmed[active] = logged[-1][0]
    await state.update_data(confirmed_weights=confirmed)
    user = await db.get_user(callback.from_user.id)
    await callback.answer()
    finalize = _finalize_voice_sets if pending.get("source") == "voice" else _finalize_logged_sets
    await finalize(
        callback.bot, state, user, data, active, logged, pending["chat_id"], pending["message_id"],
    )


@router.callback_query(StateFilter(WorkoutFlow.logging_set), F.data == "live:undo")
async def live_undo(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    result = await _undo_last_set(callback.bot, state, user, data)
    if result.removed is None:
        await callback.answer("Нет сетов для удаления")
    else:
        w, r = result.removed
        await callback.answer(f"Удалил {formatting.format_set(w, r)}")


@router.callback_query(StateFilter(WorkoutFlow.logging_set), F.data == "live:repeat")
async def live_repeat_set(callback: CallbackQuery, state: FSMContext):
    """One-tap copy of the last logged set — the "same weight, same reps" case that's
    the most common in the gym, without retyping it with chalky hands. No button
    currently sends this callback (trimmed in #164) — "=" typed in the tracker
    is the live path now; this stays wired in case a button returns to it."""
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    result = await _repeat_last_set(callback.bot, state, user, data)
    if result is None:
        await callback.answer("Нет подхода для повтора")
    else:
        w, r, rpe = result
        await callback.answer(f"➕ {formatting.format_set(w, r, rpe)}")


@router.callback_query(StateFilter(WorkoutFlow.logging_set), F.data == "live:finish_exercise")
async def live_finish_exercise(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    active = data.get("active_exercise_id")
    active_block_id = (data.get("open_blocks") or {}).get(active)
    if active_block_id is not None and not await db.list_sets_for_block(active_block_id):
        await db.delete_block(active_block_id)
    open_exercises = [eid for eid in (data.get("open_exercises") or []) if eid != active]
    open_blocks = dict(data.get("open_blocks") or {})
    open_blocks.pop(active, None)
    user = await db.get_user(callback.from_user.id)

    if open_exercises:
        await state.update_data(
            open_exercises=open_exercises, open_blocks=open_blocks, active_exercise_id=open_exercises[0],
        )
        await _render_logging_screen(callback.bot, state, user)
    else:
        await state.update_data(
            open_exercises=[], open_blocks={}, active_exercise_id=None, last_finished_exercise_id=active,
        )
        await state.set_state(WorkoutFlow.idle)
        await _enter_idle_screen(callback.bot, state, user, data["workout_id"])
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.idle), F.data.startswith("live:suggest:"))
async def live_pick_suggested(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    await _on_exercise_chosen(callback, state, ex_id)


async def _load_next_planned_block(event, state: FSMContext) -> bool:
    """Open the next block from a routine's planned_blocks. Returns False if none left.

    Shared by the "▶️ Следующее по шаблону" button and by starting a workout from
    a routine (handlers/routines.py), so both paths open blocks identically.
    """
    data = await state.get_data()
    planned = list(data.get("planned_blocks") or [])
    if not planned:
        return False
    block_plan = planned.pop(0)
    await state.update_data(planned_blocks=planned)
    workout_id = data["workout_id"]

    open_exercises: list[int] = []
    open_blocks: dict[int, int] = {}
    last_by = dict(data.get("last_by_exercise") or {})
    last_session_sets = dict(data.get("last_session_sets") or {})
    weight_steps = dict(data.get("weight_steps") or {})
    for ex_id in block_plan["exercise_ids"]:
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.touch_exercise_last_used(ex_id)
        last_by = await _seed_last_value({"last_by_exercise": last_by}, ex_id)
        last_session_sets[ex_id], step = await _exercise_history(ex_id)
        if step is not None:
            weight_steps[ex_id] = step
        open_exercises.append(ex_id)
        open_blocks[ex_id] = block_id

    await state.update_data(
        open_exercises=open_exercises, open_blocks=open_blocks,
        active_exercise_id=open_exercises[0], last_by_exercise=last_by, last_session_sets=last_session_sets,
        weight_steps=weight_steps,
    )
    await state.set_state(WorkoutFlow.logging_set)
    user = await db.get_user(event.from_user.id)
    await _render_logging_screen(event.bot, state, user)
    return True


@router.callback_query(StateFilter(WorkoutFlow.idle), F.data == "live:next_planned")
async def live_next_planned(callback: CallbackQuery, state: FSMContext):
    if not await _load_next_planned_block(callback, state):
        await callback.answer("Шаблон закончился")
        return
    await callback.answer()


# ---------- finishing the workout ----------


@router.callback_query(StateFilter(WorkoutFlow.idle), F.data == "live:finish_workout")
async def live_finish_workout(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    workout_id = data["workout_id"]
    exercise_ids = await db.list_exercise_ids_for_workout(workout_id)
    if not exercise_ids:
        await db.discard_workout(workout_id)
        await _clear_sticky_photo(callback.bot, state)
        await state.clear()
        await _show_main_menu(callback, state)
        await callback.answer("Тренировка была пустая — удалил её.")
        return
    workout = await db.get_workout(workout_id)
    user = await db.get_user(callback.from_user.id)
    started = dt.datetime.fromisoformat(workout["started_at"])
    started_local = timeutil.to_user_local(started, user)
    today_local = timeutil.user_today(user)
    if not data.get("is_backfill") and started_local.date() != today_local:
        await state.set_state(WorkoutFlow.confirming_finish_date)
        await ui.safe_edit(
            callback,
            f"⚠️ Тренировка начата {formatting.format_date_ru(started_local)}, а сегодня "
            f"{formatting.format_date_ru(today_local)}.\n\nВсё верно?",
            reply_markup=keyboards.finish_date_mismatch_keyboard(),
        )
        await callback.answer()
        return
    await _finalize_workout(callback, state, note=None)


@router.callback_query(StateFilter(WorkoutFlow.confirming_finish_date), F.data == "finconfirm:keep")
async def finish_confirm_keep(callback: CallbackQuery, state: FSMContext):
    await _finalize_workout(callback, state, note=None)


@router.callback_query(StateFilter(WorkoutFlow.confirming_finish_date), F.data == "finconfirm:changedate")
async def finish_confirm_changedate(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutFlow.awaiting_finish_date)
    # The user's own today, matching the mismatch warning that led here — a
    # calendar built on the server's date would mark a different day as "сегодня"
    # than the message just named.
    today = timeutil.user_today(await db.get_user(callback.from_user.id))
    await ui.safe_edit(
        callback,
        "На какую дату перенести тренировку?\nВыбери в календаре или напиши дату в формате дд.мм.гггг:",
        reply_markup=keyboards.calendar_keyboard("findate", today.year, today.month, today=today),
    )
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.awaiting_finish_date), F.data.startswith("findate:cal:"))
async def finish_date_cal_nav(callback: CallbackQuery, state: FSMContext):
    year, month = (int(x) for x in callback.data.split(":")[2].split("-"))
    today = timeutil.user_today(await db.get_user(callback.from_user.id))
    with suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(
            reply_markup=keyboards.calendar_keyboard("findate", year, month, today=today)
        )
    await callback.answer()


@router.callback_query(F.data == "findate:noop")
async def finish_date_noop(callback: CallbackQuery):
    await callback.answer()


async def _apply_finish_date(workout_id: int, new_date: dt.date) -> None:
    workout = await db.get_workout(workout_id)
    started = dt.datetime.fromisoformat(workout["started_at"])
    new_started = dt.datetime.combine(new_date, started.time())
    await db.update_workout_date(
        workout_id, new_started.isoformat(timespec="seconds"), workout["finished_at"]
    )


@router.callback_query(StateFilter(WorkoutFlow.awaiting_finish_date), F.data.startswith("findate:date:"))
async def finish_date_quick(callback: CallbackQuery, state: FSMContext):
    date = dt.date.fromisoformat(callback.data.split(":", 2)[2])
    data = await state.get_data()
    await _apply_finish_date(data["workout_id"], date)
    await _finalize_workout(callback, state, note=None)


@router.message(StateFilter(WorkoutFlow.awaiting_finish_date))
async def finish_date_text(message: Message, state: FSMContext):
    try:
        date = parse_ru_date(message.text, today=timeutil.user_today(await db.get_user(message.from_user.id)))
    except ParseError as e:
        await message.reply(e.message)
        return
    data = await state.get_data()
    await _apply_finish_date(data["workout_id"], date)
    await _finalize_workout(message, state, note=None)


@router.callback_query(StateFilter(WorkoutFlow.awaiting_finish_date), F.data == "findate:cancel")
async def finish_date_cancel(callback: CallbackQuery, state: FSMContext):
    await _finalize_workout(callback, state, note=None)


@router.callback_query(StateFilter(WorkoutFlow.confirming_finish_date), F.data == "live:cancel_finish")
async def cancel_finish(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await _back_after_cancel(callback.bot, state, user)
    await callback.answer()


async def _record_highlights_and_summary(
    workout, user, note: str | None
) -> tuple[Callable[[int | None], str], str, float, float | None]:
    """Recomputes the per-exercise PR/comparison highlights, this session's total
    tonnage, and a builder for the header+sets summary text of a finished workout.

    The summary comes back as a callable(max_chars) rather than a finished
    string: callers combine it with tonnage/highlights/achievements/AI-comment
    text they assemble themselves, and only once all of that is known can the
    summary's own length budget be computed (see formatting.fit_workout_text) —
    building the final string here would be measuring the wrong thing.

    Split out of _finalize_workout so the same computation can re-render the
    completion card later when a note is attached via "📝 Заметка" — it only
    depends on what's already saved in the DB, not on anything from the finish flow.
    """
    workout_id = workout["id"]
    formula = user["e1rm_formula"]
    exercise_ids = await db.list_exercise_ids_for_workout(workout_id)
    highlight_groups: list[tuple[str, list[str], str | None]] = []
    session_tonnage = 0.0
    started_at = dt.datetime.fromisoformat(workout["started_at"])

    for ex_id in exercise_ids:
        ex = await db.get_exercise(ex_id)
        history_rows = await db.list_sets_for_exercise(ex_id, exclude_workout_id=workout_id)
        history_set_rows = [
            analytics.SetRow(r["weight"], r["reps"], r["workout_id"], r["started_at"])
            for r in history_rows
            if r["started_at"] < workout["started_at"]
        ]
        prior_sessions = analytics.group_sets_by_session(history_set_rows)
        for s in prior_sessions:
            s.formula = formula

        this_rows = await db.list_sets_for_workout_exercise(workout_id, ex_id)
        this_set_rows = [
            analytics.SetRow(r["weight"], r["reps"], workout_id, workout["started_at"])
            for r in this_rows
        ]
        new_session = analytics.SessionStats(
            workout_id=workout_id, started_at=workout["started_at"], sets=this_set_rows, formula=formula
        )
        if not new_session.sets:
            continue
        session_tonnage += new_session.tonnage

        records = analytics.detect_new_records(prior_sessions, new_session)
        pr_details = [
            formatting.format_pr_detail(r.kind, r.value, r.extra, unit=user["unit"])
            for r in records
            if r.kind != "e1rm"
        ]

        comparison_line = None
        if prior_sessions and not new_session.is_bodyweight_mode:
            prior_pr = analytics.compute_personal_records(prior_sessions)
            e1rm_delta = new_session.top_e1rm - prior_pr.max_e1rm
            if e1rm_delta > 0:
                comparison_line = formatting.format_comparison_line(e1rm_delta, unit=user["unit"])

        if pr_details or comparison_line:
            highlight_groups.append((ex["display_name"], pr_details, comparison_line))

    blocks = await view_builder.build_block_views(
        workout_id, formula, previous_before=workout["started_at"]
    )
    duration_seconds = await view_builder.workout_duration_seconds(workout)

    def summary_fn(max_chars: int | None) -> str:
        return formatting.build_workout_summary(
            started_at, blocks, note, show_extra_stats=bool(user["show_extra_stats"]),
            duration_seconds=duration_seconds, unit=user["unit"], max_chars=max_chars,
        )

    highlights = formatting.build_exercise_highlights(highlight_groups)
    return summary_fn, highlights, session_tonnage, duration_seconds


_UNSET = object()


def _finished_workout_ai_button_visible(workout, user) -> bool:
    existing_comment = workout["ai_comment"]
    needs_ai_comment = existing_comment is None and bool(user["ai_comments_enabled"]) and ai_trainer.is_configured()
    return existing_comment is None and not needs_ai_comment and ai_trainer.is_configured()


async def _finished_workout_card_text(workout, user, note: str | None, comment=_UNSET) -> str:
    """The completion card's body for an already-finished workout: sets, PR
    highlights, tonnage-equivalent and any AI-trainer comment — everything
    that's still true if you look at the workout again later, e.g. right after
    attaching a note via "📝 Заметка", or when reopening it from history.
    Deliberately excludes the milestone/achievement banners, which only belong
    to the moment of finishing.

    `comment` defaults to whatever's already saved on the workout row; pass an
    explicit value (including None) when the caller has just resolved one
    itself, e.g. history's show_history_item generating a fresh comment via
    ai_trainer.ensure_workout_comment.
    """
    summary_fn, highlights, session_tonnage, _duration = await _record_highlights_and_summary(
        workout, user, note
    )
    suffix = ""
    equivalent = formatting.format_tonnage_equivalent(session_tonnage, seed=workout["id"])
    if equivalent:
        tonnage = formatting.format_tonnage(session_tonnage)
        suffix += f"\n\n🏋️ Суммарно за тренировку — {tonnage}. {equivalent}"
    if highlights:
        header = "🔥 <b>Рекорды и сравнения</b>"
        suffix += f"\n{formatting.DIVIDER}\n{header}\n{formatting.collapsible_if_long(highlights)}"
    effective_comment = workout["ai_comment"] if comment is _UNSET else comment
    if effective_comment:
        suffix += "\n" + formatting.build_ai_comment_block(effective_comment)
    return formatting.fit_workout_text(summary_fn, suffix)


@router.callback_query(F.data.startswith("live:addnote:"))
async def workout_card_note_prompt(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[2])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    await state.update_data(
        note_workout_id=workout_id,
        note_chat_id=callback.message.chat.id,
        note_message_id=callback.message.message_id,
    )
    await state.set_state(WorkoutFlow.editing_finished_note)
    current = f"\n\nСейчас: <i>{escape(workout['note'])}</i>" if workout["note"] else ""
    hint = "\n\nПришли «-», чтобы убрать заметку." if workout["note"] else ""
    await callback.message.answer(
        f"Заметка к тренировке — напиши текст (сон, самочувствие, что угодно).{current}{hint}",
        reply_markup=keyboards.cancel_keyboard("live:addnote_cancel"),
    )
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.editing_finished_note), F.data == "live:addnote_cancel")
async def workout_card_note_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()


@router.message(StateFilter(WorkoutFlow.editing_finished_note))
async def workout_card_note_entered(message: Message, state: FSMContext):
    data = await state.get_data()
    workout_id = data["note_workout_id"]
    text = message.text.strip()
    note = None if text == "-" else text
    await db.update_workout_note(workout_id, note)
    await state.clear()

    workout = await db.get_workout(workout_id)
    user = await db.get_user(message.from_user.id)
    full_text = await _finished_workout_card_text(workout, user, note)
    card_kb = keyboards.workout_card_keyboard(
        workout_id, show_ai_button=_finished_workout_ai_button_visible(workout, user)
    )
    with suppress(TelegramBadRequest):
        await message.bot.edit_message_text(
            chat_id=data["note_chat_id"], message_id=data["note_message_id"], text=full_text,
            parse_mode="HTML", reply_markup=card_kb,
        )
    await message.reply("📝 Заметка сохранена.")


async def _finalize_workout(event, state: FSMContext, note: str | None):
    data = await state.get_data()
    workout_id = data["workout_id"]
    user_id = event.from_user.id
    bot = event.bot

    # Guards against a double-tap on "finish" (e.g. two quick taps on
    # "✅ Без заметки") racing each other into this function before the
    # first call's state.clear() lands — without this, both calls would
    # finalize the same workout and produce duplicate PR messages/menus.
    workout = await db.get_workout(workout_id)
    if workout is None or workout["status"] == "finished":
        if isinstance(event, CallbackQuery):
            await event.answer()
        return

    user = await db.get_user(user_id)
    started_at = dt.datetime.fromisoformat(workout["started_at"])

    is_backfill = bool(data.get("is_backfill"))
    finished_at = f"{data['bf_date']}T12:00:00" if is_backfill else None
    await db.delete_empty_blocks(workout_id)
    await db.finish_workout(workout_id, note, finished_at=finished_at)
    workout = await db.get_workout(workout_id)

    summary_fn, highlights, session_tonnage, duration_seconds = await _record_highlights_and_summary(
        workout, user, note
    )
    suffix = ""
    equivalent = formatting.format_tonnage_equivalent(session_tonnage, seed=workout_id)
    if equivalent:
        tonnage = formatting.format_tonnage(session_tonnage)
        suffix += f"\n\n🏋️ Суммарно за тренировку — {tonnage}. {equivalent}"
    # Backfilled/imported past workouts shouldn't fire the "Nth workout" milestone —
    # they're entered out of order, so the running count isn't meaningful for them.
    if not is_backfill:
        total_finished = await db.count_workouts(user_id)
        if analytics.is_workout_milestone(total_finished):
            suffix += "\n\n" + formatting.format_milestone_line(total_finished)

    new_badges = await _evaluate_achievements(user_id, workout_id, started_at, duration_seconds)
    achievement_line = formatting.format_new_achievements(new_badges)
    if achievement_line:
        suffix += "\n\n" + achievement_line

    if highlights:
        header = "🔥 <b>Рекорды и сравнения</b>"
        suffix += f"\n{formatting.DIVIDER}\n{header}\n{formatting.collapsible_if_long(highlights)}"

    # Existing comment (already generated, e.g. from a backfilled workout) shows right
    # away; a fresh one is generated in the background so finishing a workout doesn't
    # block on the LLM call — see _attach_ai_comment below.
    existing_comment = workout["ai_comment"]
    if existing_comment:
        suffix += "\n" + formatting.build_ai_comment_block(existing_comment)
    needs_ai_comment = (
        existing_comment is None and bool(user["ai_comments_enabled"]) and ai_trainer.is_configured()
    )

    prefix = "✅ Сохранено как прошлая тренировка\n\n" if is_backfill else ""
    full_text = formatting.fit_workout_text(lambda mc: prefix + summary_fn(mc), suffix)
    card_kb = keyboards.workout_card_keyboard(
        workout_id,
        show_ai_button=existing_comment is None and not needs_ai_comment and ai_trainer.is_configured(),
    )
    message_id = data["live_message_id"]
    try:
        sent = await bot.edit_message_text(
            chat_id=data["live_chat_id"], message_id=message_id, text=full_text,
            parse_mode="HTML", reply_markup=card_kb,
        )
        if isinstance(sent, Message):
            message_id = sent.message_id
    except TelegramBadRequest:
        sent = await bot.send_message(
            chat_id=data["live_chat_id"], text=full_text, parse_mode="HTML", reply_markup=card_kb
        )
        message_id = sent.message_id

    if needs_ai_comment:
        asyncio.create_task(
            _attach_ai_comment(bot, data["live_chat_id"], message_id, user_id, workout_id, full_text)
        )

    await _clear_sticky_photo(bot, state)
    await state.clear()
    # No auto-sent menu message here on purpose: it used to bury the card (the
    # PR/comparison highlights, the AI comment) the instant it appeared. The
    # card's own "⬅️ В меню" button (live:back_to_menu below) opens the menu
    # in its place instead, so it's a beat the user chooses, not one forced on them.
