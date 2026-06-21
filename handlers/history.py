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
    await callback.message.edit_text(text, reply_markup=kb)
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


async def show_history_item(callback: CallbackQuery, workout_id: int):
    workout = await db.get_workout(workout_id)
    user = await db.get_user(callback.from_user.id)
    blocks = await view_builder.build_block_views(workout_id, user["e1rm_formula"])
    started = dt.datetime.fromisoformat(workout["started_at"])
    finished = dt.datetime.fromisoformat(workout["finished_at"]) if workout["finished_at"] else started
    text = formatting.build_workout_summary(
        started, finished, blocks, workout["note"],
        hide_warmups=bool(user["hide_warmups"]), show_extra_stats=bool(user["show_extra_stats"]),
    )
    await callback.message.edit_text(text, reply_markup=keyboards.history_item_keyboard(workout_id))


@router.callback_query(F.data.startswith("hist:item:"))
async def hist_item(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[2])
    await show_history_item(callback, workout_id)
    await callback.answer()


@router.callback_query(F.data.startswith("hist:edit:"))
async def hist_edit(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[2])
    from handlers.edit_workout import show_edit_screen
    await show_edit_screen(callback, state, workout_id)
    await callback.answer()


@router.callback_query(F.data.startswith("hist:dup:"))
async def hist_duplicate(callback: CallbackQuery, state: FSMContext):
    src_workout_id = int(callback.data.split(":")[2])
    user_id = callback.from_user.id
    active = await db.get_active_workout(user_id)
    if active:
        await callback.answer("У тебя уже есть активная тренировка — заверши её сначала.", show_alert=True)
        return

    planned_blocks = []
    for block in await db.list_blocks_for_workout(src_workout_id):
        block_exs = await db.get_block_exercises(block["id"])
        if not block_exs:
            continue
        planned_blocks.append(
            {"type": block["type"], "exercise_ids": [be["exercise_id"] for be in block_exs]}
        )

    new_workout_id = await db.create_workout(user_id)
    await state.update_data(planned_blocks=planned_blocks)

    from handlers.workout import _enter_live
    await _enter_live(callback, state, new_workout_id)


# ---------- progress ----------

async def show_progress_entry(callback: CallbackQuery, state: FSMContext):
    exercises = await db.list_user_exercises(callback.from_user.id)
    b = InlineKeyboardBuilder()
    for ex in exercises:
        b.button(text=ex["display_name"], callback_data=f"prog:ex:{ex['id']}")
    b.button(text="⬅️ Назад", callback_data="prog:back")
    b.adjust(1)
    text = "📈 Прогресс — выбери упражнение:" if exercises else "Пока нет своих упражнений с историей."
    await callback.message.edit_text(text, reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "prog:back")
async def prog_back(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)


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
    kb = keyboards.progress_charts_keyboard(ex_id) if sessions else None
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("chart:"))
async def progress_chart(callback: CallbackQuery, state: FSMContext):
    _, kind, ex_id_str = callback.data.split(":")
    ex_id = int(ex_id_str)
    ex = await db.get_exercise(ex_id)
    user = await db.get_user(callback.from_user.id)
    sessions = await _load_sessions(ex_id, user["e1rm_formula"])
    if not sessions:
        await callback.answer("Нет данных")
        return
    is_bw = sessions[-1].is_bodyweight_mode

    if kind == "e1rm":
        metric = "повторы" if is_bw else "e1RM"
        points = [
            (dt.datetime.fromisoformat(s.started_at), float(s.max_reps_in_set if is_bw else s.top_e1rm))
            for s in sessions
        ]
        png = charts.render_metric_over_sessions(points, f"{ex['display_name']} — {metric}", metric)
    elif kind == "tonnage":
        points = [(dt.datetime.fromisoformat(s.started_at), s.tonnage) for s in sessions]
        png = charts.render_metric_over_sessions(points, f"{ex['display_name']} — тоннаж", "тоннаж")
    else:
        scatter_points = [
            (dt.datetime.fromisoformat(s.started_at), st.weight, st.reps)
            for s in sessions for st in s.working_sets
        ]
        png = charts.render_scatter_sets(scatter_points, f"{ex['display_name']} — сеты")

    await callback.message.answer_photo(BufferedInputFile(png, filename="chart.png"))
    await callback.answer()
