"""User settings: units, e1RM formula, CSV export."""

import csv
import io

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery

import db
import keyboards
import ui
from fsm import SettingsFlow

router = Router(name="settings")


async def show_settings(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsFlow.menu)
    user = await db.get_user(callback.from_user.id)
    kb = keyboards.settings_keyboard(
        user["unit"], user["e1rm_formula"], bool(user["pushes_enabled"]), bool(user["ai_comments_enabled"])
    )
    await ui.safe_edit(callback, "🔧 Настройки:", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "settings:back")
async def settings_back(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == "settings:unit")
async def settings_unit(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    new_unit = "lb" if user["unit"] == "kg" else "kg"
    await db.update_user(callback.from_user.id, unit=new_unit)
    await show_settings(callback, state)


@router.callback_query(F.data == "settings:formula")
async def settings_formula(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    new_formula = "brzycki" if user["e1rm_formula"] == "epley" else "epley"
    await db.update_user(callback.from_user.id, e1rm_formula=new_formula)
    await show_settings(callback, state)


@router.callback_query(F.data == "settings:pushes")
async def settings_pushes(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await db.update_user(callback.from_user.id, pushes_enabled=0 if user["pushes_enabled"] else 1)
    await show_settings(callback, state)


@router.callback_query(F.data == "settings:ai_comments")
async def settings_ai_comments(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await db.update_user(callback.from_user.id, ai_comments_enabled=0 if user["ai_comments_enabled"] else 1)
    await show_settings(callback, state)


@router.callback_query(F.data == "settings:export")
async def settings_export(callback: CallbackQuery, state: FSMContext):
    rows = await db.export_rows_for_user(callback.from_user.id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["started_at", "exercise", "round_index", "weight", "reps"])
    for r in rows:
        writer.writerow([
            r["started_at"], r["exercise"], r["round_index"], r["weight"], r["reps"],
        ])
    data = buf.getvalue().encode("utf-8-sig")
    await callback.message.answer_document(
        BufferedInputFile(data, filename="training_log.csv"), caption="Экспорт истории тренировок"
    )
    await callback.answer()
