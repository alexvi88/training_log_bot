"""§A1 — manual backfill of a past workout: pick a date, then log it exactly like a live workout."""

import datetime as dt
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import db
import formatting
import keyboards
import ui
from fsm import BackfillFlow, WorkoutFlow
from parser import ParseError, parse_ru_date

router = Router(name="backfill")


_BACKFILL_PROMPT = "📅 На какую дату занести тренировку?\nВыбери в календаре или напиши дату в формате дд.мм.гггг:"


@router.callback_query(F.data == "menu:backfill_workout")
async def backfill_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(BackfillFlow.awaiting_date)
    today = dt.date.today()
    await ui.safe_edit(
        callback,
        _BACKFILL_PROMPT,
        reply_markup=keyboards.calendar_keyboard("bf", today.year, today.month),
    )
    await callback.answer()


@router.callback_query(StateFilter(BackfillFlow.awaiting_date), F.data.startswith("bf:cal:"))
async def bf_cal_nav(callback: CallbackQuery, state: FSMContext):
    year, month = (int(x) for x in callback.data.split(":")[2].split("-"))
    with suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(
            reply_markup=keyboards.calendar_keyboard("bf", year, month)
        )
    await callback.answer()


@router.callback_query(F.data == "bf:noop")
async def bf_noop(callback: CallbackQuery):
    await callback.answer()


async def _date_chosen(event, state: FSMContext, date: dt.date):
    """Open the exact same exercise picker / set-logging flow as a live workout, dated in the past."""
    from handlers.workout import _picker_screen_groups

    started_at = f"{date.isoformat()}T12:00:00"
    workout_id = await db.create_workout(event.from_user.id, started_at=started_at, status="backfill")
    greeting = f"🏋️ Тренировка — {formatting.format_date_ru(date)}"
    if isinstance(event, CallbackQuery):
        await event.message.delete()
        sent = await event.message.answer(greeting)
    else:
        sent = await event.answer(greeting)
    await state.update_data(
        workout_id=workout_id, live_chat_id=sent.chat.id, live_message_id=sent.message_id,
        last_by_exercise={}, is_backfill=True, bf_date=date.isoformat(),
    )
    await state.set_state(WorkoutFlow.picking_group)
    await _picker_screen_groups(event, state)


@router.callback_query(StateFilter(BackfillFlow.awaiting_date), F.data.startswith("bf:date:"))
async def bf_date_quick(callback: CallbackQuery, state: FSMContext):
    date = dt.date.fromisoformat(callback.data.split(":", 2)[2])
    await _date_chosen(callback, state, date)
    await callback.answer()


@router.message(StateFilter(BackfillFlow.awaiting_date))
async def bf_date_text(message: Message, state: FSMContext):
    try:
        date = parse_ru_date(message.text)
    except ParseError as e:
        await message.reply(e.message)
        return
    await _date_chosen(message, state, date)


@router.callback_query(StateFilter(BackfillFlow.awaiting_date), F.data == "bf:cancel")
async def bf_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer("Отменено")
