"""💪 Объём/нед — weekly working-set volume per muscle group vs the 5-10 target.

Closes the gap between the AI trainer's methodology ("5-10 sets/week/muscle")
and what the product could actually show: sets are aggregated by
exercises.primary_group_id over a calendar week (Mon-Sun), and each group is
flagged low / in-range / high. Every non-archived group the user sees is
listed, including those with zero sets — the gaps are the whole point.
"""

import datetime as dt

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

import analytics
import db
import formatting
import keyboards
import ui

router = Router(name="volume")


def _week_bounds(today: dt.date, offset: int) -> tuple[dt.date, dt.date]:
    this_monday = today - dt.timedelta(days=today.weekday())
    week_start = this_monday - dt.timedelta(weeks=offset)
    return week_start, week_start + dt.timedelta(days=6)


async def _build_rows(user_id: int, week_start: dt.date, week_end: dt.date) -> list[tuple[str, int, str]]:
    counts = await db.weekly_volume_by_group(user_id, week_start.isoformat(), week_end.isoformat())
    groups = await db.list_muscle_groups(user_id)
    rows: list[tuple[str, int, str]] = []
    for g in groups:
        count = counts.get(g["id"], 0)
        rows.append((g["name"], count, analytics.classify_weekly_volume(count)))
    ungrouped = counts.get(None, 0)
    if ungrouped:
        rows.append(("Без группы", ungrouped, analytics.classify_weekly_volume(ungrouped)))
    return rows


async def show_weekly_volume(callback: CallbackQuery, offset: int) -> None:
    today = dt.date.today()
    week_start, week_end = _week_bounds(today, offset)
    rows = await _build_rows(callback.from_user.id, week_start, week_end)
    text = formatting.build_weekly_volume_screen(week_start, rows, is_current_week=(offset == 0))
    kb = keyboards.weekly_volume_keyboard(offset)
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "menu:volume")
async def menu_volume(callback: CallbackQuery, state: FSMContext):
    await db.get_or_create_user(callback.from_user.id, callback.from_user.username)
    await show_weekly_volume(callback, offset=0)
    await callback.answer()


@router.callback_query(F.data.startswith("vol:wk:"))
async def vol_week(callback: CallbackQuery, state: FSMContext):
    offset = max(0, int(callback.data.split(":")[2]))
    await show_weekly_volume(callback, offset=offset)
    await callback.answer()
