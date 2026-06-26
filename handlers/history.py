"""History browsing (§8) and progress/analytics screens (§7)."""

import datetime as dt

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import analytics
import charts
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
    blocks = await view_builder.build_block_views(workout_id, user["e1rm_formula"])
    started = dt.datetime.fromisoformat(workout["started_at"])
    text = formatting.build_workout_summary(
        started, blocks, workout["note"],
        hide_warmups=bool(user["hide_warmups"]), show_extra_stats=bool(user["show_extra_stats"]),
    )
    await ui.safe_edit(
        callback, text, reply_markup=keyboards.history_item_keyboard(workout_id), parse_mode="HTML"
    )
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
        started, blocks, workout["note"], hide_warmups=bool(user["hide_warmups"]), unit=user["unit"]
    )
    png = charts.render_workout_card(title, body, footer, note)
    await callback.message.answer_photo(
        BufferedInputFile(png, filename="workout.png"),
        caption="Готово — можно переслать друзьям 💪",
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


@router.callback_query(F.data.startswith("prog:grp:"))
async def prog_pick_group(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.split(":")[2]
    group_id = None if raw == "all" else int(raw)
    exercises = (
        await db.list_user_exercises(callback.from_user.id)
        if group_id is None
        else await db.list_user_exercises_in_group(callback.from_user.id, group_id)
    )
    b = InlineKeyboardBuilder()
    for ex in exercises:
        b.button(text=ex["display_name"], callback_data=f"prog:ex:{ex['id']}")
    b.button(text="⬅️ Назад", callback_data="prog:groups")
    b.adjust(1)
    text = "📈 Прогресс — выбери упражнение:" if exercises else "Пока нет своих упражнений с историей в этой группе."
    await ui.safe_edit(callback, text, reply_markup=b.as_markup())
    await callback.answer()


async def _load_sessions(exercise_id: int, formula: str) -> list[analytics.SessionStats]:
    rows = await db.list_sets_for_exercise(exercise_id)
    set_rows = [
        analytics.SetRow(r["weight"], r["reps"], bool(r["is_warmup"]), r["workout_id"], r["started_at"])
        for r in rows
    ]
    sessions = analytics.group_sets_by_session(set_rows)
    for s in sessions:
        s.formula = formula
    return sessions


@router.callback_query(F.data.startswith("prog:ex:"))
async def prog_show_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    await state.update_data(prog_exercise_id=ex_id)
    ex = await db.get_exercise(ex_id)
    user = await db.get_user(callback.from_user.id)
    sessions = await _load_sessions(ex_id, user["e1rm_formula"])

    trend = None
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
        ex["display_name"], sessions, trend, comparison, records, unit=user["unit"]
    )
    kb = keyboards.progress_back_keyboard()

    if sessions:
        is_bw = sessions[-1].is_bodyweight_mode
        metric = "повторы" if is_bw else "e1RM"
        points = [
            (dt.datetime.fromisoformat(s.started_at), float(s.max_reps_in_set if is_bw else s.top_e1rm))
            for s in sessions
        ]
        png = charts.render_metric_over_sessions(points, f"{ex['display_name']} — {metric}", metric)
        await callback.message.answer_photo(
            BufferedInputFile(png, filename="chart.png"),
            caption=text,
            reply_markup=kb,
            parse_mode="HTML",
        )
    else:
        await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()
