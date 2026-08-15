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
import timeutil
import ui
from fsm import BackfillFlow, WorkoutFlow
from parser import ParseError, parse_ru_date
from state_scaffold import clear_state_keep_ai

router = Router(name="backfill")

# Guards date-picking against a double tap (calendar cell / "Сегодня"/"Вчера" /
# typed date): _date_chosen creates a workout row before the flow moves state
# past BackfillFlow.awaiting_date, so two callbacks racing on the same tap
# would each create their own db.create_workout — one of them winning the
# state.update_data write and the other left as an orphaned "backfill"-status
# workout nobody ever finishes. Same shape as handlers.workout._confirming.
_picking: set[int] = set()


def _try_claim_picking(user_id: int) -> bool:
    """Atomically check-and-reserve `_picking` for this user — no `await`
    between the membership check and the `.add()`, same reasoning as
    ai_trainer._try_claim_busy."""
    if user_id in _picking:
        return False
    _picking.add(user_id)
    return True


_BACKFILL_PROMPT = "📅 На какую дату занести тренировку?\nВыбери в календаре или напиши дату в формате дд.мм.гггг:"


@router.callback_query(F.data == "menu:backfill_workout")
async def backfill_start(callback: CallbackQuery, state: FSMContext):
    # Сброс перед новым потоком, но переписка с AI-тренером и черновик его
    # программы переживают тренировки — их не трогаем.
    await clear_state_keep_ai(state)
    await state.set_state(BackfillFlow.awaiting_date)
    today = timeutil.user_today(await db.get_user(callback.from_user.id))
    await ui.safe_edit(
        callback,
        _BACKFILL_PROMPT,
        reply_markup=keyboards.calendar_keyboard("bf", today.year, today.month, today=today),
    )
    await callback.answer()


@router.callback_query(StateFilter(BackfillFlow.awaiting_date), F.data.startswith("bf:cal:"))
async def bf_cal_nav(callback: CallbackQuery, state: FSMContext):
    year, month = (int(x) for x in callback.data.split(":")[2].split("-"))
    today = timeutil.user_today(await db.get_user(callback.from_user.id))
    with suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(
            reply_markup=keyboards.calendar_keyboard("bf", year, month, today=today)
        )
    await callback.answer()


@router.callback_query(F.data == "bf:noop")
async def bf_noop(callback: CallbackQuery):
    await callback.answer()


async def _date_chosen(event, state: FSMContext, date: dt.date):
    """Open the exact same exercise picker / set-logging flow as a live workout, dated in the past.

    The greeting is sent, then the workout row is created and wired into
    state, *before* the old calendar prompt is touched at all — its cleanup
    is left to `_picker_screen_groups`'s own `ui.safe_edit`, which already
    deletes it under `suppress(TelegramBadRequest)`. That ordering means a
    stale/already-gone prompt can never abort the flow partway through and
    leave the just-created "backfill" workout with nothing in state pointing
    at it — the previous order deleted the prompt (unsuppressed) first, so a
    failure there orphaned the row.
    """
    from handlers.workout import _picker_screen_groups

    greeting = f"🏋️ Тренировка — {formatting.format_date_ru(date)}"
    if isinstance(event, CallbackQuery):
        sent = await event.message.answer(greeting)
    else:
        sent = await event.answer(greeting)

    started_at = f"{date.isoformat()}T12:00:00"
    workout_id = await db.create_workout(event.from_user.id, started_at=started_at, status="backfill")
    await state.update_data(
        workout_id=workout_id, live_chat_id=sent.chat.id, live_message_id=sent.message_id,
        last_by_exercise={}, is_backfill=True, bf_date=date.isoformat(),
    )
    await state.set_state(WorkoutFlow.picking_group)
    await _picker_screen_groups(event, state)


@router.callback_query(StateFilter(BackfillFlow.awaiting_date), F.data.startswith("bf:date:"))
async def bf_date_quick(callback: CallbackQuery, state: FSMContext):
    """Claims `_picking` before the first `await`: two fast taps on the same
    calendar cell (or "Сегодня"/"Вчера") both pass StateFilter before either
    moves the state past awaiting_date, so without this guard both would call
    _date_chosen and create their own backfill workout."""
    user_id = callback.from_user.id
    if not _try_claim_picking(user_id):
        await callback.answer()
        return
    try:
        date = dt.date.fromisoformat(callback.data.split(":", 2)[2])
        await _date_chosen(callback, state, date)
        await callback.answer()
    finally:
        _picking.discard(user_id)


@router.message(StateFilter(BackfillFlow.awaiting_date), F.text)
async def bf_date_text(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not _try_claim_picking(user_id):
        return
    try:
        try:
            date = parse_ru_date(
                message.text, today=timeutil.user_today(await db.get_user(user_id))
            )
        except ParseError as e:
            await ui.reply_transient(message, e.message)
            return
        await _date_chosen(message, state, date)
    finally:
        _picking.discard(user_id)


@router.callback_query(StateFilter(BackfillFlow.awaiting_date), F.data == "bf:cancel")
async def bf_cancel(callback: CallbackQuery, state: FSMContext):
    # Отмена задним числом не отменяет переписку с AI-тренером и черновик его
    # программы — сохраняем их.
    await clear_state_keep_ai(state)
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer("Отменил")
