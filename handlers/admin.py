"""Admin-only: browse other users' workout history (read-only)."""

import datetime as dt

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import db
import formatting
import keyboards
import ui
import view_builder
from fsm import AdminFlow

router = Router(name="admin")

USERS_PAGE_SIZE = 10
HISTORY_PAGE_SIZE = 8


def _is_admin(telegram_id: int) -> bool:
    return config.ADMIN_ID is not None and telegram_id == config.ADMIN_ID


async def _show_users_list(target: Message | CallbackQuery, state: FSMContext, page: int):
    await state.set_state(AdminFlow.browsing_users)
    await state.update_data(admin_users_page=page)
    total = await db.count_users()
    users = await db.list_users_with_workout_counts(limit=USERS_PAGE_SIZE, offset=page * USERS_PAGE_SIZE)
    has_next = (page + 1) * USERS_PAGE_SIZE < total
    kb = keyboards.admin_users_keyboard(users, page, has_next)
    text = "👥 Пользователи:" if users else "Пользователей пока нет."
    if isinstance(target, CallbackQuery):
        await ui.safe_edit(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await _show_users_list(message, state, page=0)


@router.callback_query(F.data.startswith("admin:up:"))
async def admin_users_page(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    page = int(callback.data.split(":")[2])
    await _show_users_list(callback, state, page)
    await callback.answer()


@router.callback_query(F.data == "admin:back")
async def admin_back_to_users(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    data = await state.get_data()
    await _show_users_list(callback, state, data.get("admin_users_page", 0))
    await callback.answer()


async def _show_history_list(callback: CallbackQuery, state: FSMContext, target_user_id: int, page: int):
    await state.set_state(AdminFlow.browsing_history)
    await state.update_data(admin_target_user=target_user_id, admin_history_page=page)
    total = await db.count_workouts(target_user_id)
    workouts = await db.list_workouts(target_user_id, limit=HISTORY_PAGE_SIZE, offset=page * HISTORY_PAGE_SIZE)
    items = []
    for w in workouts:
        started = dt.datetime.fromisoformat(w["started_at"])
        items.append({"id": w["id"], "label": formatting.format_date_ru(started)})
    has_next = (page + 1) * HISTORY_PAGE_SIZE < total
    kb = keyboards.admin_history_list_keyboard(items, target_user_id, page, has_next)
    text = "📚 История тренировок:" if items else "У этого пользователя пока нет завершённых тренировок."
    await ui.safe_edit(callback, text, reply_markup=kb)


@router.callback_query(F.data.startswith("admin:u:"))
async def admin_pick_user(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    target_user_id = int(callback.data.split(":")[2])
    await _show_history_list(callback, state, target_user_id, page=0)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:hp:"))
async def admin_history_page(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, _, target_raw, page_raw = callback.data.split(":")
    await _show_history_list(callback, state, int(target_raw), int(page_raw))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:hb:"))
async def admin_history_back(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    target_user_id = int(callback.data.split(":")[2])
    data = await state.get_data()
    await _show_history_list(callback, state, target_user_id, data.get("admin_history_page", 0))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:hi:"))
async def admin_history_item(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, _, target_raw, workout_raw = callback.data.split(":")
    target_user_id, workout_id = int(target_raw), int(workout_raw)
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != target_user_id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    user = await db.get_user(target_user_id)
    blocks = await view_builder.build_block_views(
        workout_id, user["e1rm_formula"], previous_before=workout["started_at"]
    )
    started = dt.datetime.fromisoformat(workout["started_at"])
    text = formatting.build_workout_summary(
        started, blocks, workout["note"], show_extra_stats=bool(user["show_extra_stats"]),
        italic_prev=True,
    )
    await ui.safe_edit(
        callback, text, reply_markup=keyboards.admin_history_item_keyboard(target_user_id), parse_mode="HTML"
    )
    await callback.answer()
