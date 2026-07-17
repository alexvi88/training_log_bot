"""History browsing (§8) and progress/analytics screens (§7)."""

import asyncio
import datetime as dt

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InputMediaPhoto
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
from fsm import HistoryFlow

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
    groups = await db.list_muscle_groups(callback.from_user.id)
    kb = keyboards.groups_keyboard(
        groups, prefix="prog", extra_buttons=[("⬅️ Назад", "prog:back")], show_all=True
    )
    await ui.safe_edit(callback, "📈 Прогресс — выбери группу мышц:", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "prog:back")
async def prog_back(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)


@router.callback_query(F.data == "prog:groups")
async def prog_back_to_groups(callback: CallbackQuery, state: FSMContext):
    await show_progress_entry(callback, state)


async def _render_progress_exercise_list(callback: CallbackQuery, raw: str, page: int) -> None:
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
    text = "📈 Прогресс — выбери упражнение:" if exercises else "Пока нет своих упражнений с историей в этой группе."
    await ui.safe_edit(callback, text, reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("prog:grp:"))
async def prog_pick_group(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.split(":")[2]
    await _render_progress_exercise_list(callback, raw, page=0)
    await callback.answer()


@router.callback_query(F.data.startswith("prog:gpage:"))
async def prog_group_page(callback: CallbackQuery, state: FSMContext):
    _, _, raw, page_str = callback.data.split(":")
    await _render_progress_exercise_list(callback, raw, page=int(page_str))
    await callback.answer()


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

    trend = None
    points: list[tuple[dt.datetime, float]] = []
    if sessions:
        is_bw = sessions[-1].is_bodyweight_mode
        points = [
            (dt.datetime.fromisoformat(s.started_at), float(s.max_reps_in_set if is_bw else s.top_e1rm))
            for s in sessions
        ]
        trend = analytics.linear_trend(points)
    comparison = analytics.compare_to_previous_session(sessions)
    records = analytics.compute_personal_records(sessions)

    text = formatting.format_progress_screen(
        ex["display_name"], sessions, trend, comparison, records, limit=limit, unit=user["unit"]
    )

    png = None
    if sessions:
        metric = "повторы" if sessions[-1].is_bodyweight_mode else "e1RM"
        png = await asyncio.to_thread(
            charts.render_metric_over_sessions, points[-limit:], f"{ex['display_name']} — {metric}", metric
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
