"""Дневник веса — log body weight over time and view the trend."""

import datetime as dt

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import charts
import db
import keyboards
from fsm import BodyweightFlow

router = Router(name="bodyweight")


async def _menu_text(user_id: int) -> str:
    latest = await db.get_latest_bodyweight(user_id)
    if latest:
        d = dt.date.fromisoformat(latest["date"])
        return f"⚖️ Дневник веса\nПоследняя запись: {latest['weight']} кг ({d.strftime('%d.%m.%Y')})"
    return "⚖️ Дневник веса\nПока нет записей."


@router.callback_query(F.data == "menu:bodyweight")
async def bodyweight_menu(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BodyweightFlow.menu)
    text = await _menu_text(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboards.bodyweight_keyboard())
    await callback.answer()


@router.callback_query(F.data == "bw:back")
async def bodyweight_back(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == "bw:add")
async def bodyweight_add_prompt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BodyweightFlow.awaiting_entry)
    await callback.message.edit_text(
        "Напиши свой текущий вес в кг (например 82.5):",
        reply_markup=keyboards.cancel_keyboard("bw:cancel"),
    )
    await callback.answer()


@router.callback_query(F.data == "bw:cancel")
async def bodyweight_cancel(callback: CallbackQuery, state: FSMContext):
    await bodyweight_menu(callback, state)


@router.message(StateFilter(BodyweightFlow.awaiting_entry))
async def bodyweight_entry_text(message: Message, state: FSMContext):
    try:
        value = float(message.text.strip().replace(",", "."))
        if value <= 0:
            raise ValueError
    except ValueError:
        await message.reply("Нужно положительное число, например 82.5")
        return
    today = dt.date.today().isoformat()
    await db.add_bodyweight_entry(message.from_user.id, today, value)
    await state.set_state(BodyweightFlow.menu)
    text = await _menu_text(message.from_user.id)
    await message.answer(text, reply_markup=keyboards.bodyweight_keyboard())


@router.callback_query(F.data == "bw:chart")
async def bodyweight_chart(callback: CallbackQuery, state: FSMContext):
    entries = await db.list_bodyweight_entries(callback.from_user.id)
    if len(entries) < 2:
        await callback.answer("Нужно хотя бы 2 записи для графика", show_alert=True)
        return
    points = [
        (dt.datetime.combine(dt.date.fromisoformat(e["date"]), dt.time()), e["weight"])
        for e in entries
    ]
    png = charts.render_metric_over_sessions(points, "Вес тела", "кг")
    await callback.message.answer_photo(BufferedInputFile(png, filename="bodyweight.png"))
    await callback.answer()
