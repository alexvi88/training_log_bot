"""History browsing (§8) and progress/analytics screens (§7)."""

import asyncio
import datetime as dt
from html import escape

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InputMediaPhoto, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ai_trainer
import analytics
import charts
import config
import db
import formatting
import keyboards
import ui
import view_builder
from fsm import HistoryFlow, ProgressFlow

router = Router(name="history")

HISTORY_PAGE_SIZE = 8


# ---------- history ----------

async def show_history_list(callback: CallbackQuery, state: FSMContext, page: int):
    await state.set_state(HistoryFlow.browsing)
    await state.update_data(history_page=page)
    user_id = callback.from_user.id
    total = await db.count_workouts(user_id)
    workouts = await db.list_workouts(user_id, limit=HISTORY_PAGE_SIZE, offset=page * HISTORY_PAGE_SIZE)
    items = []
    for w in workouts:
        started = dt.datetime.fromisoformat(w["started_at"])
        items.append({"id": w["id"], "label": formatting.format_date_ru(started)})
    has_next = (page + 1) * HISTORY_PAGE_SIZE < total
    kb = keyboards.history_list_keyboard(items, page, has_next)
    text = "📚 История тренировок:" if items else "Пока нет завершённых тренировок."
    await ui.safe_edit(callback, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("hist:page:"))
async def hist_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[2])
    await show_history_list(callback, state, page)


@router.callback_query(F.data == "hist:back")
async def hist_back(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await show_history_list(callback, state, data.get("history_page", 0))


@router.callback_query(F.data == "hist:menu")
async def hist_to_menu(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer()


async def _top_lifts(user_id: int, formula: str) -> list[tuple[str, float, int, float]]:
    """Best working set per exercise, strongest first — for the Hall of Fame.

    Every exercise the user has ever logged gets a line, including bodyweight
    ones: those have no load to rank by, so their record is the best set of reps
    and they follow the weighted lifts (weight 0 marks them for the formatter).
    The list isn't capped here — build_hall_of_fame folds it and trims whatever
    doesn't fit the message.
    """
    weighted: list[tuple[str, float, int, float]] = []
    bodyweight: list[tuple[str, float, int, float]] = []
    for ex in await db.list_user_exercises(user_id):
        rows = await db.list_sets_for_exercise(ex["id"])
        if not rows:
            continue
        set_rows = [analytics.SetRow(r["weight"], r["reps"], r["workout_id"], r["started_at"]) for r in rows]
        sessions = analytics.group_sets_by_session(set_rows)
        for s in sessions:
            s.formula = formula
        pr = analytics.compute_personal_records(sessions)
        if pr.max_e1rm > 0 and pr.best_e1rm_weight > 0:
            weighted.append((ex["display_name"], pr.best_e1rm_weight, pr.best_e1rm_reps, pr.max_e1rm))
        elif pr.max_reps_at_weight:
            best_reps = max(pr.max_reps_at_weight.values())
            bodyweight.append((ex["display_name"], 0.0, best_reps, 0.0))
    weighted.sort(key=lambda t: t[3], reverse=True)
    bodyweight.sort(key=lambda t: t[2], reverse=True)
    return weighted + bodyweight


async def build_hall_of_fame_text(user_id: int, max_chars: int | None = None) -> str:
    user = await db.get_user(user_id)
    formula = user["e1rm_formula"] if user else config.DEFAULT_E1RM_FORMULA
    unit = user["unit"] if user else "kg"
    total_workouts = await db.count_workouts(user_id)
    agg = await db.hall_of_fame_aggregates(user_id)
    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id)]
    best_streak = analytics.max_week_streak(dates)
    top = await _top_lifts(user_id, formula)
    equivalent = formatting.format_tonnage_equivalent(agg["tonnage"], seed=user_id)
    return formatting.build_hall_of_fame(
        total_workouts=total_workouts,
        tonnage_kg=agg["tonnage"],
        tonnage_equivalent=equivalent,
        best_week_streak=best_streak,
        longest_workout_seconds=agg["longest_workout_seconds"],
        top_lifts=top,
        unit=unit,
        max_chars=max_chars,
    )


@router.callback_query(F.data == "menu:achievements")
async def menu_achievements(callback: CallbackQuery, state: FSMContext):
    """'🏆 Достижения' — reached from the Progress entry screen. Combines the
    old standalone Hall of Fame screen (lifetime totals, personal records)
    with the badge grid into one screen."""
    await state.clear()
    earned = await db.list_achievement_codes(callback.from_user.id)
    ach_text = formatting.build_achievements_screen(earned)
    # Records and badges share one message, and the record list is the open-ended
    # half (one line per exercise ever logged), so it gets whatever room the fixed
    # badge grid leaves — otherwise a long-time user's screen overflows the 4096
    # cap and ui.safe_edit deletes it without putting anything back.
    budget = formatting.MESSAGE_LIMIT - formatting.telegram_length(ach_text) - 2
    hof_text = await build_hall_of_fame_text(callback.from_user.id, max_chars=budget)
    text = hof_text + "\n\n" + ach_text
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="prog:groups")
    kb.adjust(1)
    await ui.safe_edit(callback, text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()


async def show_history_item(callback: CallbackQuery, workout_id: int) -> bool:
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return False
    user = await db.get_user(callback.from_user.id)
    blocks = await view_builder.build_block_views(
        workout_id, user["e1rm_formula"], previous_before=workout["started_at"]
    )
    started = dt.datetime.fromisoformat(workout["started_at"])
    duration_seconds = await view_builder.workout_duration_seconds(workout)
    text = formatting.build_workout_summary(
        started, blocks, workout["note"], show_extra_stats=bool(user["show_extra_stats"]),
        italic_prev=True, duration_seconds=duration_seconds,
    )
    comment = await ai_trainer.ensure_workout_comment(user, workout_id)
    if comment:
        text += "\n\n" + formatting.build_ai_comment_block(comment)
    kb = keyboards.history_item_keyboard(
        workout_id, show_ai_button=comment is None and ai_trainer.is_configured()
    )
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    return True


@router.callback_query(F.data.startswith("hist:item:"))
async def hist_item(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[2])
    if await show_history_item(callback, workout_id):
        await callback.answer()


@router.callback_query(F.data.startswith("hist:card:"))
async def hist_card(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[2])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    user = await db.get_user(callback.from_user.id)
    blocks = await view_builder.build_block_views(workout_id, user["e1rm_formula"])
    started = dt.datetime.fromisoformat(workout["started_at"])
    title, body, footer, note = formatting.build_workout_card(
        started, blocks, workout["note"], unit=user["unit"]
    )
    png = await asyncio.to_thread(charts.render_workout_card, title, body, footer, note)
    await callback.message.answer_photo(
        BufferedInputFile(png, filename="workout.png"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("hist:edit:"))
async def hist_edit(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[2])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    from handlers.edit_workout import show_edit_screen
    await show_edit_screen(callback, state, workout_id)
    await callback.answer()


@router.callback_query(F.data.startswith("hist:del:"))
async def hist_delete_confirm(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[2])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    kb = keyboards.yes_no_keyboard(
        yes_cb=f"hist:delyes:{workout_id}",
        no_cb=f"hist:item:{workout_id}",
        yes_text="🗑 Удалить",
        no_text="❌ Отмена",
    )
    await ui.safe_edit(callback, "Удалить эту тренировку? Это действие нельзя отменить.", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("hist:delyes:"))
async def hist_delete(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[2])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    await db.discard_workout(workout_id)
    data = await state.get_data()
    await show_history_list(callback, state, data.get("history_page", 0))
    await callback.answer("Тренировка удалена.")


# ---------- progress ----------

async def show_progress_entry(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProgressFlow.picking_group)
    groups = await db.list_muscle_groups(callback.from_user.id)
    kb = keyboards.groups_keyboard(
        groups, prefix="prog",
        extra_buttons=[("🏆 Достижения", "menu:achievements"), ("⬅️ Назад", "prog:back")],
        show_all=True,
    )
    text = "📈 Прогресс — выбери группу мышц или напиши название упражнения для поиска:"
    await ui.safe_edit(callback, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "prog:back")
async def prog_back(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)


@router.callback_query(F.data == "prog:groups")
async def prog_back_to_groups(callback: CallbackQuery, state: FSMContext):
    await show_progress_entry(callback, state)


async def _render_progress_exercise_list(callback: CallbackQuery, state: FSMContext, raw: str, page: int) -> None:
    await state.set_state(ProgressFlow.picking_exercise)
    group_id = None if raw == "all" else int(raw)
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

    b = InlineKeyboardBuilder()
    for ex in exercises:
        b.row(InlineKeyboardButton(text=ex["display_name"], callback_data=f"prog:ex:{ex['id']}:{raw}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"prog:gpage:{raw}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"prog:gpage:{raw}:{page + 1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="prog:groups"))
    if exercises:
        text = "📈 Прогресс — выбери упражнение или напиши название для поиска:"
    else:
        text = "Пока нет своих упражнений с историей в этой группе. Можно написать название для поиска."
    await ui.safe_edit(callback, text, reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("prog:grp:"))
async def prog_pick_group(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.split(":")[2]
    await _render_progress_exercise_list(callback, state, raw, page=0)
    await callback.answer()


@router.callback_query(F.data.startswith("prog:gpage:"))
async def prog_group_page(callback: CallbackQuery, state: FSMContext):
    _, _, raw, page_str = callback.data.split(":")
    await _render_progress_exercise_list(callback, state, raw, page=int(page_str))
    await callback.answer()


@router.message(StateFilter(ProgressFlow.picking_group, ProgressFlow.picking_exercise))
async def prog_search_text(message: Message, state: FSMContext):
    """Typing while browsing Progress searches the user's own exercises instead
    of falling through to the fallback router's "Не понял" — same pattern as
    the workout picker and ⚙️ Упражнения, minus template suggestions: Progress
    is about history you already have, and a never-trained template would just
    show "Пока нет завершённых тренировок с этим упражнением" if picked."""
    query = message.text.strip()
    if not query:
        return
    results = await db.search_exercises(message.from_user.id, query)
    b = InlineKeyboardBuilder()
    for ex in results:
        b.row(InlineKeyboardButton(text=ex["display_name"], callback_data=f"prog:ex:{ex['id']}:all"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="prog:groups"))
    text = f"Результаты поиска «{escape(query)}»:" if results else f"Ничего не нашлось по «{escape(query)}»."
    await state.set_state(ProgressFlow.picking_exercise)
    await message.answer(text, reply_markup=b.as_markup(), parse_mode="HTML")


async def _load_sessions(exercise_id: int, formula: str) -> list[analytics.SessionStats]:
    rows = await db.list_sets_for_exercise(exercise_id)
    set_rows = [
        analytics.SetRow(r["weight"], r["reps"], r["workout_id"], r["started_at"], r["rpe"])
        for r in rows
    ]
    sessions = analytics.group_sets_by_session(set_rows)
    for s in sessions:
        s.formula = formula
    return sessions


async def _render_progress_view(ex_id: int, user, limit: int, origin: str = "all"):
    """Build the text/chart/keyboard for an exercise's progress screen.

    Trend/comparison/PRs always look at the full history; `limit` only
    controls how many recent sessions are shown in the text list and plotted
    on the chart, so switching periods doesn't change what counts as a record.
    """
    ex = await db.get_exercise(ex_id)
    sessions = await _load_sessions(ex_id, user["e1rm_formula"])

    points: list[tuple[dt.datetime, float]] = []
    if sessions:
        is_bw = sessions[-1].is_bodyweight_mode
        points = [
            (dt.datetime.fromisoformat(s.started_at), float(s.max_reps_in_set if is_bw else s.top_e1rm))
            for s in sessions
        ]
    comparison = analytics.compare_to_previous_session(sessions)
    records = analytics.compute_personal_records(sessions)

    text = formatting.format_progress_screen(
        ex["display_name"], sessions, comparison, records, limit=limit, unit=user["unit"]
    )

    png = None
    if sessions:
        metric = "повторы" if sessions[-1].is_bodyweight_mode else "e1RM"
        png = await asyncio.to_thread(
            charts.render_metric_over_sessions,
            points[-limit:],
            f"{ex['display_name']} — {metric}",
            metric,
            show_weekly_rate=False,
        )

    kb = (
        keyboards.progress_chart_keyboard(ex_id, limit, origin)
        if sessions
        else keyboards.progress_back_keyboard(ex_id, origin)
    )
    return text, png, kb


@router.callback_query(F.data.startswith("prog:ex:"))
async def prog_show_exercise(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    ex_id = int(parts[2])
    origin = parts[3] if len(parts) > 3 else "all"
    await state.update_data(prog_exercise_id=ex_id, prog_origin=origin)
    user = await db.get_user(callback.from_user.id)
    text, png, kb = await _render_progress_view(ex_id, user, keyboards.DEFAULT_PROGRESS_LIMIT, origin)

    if png:
        await ui.safe_edit_photo(callback, png, "chart.png", text, reply_markup=kb, parse_mode="HTML")
    else:
        await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("prog:per:"))
async def prog_change_period(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    ex_id, limit = int(parts[2]), int(parts[3])
    origin = parts[4] if len(parts) > 4 else "all"
    user = await db.get_user(callback.from_user.id)
    text, png, kb = await _render_progress_view(ex_id, user, limit, origin)

    if png:
        media = InputMediaPhoto(
            media=BufferedInputFile(png, filename="chart.png"), caption=text, parse_mode="HTML"
        )
        await callback.message.edit_media(media, reply_markup=kb)
    else:
        await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()
