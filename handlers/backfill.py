"""§A1 — manual backfill of a past workout: pick a date, then bulk-text entry."""

import datetime as dt

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import db
import formatting
import keyboards
import view_builder
from fsm import BackfillFlow
from parser import ParseError, parse_bulk_session, parse_ru_date

router = Router(name="backfill")

BULK_HINT = (
    "📝 Напиши тренировку текстом — одно упражнение на строку, сеты через запятую:\n\n"
    "Жим лёжа: 100x8, 100x7, 90x8\n"
    "Присед: 120x5x3\n\n"
    "«вес x повторы», «xN» в конце — N одинаковых подходов. Неизвестные названия "
    "потом помогу сопоставить."
)


def _entries_to_state(entries) -> list[dict]:
    return [
        {"name": e.name, "sets": [[s.weight, s.reps, s.is_warmup] for s in e.sets]}
        for e in entries
    ]


@router.callback_query(F.data == "menu:backfill_workout")
async def backfill_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(BackfillFlow.awaiting_date)
    await callback.message.edit_text(
        "📅 На какую дату занести тренировку?\nВыбери или напиши дату в формате дд.мм.гггг:",
        reply_markup=keyboards.date_quick_keyboard("bf"),
    )
    await callback.answer()


async def _date_chosen(event, state: FSMContext, date: dt.date):
    await state.update_data(bf_date=date.isoformat())
    await state.set_state(BackfillFlow.awaiting_bulk_text)
    kb = keyboards.cancel_keyboard("bf:cancel")
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(BULK_HINT, reply_markup=kb)
    else:
        await event.answer(BULK_HINT, reply_markup=kb)


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


@router.message(StateFilter(BackfillFlow.awaiting_bulk_text))
async def bf_bulk_text(message: Message, state: FSMContext):
    try:
        entries = parse_bulk_session(message.text)
    except ParseError as e:
        await message.reply(e.message)
        return

    resolved: dict[str, int] = {}
    unresolved: list[str] = []
    for entry in entries:
        ex = await db.find_exercise_by_name(message.from_user.id, entry.name)
        if ex:
            resolved[entry.name] = ex["id"]
        elif entry.name not in unresolved:
            unresolved.append(entry.name)

    await state.update_data(bf_entries=_entries_to_state(entries), bf_resolved=resolved)

    if unresolved:
        from handlers.exercise_resolve import start as start_resolve
        await start_resolve(message, state, unresolved, flow="backfill")
    else:
        await show_confirmation(message, state)


async def on_exercises_resolved(event, state: FSMContext) -> None:
    data = await state.get_data()
    resolved = dict(data.get("bf_resolved") or {})
    resolved.update(data.get("resolve_resolved") or {})
    await state.update_data(bf_resolved=resolved)
    await show_confirmation(event, state)


async def show_confirmation(event, state: FSMContext) -> None:
    data = await state.get_data()
    entries = data["bf_entries"]
    resolved = data["bf_resolved"]
    date = dt.date.fromisoformat(data["bf_date"])

    lines = [f"📋 Проверь перед сохранением — {formatting.format_date_ru(date)}:"]
    total_sets = 0
    for entry in entries:
        ex = await db.get_exercise(resolved[entry["name"]])
        sets_text = ", ".join(formatting.format_set(w, r, bool(warm)) for w, r, warm in entry["sets"])
        lines.append(f"• {ex['display_name']}: {sets_text}")
        total_sets += len(entry["sets"])
    lines.append(f"\nВсего {len(entries)} упражнения, {total_sets} сетов.")

    await state.set_state(BackfillFlow.confirming)
    kb = keyboards.confirm_cancel_keyboard("bf:save", "bf:cancel")
    text = "\n".join(lines)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb)
    else:
        await event.answer(text, reply_markup=kb)


@router.callback_query(StateFilter(BackfillFlow.confirming), F.data == "bf:save")
async def bf_save(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    entries = data["bf_entries"]
    resolved = data["bf_resolved"]
    user_id = callback.from_user.id
    started_at = f"{data['bf_date']}T12:00:00"

    workout_id = await db.create_finished_workout(user_id, started_at, started_at, source="manual")
    for entry in entries:
        ex_id = resolved[entry["name"]]
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.touch_exercise_last_used(ex_id)
        for idx, (weight, reps, is_warmup) in enumerate(entry["sets"], start=1):
            await db.add_set(block_id, ex_id, idx, 0, weight, reps, bool(is_warmup))

    user = await db.get_user(user_id)
    blocks = await view_builder.build_block_views(workout_id, user["e1rm_formula"])
    started = dt.datetime.fromisoformat(started_at)
    summary = formatting.build_workout_summary(
        started, blocks, None,
        hide_warmups=bool(user["hide_warmups"]), show_extra_stats=bool(user["show_extra_stats"]),
    )
    await state.clear()
    await callback.message.edit_text(
        f"✅ Сохранено как прошлая тренировка\n\n{summary}", parse_mode="HTML"
    )
    active = await db.get_active_workout(user_id)
    await callback.message.answer("Что дальше?", reply_markup=keyboards.main_menu(bool(active)))
    await callback.answer()


@router.callback_query(F.data == "bf:cancel")
async def bf_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer("Отменено")
