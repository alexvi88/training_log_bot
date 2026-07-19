"""⚖️ Вес тела — a lightweight bodyweight log with a trend chart.

Only body weight is tracked (no other measurements). Entries are timestamped
and stored in the user's current unit; switching units rescales them (see
handlers/settings.py). The screen shows the latest value, change since the
previous/first entry, and — once there are two points — a line chart.
"""

import asyncio
import datetime as dt

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import charts
import db
import formatting
import keyboards
import ui
from fsm import BodyweightFlow
from parser import ParseError, parse_bodyweight

router = Router(name="bodyweight")


def _window(logs: list, weeks: int) -> list:
    """Logs within the last `weeks` weeks (0 = all), for the chart window."""
    if weeks <= 0:
        return logs
    cutoff = (dt.date.today() - dt.timedelta(weeks=weeks)).isoformat()
    return [r for r in logs if r["logged_at"][:10] >= cutoff]


def _daily_average_points(logs: list) -> list[tuple[dt.datetime, float]]:
    """One point per day (averaging same-day entries), so logging weight
    several times a day doesn't turn the trend line into noise."""
    by_date: dict[dt.date, list[float]] = {}
    for r in logs:
        d = dt.datetime.fromisoformat(r["logged_at"]).date()
        by_date.setdefault(d, []).append(float(r["weight"]))
    return [(dt.datetime.combine(d, dt.time()), sum(ws) / len(ws)) for d, ws in sorted(by_date.items())]


async def _render(event, state: FSMContext) -> None:
    """Render (or re-render) the bodyweight screen for a Message or CallbackQuery."""
    user_id = event.from_user.id
    user = await db.get_user(user_id)
    logs = await db.list_bodyweight_logs(user_id)
    data = await state.get_data()
    weeks = data.get("bw_weeks", keyboards.DEFAULT_BODYWEIGHT_WEEKS)
    await state.set_state(BodyweightFlow.viewing)
    await state.update_data(bw_weeks=weeks)
    chart_logs = _window(logs, weeks)
    text = formatting.build_bodyweight_screen(logs, user["unit"], period_logs=chart_logs)
    show_periods = len(logs) >= 2
    kb = keyboards.bodyweight_keyboard(has_logs=bool(logs), weeks=weeks, show_periods=show_periods)

    png = None
    points = _daily_average_points(chart_logs)
    if len(points) >= 2:
        unit_label = formatting.UNIT_LABELS.get(user["unit"], "кг")
        png = await asyncio.to_thread(
            charts.render_metric_over_sessions, points, f"Вес тела, {unit_label}", unit_label
        )

    message = event.message if isinstance(event, CallbackQuery) else event
    if png is None:
        if isinstance(event, CallbackQuery):
            await ui.safe_edit(event, text, reply_markup=kb, parse_mode="HTML")
        else:
            await message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        photo = BufferedInputFile(png, filename="bodyweight.png")
        if isinstance(event, CallbackQuery):
            await ui.safe_edit_photo(event, png, "bodyweight.png", text, reply_markup=kb, parse_mode="HTML")
        else:
            await message.answer_photo(photo, caption=text, reply_markup=kb, parse_mode="HTML")


async def show_bodyweight(callback: CallbackQuery, state: FSMContext) -> None:
    await db.get_or_create_user(callback.from_user.id, callback.from_user.username)
    await _render(callback, state)
    await callback.answer()


@router.callback_query(F.data == "menu:bodyweight")
async def menu_bodyweight(callback: CallbackQuery, state: FSMContext):
    await show_bodyweight(callback, state)


@router.callback_query(F.data == "bw:menu")
async def bw_menu(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("bw:period:"))
async def bw_period(callback: CallbackQuery, state: FSMContext):
    weeks = int(callback.data.split(":")[2])
    await state.update_data(bw_weeks=weeks)
    await _render(callback, state)
    await callback.answer()


@router.callback_query(F.data == "bw:undo")
async def bw_undo(callback: CallbackQuery, state: FSMContext):
    removed = await db.delete_last_bodyweight(callback.from_user.id)
    await callback.answer("Удалил последнюю запись" if removed else "Нет записей")
    await _render(callback, state)


@router.message(StateFilter(BodyweightFlow.viewing))
async def bw_weight_entered(message: Message, state: FSMContext):
    try:
        weight = parse_bodyweight(message.text)
    except ParseError as e:
        await message.reply(e.message)
        return
    await db.add_bodyweight_log(message.from_user.id, weight)
    await _render(message, state)
