"""User settings: units, weight step, bodyweight, e1RM formula, CSV export."""

import csv
import io

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import db
import keyboards
from fsm import SettingsFlow

router = Router(name="settings")


async def show_settings(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsFlow.menu)
    user = await db.get_user(callback.from_user.id)
    kb = keyboards.settings_keyboard(
        user["unit"], user["weight_step"], bool(user["hide_warmups"]), user["e1rm_formula"]
    )
    await callback.message.edit_text("🔧 Настройки:", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "settings:back")
async def settings_back(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)


@router.callback_query(F.data == "settings:unit")
async def settings_unit(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Сейчас поддерживаются только кг — фунты добавим позже", show_alert=True)


@router.callback_query(F.data == "settings:hide_warmups")
async def settings_hide_warmups(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await db.update_user(callback.from_user.id, hide_warmups=int(not user["hide_warmups"]))
    await show_settings(callback, state)


@router.callback_query(F.data == "settings:formula")
async def settings_formula(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    new_formula = "brzycki" if user["e1rm_formula"] == "epley" else "epley"
    await db.update_user(callback.from_user.id, e1rm_formula=new_formula)
    await show_settings(callback, state)


@router.callback_query(F.data == "settings:step")
async def settings_step(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsFlow.awaiting_weight_step)
    await callback.message.edit_text("Введи шаг веса в кг (например 2.5):")
    await callback.answer()


@router.message(StateFilter(SettingsFlow.awaiting_weight_step))
async def settings_step_entered(message: Message, state: FSMContext):
    try:
        value = float(message.text.strip().replace(",", "."))
        if value <= 0:
            raise ValueError
    except ValueError:
        await message.reply("Нужно положительное число, например 2.5")
        return
    await db.update_user(message.from_user.id, weight_step=value)
    await state.set_state(SettingsFlow.menu)
    user = await db.get_user(message.from_user.id)
    kb = keyboards.settings_keyboard(
        user["unit"], user["weight_step"], bool(user["hide_warmups"]), user["e1rm_formula"]
    )
    await message.answer("🔧 Настройки:", reply_markup=kb)


@router.callback_query(F.data == "settings:bodyweight")
async def settings_bodyweight(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsFlow.awaiting_bodyweight)
    await callback.message.edit_text("Введи свой вес тела в кг:")
    await callback.answer()


@router.message(StateFilter(SettingsFlow.awaiting_bodyweight))
async def settings_bodyweight_entered(message: Message, state: FSMContext):
    try:
        value = float(message.text.strip().replace(",", "."))
        if value <= 0:
            raise ValueError
    except ValueError:
        await message.reply("Нужно положительное число, например 82.5")
        return
    await db.update_user(message.from_user.id, bodyweight=value)
    await state.set_state(SettingsFlow.menu)
    user = await db.get_user(message.from_user.id)
    kb = keyboards.settings_keyboard(
        user["unit"], user["weight_step"], bool(user["hide_warmups"]), user["e1rm_formula"]
    )
    await message.answer("🔧 Настройки:", reply_markup=kb)


@router.callback_query(F.data == "settings:export")
async def settings_export(callback: CallbackQuery, state: FSMContext):
    rows = await db.export_rows_for_user(callback.from_user.id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "started_at", "finished_at", "exercise", "block_type",
        "round_index", "order_in_round", "weight", "reps", "is_warmup", "rpe",
    ])
    for r in rows:
        writer.writerow([
            r["started_at"], r["finished_at"], r["exercise"], r["block_type"],
            r["round_index"], r["order_in_round"], r["weight"], r["reps"], r["is_warmup"], r["rpe"],
        ])
    data = buf.getvalue().encode("utf-8-sig")
    await callback.message.answer_document(
        BufferedInputFile(data, filename="training_log.csv"), caption="Экспорт истории тренировок"
    )
    await callback.answer()
