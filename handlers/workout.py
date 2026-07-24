"""Workout lifecycle: start, add exercises, switch between them, log sets, finish."""

import asyncio
import datetime as dt
import logging
from collections import Counter
from contextlib import suppress
from html import escape

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
from parser import ParseError, parse_ru_date, parse_sets_line

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
    new_text = base_text + "\n\n" + formatting.build_ai_comment_block(comment)
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
    """Re-send the live tracker message so it always sits at the bottom of the chat.

    Telegram doesn't let a bot move an edited message down past newer messages
    (e.g. the weight/reps the user just typed), so we delete and resend instead
    of editing in place.
    """
    data = await state.get_data()
    chat_id = data["live_chat_id"]
    blocks = await view_builder.build_block_views(workout_id, user["e1rm_formula"])
    active = data.get("active_exercise_id")
    blocks = _move_open_exercises_last(blocks, data.get("open_exercises") or [], active)
    text = formatting.build_live_session_text(blocks, hint, active_exercise_id=active)
    if data.get("is_backfill") and data.get("bf_date"):
        date = dt.date.fromisoformat(data["bf_date"])
        text = f"📅 {formatting.format_date_ru(date)}\n\n{text}"
    with suppress(TelegramBadRequest):
        await bot.delete_message(chat_id=chat_id, message_id=data["live_message_id"])
    sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(live_message_id=sent.message_id)


async def _suggested_next_exercise(user_id: int, last_finished_id: int | None):
    """What the user did right after `last_finished_id` last time, for a one-tap suggestion."""
    if last_finished_id is None:
        return None
    workout_id = await db.find_last_finished_workout_with_exercise(user_id, last_finished_id)
    if workout_id is None:
        return None
    nxt = await db.get_next_exercise_in_workout(workout_id, last_finished_id)
    if nxt is None or nxt["exercise_id"] == last_finished_id:
        return None
    ex = await db.get_exercise(nxt["exercise_id"])
    if ex is None or ex["is_archived"]:
        return None
    return ex["id"], ex["display_name"]


async def _idle_view(data: dict, user_id: int, is_empty: bool = False) -> tuple[str | None, InlineKeyboardMarkup]:
    has_planned = bool(data.get("planned_blocks"))
    suggested = await _suggested_next_exercise(user_id, data.get("last_finished_exercise_id"))
    hint = f"💡 В прошлый раз дальше было: <b>{escape(suggested[1])}</b>" if suggested else None
    kb = keyboards.exercise_picker_entry_keyboard(has_planned=has_planned, suggested=suggested, is_empty=is_empty)
    return hint, kb


async def _enter_idle_screen(bot, state: FSMContext, user, workout_id: int):
    data = await state.get_data()
    is_empty = not await db.list_exercise_ids_for_workout(workout_id)
    hint, kb = await _idle_view(data, user["telegram_id"], is_empty=is_empty)
    await _refresh_live(bot, state, user, workout_id, hint, kb)


async def _delete_message(message: Message):
    with suppress(TelegramBadRequest):
        await message.delete()


async def _log_one(block_id: int, exercise_id: int, weight: float, reps: int, rpe: float | None = None):
    round_idx = await db.next_round_index(block_id, exercise_id)
    await db.add_set(block_id, exercise_id, round_idx, 0, weight, reps, rpe)


# Smallest sensible plate/step to suggest bumping to when a lift outgrows the
# rep range — a rough default, since the bot doesn't know the actual increment.
_WEIGHT_STEP = {"kg": 2.5, "lb": 5.0}


def _logging_hint(
    last_session: list[tuple[float, int, float | None]] | None,
    has_sets: bool,
    unit: str = "kg",
    show_progression: bool = True,
    today_sets: list[tuple[float, int]] | None = None,
    note: str | None = None,
) -> str:
    base = "Вес и повторы через пробел, например «100 8»"
    if has_sets:
        base += " (можно только повторы — вес возьмётся с последнего подхода)"
    note_line = f"📝 <i>{escape(note)}</i>\n" if note else ""
    if last_session:
        sets_str = ", ".join(formatting.format_set(w, r, rpe) for w, r, rpe in last_session)
        line = f"💡 В прошлый раз: {sets_str}."
        if show_progression:
            wr_only = [(w, r) for w, r, _ in last_session]
            suggestion = analytics.suggest_progression(wr_only, _WEIGHT_STEP.get(unit, 2.5))
            if suggestion is not None:
                achieved = any(
                    w >= suggestion.target_weight and r >= suggestion.target_reps
                    for w, r in (today_sets or [])
                )
                line += f" {formatting.format_progression_hint(suggestion, unit, achieved)}"
        return f"{note_line}<i>{line}</i>\n\n{base}"
    if note_line:
        return f"{note_line}\n{base}"
    return base


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


async def _last_session_sets(ex_id: int) -> list[tuple[float, int, float | None]]:
    """Working sets (weight, reps, rpe) from this exercise's most recent finished workout."""
    rows = await db.list_sets_for_exercise(ex_id)
    if not rows:
        return []
    last_workout_id = rows[-1]["workout_id"]
    return [(r["weight"], r["reps"], r["rpe"]) for r in rows if r["workout_id"] == last_workout_id]


async def _render_logging_screen(bot, state: FSMContext, user):
    data = await state.get_data()
    open_ids: list[int] = data.get("open_exercises") or []
    active = data.get("active_exercise_id")
    last_session_sets = data.get("last_session_sets") or {}

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
    hint = _logging_hint(
        last_session_sets.get(active),
        has_sets,
        user["unit"],
        bool(user["progression_hint_enabled"]),
        today_sets,
        note=active_note,
    )
    kb = keyboards.logging_keyboard(open_items, active, has_sets)
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


async def _menu_view(user_id: int) -> tuple[str, bytes | None]:
    """Greeting, plus a year heatmap image (with the streak/this-week/30-day
    dashboard stats drawn into it) once the user has any finished workouts.
    """
    today = timeutil.user_today(await db.get_user(user_id))
    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id)]
    if not dates:
        return _ONBOARDING, None
    dashboard = analytics.compute_dashboard(dates, today)
    this_monday = today - dt.timedelta(days=today.weekday())
    year_ago = this_monday - dt.timedelta(weeks=52)
    first_monday = min(dates) - dt.timedelta(days=min(dates).weekday())
    heatmap_start = max(first_monday, year_ago)
    stat_lines = formatting.dashboard_stat_lines(dashboard)
    png = await asyncio.to_thread(charts.render_year_heatmap, Counter(dates), today, heatmap_start, stat_lines)
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


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
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
    await state.clear()
    active = await db.get_active_workout(callback.from_user.id)
    text, png = await _menu_view(callback.from_user.id)
    kb = await _main_menu_kb(callback.from_user.id, active)
    if png is None:
        await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML", delete=delete_current)
    else:
        await ui.safe_edit_photo(
            callback, png, "year.png", text, reply_markup=kb, parse_mode="HTML", delete=delete_current
        )


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
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.picking_group), F.data == "pick:repeat")
async def pick_repeat_last(callback: CallbackQuery, state: FSMContext):
    """Pre-load the current (already-started) workout with the exercises (and
    supersets) of the last finished one — the same planned-blocks machinery a
    saved program uses. Reached from the first picker screen of a fresh workout."""
    plan = await db.last_finished_workout_plan(callback.from_user.id)
    if not plan:
        await callback.answer("Нет прошлой тренировки для повтора", show_alert=True)
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
    await _enter_live(callback, state, active["id"])


async def _reopen_exercises(
    workout_id: int,
) -> tuple[list[int], dict[int, int], dict[int, list], dict[int, tuple]]:
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
    last_session_sets = {ex_id: await _last_session_sets(ex_id) for ex_id in open_exercises}
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
    return open_exercises, open_blocks, last_session_sets, last_by_exercise


async def _enter_live(
    callback: CallbackQuery, state: FSMContext, workout_id: int, delete_message: bool = True
):
    # delete_message=False when entering from the AI-trainer chat (its "К тренировке"
    # button) — that message is part of the user's chat history with the AI-тренер,
    # not a disposable menu screen, so it should stay instead of being deleted.
    user = await _ensure_user(callback.from_user.id, callback.from_user.username)
    if delete_message:
        await _delete_message(callback.message)
    sent = await callback.message.answer("🏋️ Тренировка")
    open_exercises, open_blocks, last_session_sets, last_by_exercise = await _reopen_exercises(workout_id)
    active_exercise_id = open_exercises[-1] if open_exercises else None
    await state.set_state(WorkoutFlow.logging_set if open_exercises else WorkoutFlow.idle)
    await state.update_data(
        workout_id=workout_id, live_chat_id=sent.chat.id, live_message_id=sent.message_id,
        last_by_exercise=last_by_exercise, open_exercises=open_exercises, open_blocks=open_blocks,
        active_exercise_id=active_exercise_id, last_session_sets=last_session_sets,
    )
    if open_exercises:
        await _render_logging_screen(callback.bot, state, user)
    else:
        await _enter_idle_screen(callback.bot, state, user, workout_id)


# ---------- picker: add an exercise (either to start, or alongside what's already open) ----------

async def _picker_screen_groups(callback: CallbackQuery, state: FSMContext, show_program_button: bool = False):
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    groups = await db.list_muscle_groups(callback.from_user.id)
    hint = "Выбери группу мышц или напиши название упражнения для поиска:"
    open_ids = data.get("open_exercises") or []
    if open_ids:
        names = [escape((await db.get_exercise(eid))["display_name"]) for eid in open_ids]
        hint = "Открыто сейчас: " + ", ".join(names) + "\n" + hint
    extra = []
    if show_program_button:
        # Offered only on the very first picker screen of a fresh workout: a
        # one-tap re-run of the last session for people who train A/B without a
        # saved program, plus the shortcut into saved programs.
        if await db.count_workouts(callback.from_user.id) > 0:
            extra.append(("🔁 Повторить прошлую", "pick:repeat"))
        extra.append(("🗂 Выбрать программу", "rt:manage"))
    extra.append(("❌ Отмена", "pick:cancel"))
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
    kb = keyboards.exercises_keyboard(results, prefix="pick", back_cb="back", show_new_button=group_id is not None)
    if results:
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


async def _send_exercise_photos(message: Message, ex) -> None:
    images = exercise_media.get_images(ex["name"])
    if images:
        caption = f"Название: {ex['name']}"
        description = exercise_descriptions.get_description(ex["name"])
        if description:
            caption += f"\n\n{description}"
        media = [InputMediaPhoto(media=FSInputFile(images[0]), caption=caption)]
        media += [InputMediaPhoto(media=FSInputFile(p)) for p in images[1:]]
        await message.answer_media_group(media)


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


@router.callback_query(StateFilter(WorkoutFlow.creating_exercise_name), F.data.startswith("pick:tpladd:"))
async def pick_template_add(callback: CallbackQuery, state: FSMContext):
    template_id = int(callback.data.split(":")[2])
    ex_id = await db.fork_exercise_from_template(callback.from_user.id, template_id)
    ex = await db.get_exercise(ex_id)
    with suppress(TelegramBadRequest):
        await callback.message.delete()
    await _send_exercise_photos(callback.message, ex)
    await _on_exercise_chosen(callback, state, ex_id)


@router.message(StateFilter(WorkoutFlow.creating_exercise_name))
async def new_exercise_name_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.reply("Название не может быть пустым")
        return
    await _delete_message(message)
    data = await state.get_data()
    ex_id = await db.create_exercise(message.from_user.id, name, data["pending_group_id"])
    await _on_exercise_chosen(message, state, ex_id)


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
    last_session_sets[ex_id] = await _last_session_sets(ex_id)

    await state.update_data(
        open_exercises=open_exercises, open_blocks=open_blocks,
        active_exercise_id=ex_id, last_by_exercise=last_by, last_session_sets=last_session_sets,
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


async def _send_exercise_card(message: Message, state: FSMContext, ex) -> None:
    """Sends the exercise's reference photo(s) + technique info as a separate,
    dismissable card (custom photo takes priority, same as the ⚙️ Упражнения flow),
    with a button to jump back to the live logging screen."""
    from handlers.exercises import _exercise_info_text

    text = _exercise_info_text(ex, with_created=False)
    back_kb = keyboards.exercise_card_back_keyboard()
    msg_ids: list[int] = []
    if ex["custom_photo_file_id"]:
        sent = await message.answer_photo(ex["custom_photo_file_id"], caption=text, parse_mode="HTML", reply_markup=back_kb)
        msg_ids.append(sent.message_id)
    else:
        images = exercise_media.get_images(ex["name"])
        if images:
            # Telegram doesn't allow a reply_markup on media group items, so the
            # back button still needs its own message — kept text-free (zero-width space).
            media = [InputMediaPhoto(media=FSInputFile(images[0]), caption=text, parse_mode="HTML")]
            media += [InputMediaPhoto(media=FSInputFile(p)) for p in images[1:]]
            sent_group = await message.answer_media_group(media)
            msg_ids.extend(m.message_id for m in sent_group)
            back = await message.answer("​", reply_markup=back_kb)
            msg_ids.append(back.message_id)
        else:
            sent = await message.answer(text, parse_mode="HTML", reply_markup=back_kb)
            msg_ids.append(sent.message_id)
    await state.update_data(live_card_msg_ids=msg_ids)


@router.callback_query(StateFilter(WorkoutFlow.logging_set), F.data.startswith("live:card:"))
async def live_card_show(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    await _send_exercise_card(callback.message, state, ex)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.logging_set), F.data == "live:card_back")
async def live_card_back(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    for mid in data.get("live_card_msg_ids") or []:
        with suppress(TelegramBadRequest):
            await callback.bot.delete_message(callback.message.chat.id, mid)
    await state.update_data(live_card_msg_ids=None)
    await callback.answer()


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
    user = await db.get_user(callback.from_user.id)
    await _refresh_live(
        callback.bot, state, user, (await state.get_data())["workout_id"],
        f"📝 Заметка к «{escape(ex['display_name'])}» — напиши текст (например «болит плечо — следи за локтями»).{current}",
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
    await db.set_exercise_notes(active, message.text.strip())
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


@router.message(StateFilter(WorkoutFlow.logging_set), F.text)
async def log_set_text(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        parsed = parse_sets_line(message.text)
    except ParseError as e:
        await message.reply(e.message)
        return
    active = data.get("active_exercise_id")
    logged = await _store_parsed_sets(state, data, active, parsed)

    user = await db.get_user(message.from_user.id)
    # A record-setting message keeps its place in the chat with a 🔥 reaction —
    # instant, wordless celebration — instead of being tidied away like a normal set.
    is_record = await _sets_beat_record(active, data["workout_id"], logged, user["e1rm_formula"])
    if is_record:
        with suppress(TelegramBadRequest):
            await message.bot.set_message_reaction(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji="🔥")],
            )
    else:
        await _delete_message(message)
    await _render_logging_screen(message.bot, state, user)


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
    active = data.get("active_exercise_id")
    logged = await _store_parsed_sets(state, data, active, parsed)
    user = await db.get_user(message.from_user.id)
    sets_str = ", ".join(formatting.format_set(w, r) for w, r in logged)
    await message.reply(f"🎙 Записал: {sets_str}")
    if await _sets_beat_record(active, data["workout_id"], logged, user["e1rm_formula"]):
        with suppress(TelegramBadRequest):
            await message.bot.set_message_reaction(
                chat_id=message.chat.id, message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji="🔥")],
            )
    await _render_logging_screen(message.bot, state, user)


@router.callback_query(StateFilter(WorkoutFlow.logging_set), F.data == "live:undo")
async def live_undo(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    active = data.get("active_exercise_id")
    block_id = (data.get("open_blocks") or {}).get(active)
    row = await db.delete_last_set_in_block(block_id)
    if row is None:
        await callback.answer("Нет сетов для удаления")
        return
    await callback.answer(f"Удалил {formatting.format_set(row['weight'], row['reps'])}")
    user = await db.get_user(callback.from_user.id)

    remaining = await db.list_sets_for_block(block_id)
    if not remaining:
        await db.delete_block(block_id)
        open_exercises = [eid for eid in (data.get("open_exercises") or []) if eid != active]
        open_blocks = dict(data.get("open_blocks") or {})
        open_blocks.pop(active, None)
        if open_exercises:
            await state.update_data(
                open_exercises=open_exercises, open_blocks=open_blocks, active_exercise_id=open_exercises[0],
            )
        else:
            await state.update_data(open_exercises=[], open_blocks={}, active_exercise_id=None)
            await state.set_state(WorkoutFlow.idle)
            await _enter_idle_screen(callback.bot, state, user, data["workout_id"])
            return

    await _render_logging_screen(callback.bot, state, user)


@router.callback_query(StateFilter(WorkoutFlow.logging_set), F.data == "live:repeat")
async def live_repeat_set(callback: CallbackQuery, state: FSMContext):
    """One-tap copy of the last logged set — the "same weight, same reps" case that's
    the most common in the gym, without retyping it with chalky hands."""
    data = await state.get_data()
    active = data.get("active_exercise_id")
    block_id = (data.get("open_blocks") or {}).get(active)
    sets = await db.list_sets_for_block(block_id) if block_id else []
    if not sets:
        await callback.answer("Нет подхода для повтора")
        return
    last = sets[-1]
    await _log_one(block_id, active, last["weight"], last["reps"], last["rpe"])
    last_by = dict(data.get("last_by_exercise") or {})
    last_by[active] = (last["weight"], last["reps"])
    await state.update_data(last_by_exercise=last_by)
    await callback.answer(f"➕ {formatting.format_set(last['weight'], last['reps'])}")
    user = await db.get_user(callback.from_user.id)
    await _render_logging_screen(callback.bot, state, user)


@router.callback_query(StateFilter(WorkoutFlow.logging_set), F.data == "live:finish_exercise")
async def live_finish_exercise(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    active = data.get("active_exercise_id")
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
    for ex_id in block_plan["exercise_ids"]:
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.touch_exercise_last_used(ex_id)
        last_by = await _seed_last_value({"last_by_exercise": last_by}, ex_id)
        last_session_sets[ex_id] = await _last_session_sets(ex_id)
        open_exercises.append(ex_id)
        open_blocks[ex_id] = block_id

    await state.update_data(
        open_exercises=open_exercises, open_blocks=open_blocks,
        active_exercise_id=open_exercises[0], last_by_exercise=last_by, last_session_sets=last_session_sets,
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

_FINISH_PROMPT = "Завершаем? Можно добавить заметку (сон/самочувствие):"


@router.callback_query(StateFilter(WorkoutFlow.idle), F.data == "live:finish_workout")
async def live_finish_workout(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    workout_id = data["workout_id"]
    exercise_ids = await db.list_exercise_ids_for_workout(workout_id)
    if not exercise_ids:
        await db.discard_workout(workout_id)
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
    await ui.safe_edit(callback, _FINISH_PROMPT, reply_markup=keyboards.finish_workout_keyboard())
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.confirming_finish_date), F.data == "finconfirm:keep")
async def finish_confirm_keep(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutFlow.idle)
    await ui.safe_edit(callback, _FINISH_PROMPT, reply_markup=keyboards.finish_workout_keyboard())
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.confirming_finish_date), F.data == "finconfirm:changedate")
async def finish_confirm_changedate(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutFlow.awaiting_finish_date)
    today = dt.date.today()
    await ui.safe_edit(
        callback,
        "На какую дату перенести тренировку?\nВыбери в календаре или напиши дату в формате дд.мм.гггг:",
        reply_markup=keyboards.calendar_keyboard("findate", today.year, today.month),
    )
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.awaiting_finish_date), F.data.startswith("findate:cal:"))
async def finish_date_cal_nav(callback: CallbackQuery, state: FSMContext):
    year, month = (int(x) for x in callback.data.split(":")[2].split("-"))
    with suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(
            reply_markup=keyboards.calendar_keyboard("findate", year, month)
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
    await state.set_state(WorkoutFlow.idle)
    await ui.safe_edit(
        callback,
        f"✅ Дата изменена на {formatting.format_date_ru(date)}.\n\n{_FINISH_PROMPT}",
        reply_markup=keyboards.finish_workout_keyboard(),
    )
    await callback.answer()


@router.message(StateFilter(WorkoutFlow.awaiting_finish_date))
async def finish_date_text(message: Message, state: FSMContext):
    try:
        date = parse_ru_date(message.text)
    except ParseError as e:
        await message.reply(e.message)
        return
    data = await state.get_data()
    await _apply_finish_date(data["workout_id"], date)
    await state.set_state(WorkoutFlow.idle)
    await message.answer(
        f"✅ Дата изменена на {formatting.format_date_ru(date)}.\n\n{_FINISH_PROMPT}",
        reply_markup=keyboards.finish_workout_keyboard(),
    )


@router.callback_query(StateFilter(WorkoutFlow.awaiting_finish_date), F.data == "findate:cancel")
async def finish_date_cancel(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutFlow.idle)
    await ui.safe_edit(callback, _FINISH_PROMPT, reply_markup=keyboards.finish_workout_keyboard())
    await callback.answer()


@router.callback_query(
    StateFilter(WorkoutFlow.idle, WorkoutFlow.confirming_finish_date), F.data == "live:cancel_finish"
)
async def cancel_finish(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await _back_after_cancel(callback.bot, state, user)
    await callback.answer()


@router.callback_query(StateFilter(WorkoutFlow.idle), F.data == "finish:note")
async def finish_ask_note(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutFlow.finishing_note)
    await ui.safe_edit(
        callback,
        "Напиши заметку (сон, самочувствие, что угодно):",
        reply_markup=keyboards.cancel_keyboard("live:cancel_finish"),
    )
    await callback.answer()


@router.message(StateFilter(WorkoutFlow.finishing_note))
async def finish_note_entered(message: Message, state: FSMContext):
    await _finalize_workout(message, state, note=message.text.strip())


@router.callback_query(StateFilter(WorkoutFlow.idle), F.data == "finish:skip_note")
async def finish_skip_note(callback: CallbackQuery, state: FSMContext):
    await _finalize_workout(callback, state, note=None)


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

    is_backfill = bool(data.get("is_backfill"))
    finished_at = f"{data['bf_date']}T12:00:00" if is_backfill else None
    await db.delete_empty_blocks(workout_id)
    await db.finish_workout(workout_id, note, finished_at=finished_at)

    blocks = await view_builder.build_block_views(
        workout_id, formula, previous_before=workout["started_at"]
    )
    duration_seconds = await view_builder.workout_duration_seconds(await db.get_workout(workout_id))
    summary = formatting.build_workout_summary(
        started_at, blocks, note, show_extra_stats=bool(user["show_extra_stats"]),
        duration_seconds=duration_seconds,
    )
    highlights = formatting.build_exercise_highlights(highlight_groups)
    full_text = summary
    tonnage_line = formatting.format_tonnage_equivalent(session_tonnage, seed=workout_id)
    if tonnage_line:
        full_text += f"\n\n{tonnage_line}"
    # Backfilled/imported past workouts shouldn't fire the "Nth workout" milestone —
    # they're entered out of order, so the running count isn't meaningful for them.
    if not is_backfill:
        total_finished = await db.count_workouts(user_id)
        if analytics.is_workout_milestone(total_finished):
            full_text += "\n\n" + formatting.format_milestone_line(total_finished)

    new_badges = await _evaluate_achievements(user_id, workout_id, started_at, duration_seconds)
    achievement_line = formatting.format_new_achievements(new_badges)
    if achievement_line:
        full_text += "\n\n" + achievement_line

    if highlights:
        full_text += f"\n\n{formatting.DIVIDER}\n\n{highlights}"
    if is_backfill:
        full_text = "✅ Сохранено как прошлая тренировка\n\n" + full_text

    # Existing comment (already generated, e.g. from a backfilled workout) shows right
    # away; a fresh one is generated in the background so finishing a workout doesn't
    # block on the LLM call — see _attach_ai_comment below.
    existing_comment = workout["ai_comment"]
    if existing_comment:
        full_text += "\n\n" + formatting.build_ai_comment_block(existing_comment)
    needs_ai_comment = (
        existing_comment is None and bool(user["ai_comments_enabled"]) and ai_trainer.is_configured()
    )
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

    await state.clear()
    active = await db.get_active_workout(user_id)
    menu_text, menu_png = await _menu_view(user_id)
    menu_kb = await _main_menu_kb(user_id, active)
    if menu_png is None:
        await bot.send_message(
            chat_id=data["live_chat_id"], text=menu_text, reply_markup=menu_kb, parse_mode="HTML"
        )
    else:
        await bot.send_photo(
            chat_id=data["live_chat_id"], photo=BufferedInputFile(menu_png, filename="year.png"),
            caption=menu_text, reply_markup=menu_kb, parse_mode="HTML",
        )
