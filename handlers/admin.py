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
import push_texts
import ui
import view_builder
from fsm import AdminFlow

router = Router(name="admin")

USERS_PAGE_SIZE = 10
HISTORY_PAGE_SIZE = 8
PUSHES_PAGE_SIZE = 10
AI_DIALOGS_TG_CHUNK = 4000


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


@router.message(Command("check_users"))
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


@router.callback_query(F.data == "admin:menu")
async def admin_to_menu(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
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
    duration_seconds = await view_builder.workout_duration_seconds(workout)
    text = formatting.build_workout_summary(
        started, blocks, workout["note"], show_extra_stats=bool(user["show_extra_stats"]),
        italic_prev=True, duration_seconds=duration_seconds,
    )
    await ui.safe_edit(
        callback, text, reply_markup=keyboards.admin_history_item_keyboard(target_user_id), parse_mode="HTML"
    )
    await callback.answer()


async def _show_pushes_list(target: Message | CallbackQuery, state: FSMContext, page: int):
    await state.set_state(AdminFlow.browsing_pushes)
    await state.update_data(admin_pushes_page=page)
    total = await db.count_pushes()
    pushes = await db.list_recent_pushes(limit=PUSHES_PAGE_SIZE, offset=page * PUSHES_PAGE_SIZE)
    has_next = (page + 1) * PUSHES_PAGE_SIZE < total

    if pushes:
        entries = []
        for p in pushes:
            sent = dt.datetime.fromisoformat(p["sent_at"])
            who = f"@{p['username']}" if p["username"] else str(p["telegram_id"])
            category = push_texts.CATEGORY_LABELS.get(p["category"], p["category"])
            entries.append(f"{sent.strftime('%d.%m %H:%M')} · {who} · {category}\n«{p['text']}»")
        text = f"📬 Пуши ({total}), последние сверху:\n\n" + "\n\n".join(entries)
    else:
        text = "Пушей пока не было."

    kb = keyboards.admin_pushes_keyboard(page, has_next)
    if isinstance(target, CallbackQuery):
        await ui.safe_edit(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.message(Command("pushes"))
async def cmd_pushes(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await _show_pushes_list(message, state, page=0)


@router.callback_query(F.data.startswith("admin:pp:"))
async def admin_pushes_page(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    page = int(callback.data.split(":")[2])
    await _show_pushes_list(callback, state, page)
    await callback.answer()


async def _show_ai_users_list(target: Message | CallbackQuery, state: FSMContext, page: int):
    await state.set_state(AdminFlow.browsing_ai_users)
    await state.update_data(admin_ai_users_page=page)
    total = await db.count_users()
    users = await db.list_users_with_ai_message_counts(limit=USERS_PAGE_SIZE, offset=page * USERS_PAGE_SIZE)
    has_next = (page + 1) * USERS_PAGE_SIZE < total
    kb = keyboards.admin_ai_users_keyboard(users, page, has_next)
    text = "🤖 Диалоги с AI-тренером — выберите пользователя:" if users else "Пользователей пока нет."
    if isinstance(target, CallbackQuery):
        await ui.safe_edit(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.message(Command("ai_dialogs"))
async def cmd_ai_dialogs(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await _show_ai_users_list(message, state, page=0)


@router.callback_query(F.data.startswith("admin:aip:"))
async def admin_ai_users_page(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    page = int(callback.data.split(":")[2])
    await _show_ai_users_list(callback, state, page)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:aib:"))
async def admin_ai_users_back(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    page = int(callback.data.split(":")[2])
    await _show_ai_users_list(callback, state, page)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:aiu:"))
async def admin_ai_dialogs_show(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    target_user_id = int(callback.data.split(":")[2])

    user = await db.get_user(target_user_id)
    rows = await db.get_ai_chat_history(target_user_id) if user else []
    if not rows:
        await callback.answer("У этого пользователя пока нет диалогов с AI-тренером.", show_alert=True)
        return

    data = await state.get_data()
    page = data.get("admin_ai_users_page", 0)

    who = f"@{user['username']}" if user["username"] else str(target_user_id)
    lines = [f"🤖 Диалоги с AI-тренером — {who} ({len(rows)} сообщ.):", ""]
    for row in rows:
        sent = dt.datetime.fromisoformat(row["created_at"])
        speaker = "👤 Юзер" if row["role"] == "user" else "🤖 AI"
        lines.append(f"{sent.strftime('%d.%m %H:%M')} · {speaker}:\n{row['content']}")
    text = "\n\n".join(lines)

    chunks = [text[i : i + AI_DIALOGS_TG_CHUNK] for i in range(0, len(text), AI_DIALOGS_TG_CHUNK)]
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        markup = keyboards.admin_ai_dialogs_back_keyboard(page) if is_last else None
        await callback.message.answer(chunk, reply_markup=markup)
    await callback.answer()
