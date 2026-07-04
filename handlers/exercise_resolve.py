"""Shared sub-flow: map a free-typed exercise name to an exercise row.

Used by CSV import (§A3) whenever a name in the input doesn't exactly match
anything in the user's exercise list. Walks through each unmatched name one
at a time, then hands control back to the importer via
`on_exercises_resolved(event, state)`.
"""

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import db
import keyboards
import ui
from fsm import ResolveFlow

router = Router(name="exercise_resolve")


async def start(event, state: FSMContext, names: list[str]) -> None:
    distinct = list(dict.fromkeys(n for n in names if n))
    await state.update_data(resolve_pending=distinct, resolve_resolved={})
    await _next(event, state)


async def _render(event, text: str, kb) -> None:
    if isinstance(event, CallbackQuery):
        await ui.safe_edit(event, text, reply_markup=kb)
    else:
        await event.answer(text, reply_markup=kb)


async def _dispatch_done(event, state: FSMContext) -> None:
    from handlers.csv_import import on_exercises_resolved
    await on_exercises_resolved(event, state)


async def _next(event, state: FSMContext) -> None:
    data = await state.get_data()
    pending = list(data.get("resolve_pending") or [])
    if not pending:
        await _dispatch_done(event, state)
        return
    name = pending[0]
    await state.update_data(resolve_current_name=name)
    await state.set_state(ResolveFlow.picking)
    candidates = await db.search_exercises(event.from_user.id, name)
    text = (
        f"Не нашёл упражнение «{name}» в твоём списке.\n"
        "Выбери похожее, создай новое, или напиши другое название для поиска:"
    )
    kb = keyboards.exercise_resolve_keyboard(candidates, name, "resolve")
    await _render(event, text, kb)


async def _resolve_current(event, state: FSMContext, exercise_id: int) -> None:
    data = await state.get_data()
    name = data["resolve_current_name"]
    resolved = dict(data.get("resolve_resolved") or {})
    resolved[name] = exercise_id
    pending = list(data.get("resolve_pending") or [])
    if pending and pending[0] == name:
        pending.pop(0)
    await state.update_data(resolve_resolved=resolved, resolve_pending=pending)
    await _next(event, state)


@router.callback_query(StateFilter(ResolveFlow.picking), F.data.startswith("resolve:pick:"))
async def resolve_pick(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    await db.touch_exercise_last_used(ex_id)
    await _resolve_current(callback, state, ex_id)
    await callback.answer()


@router.callback_query(StateFilter(ResolveFlow.picking), F.data == "resolve:create")
async def resolve_create(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    name = data["resolve_current_name"]
    groups = await db.list_muscle_groups(callback.from_user.id)
    kb = keyboards.groups_keyboard(groups, prefix="resolvegrp", extra_buttons=[("⬅️ Назад", "resolve:back")])
    await state.set_state(ResolveFlow.picking_new_group)
    await ui.safe_edit(callback, f"«{name}» — выбери группу мышц:", reply_markup=kb)
    await callback.answer()


@router.callback_query(StateFilter(ResolveFlow.picking_new_group), F.data == "resolve:back")
async def resolve_create_back(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ResolveFlow.picking)
    await _next(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(ResolveFlow.picking_new_group), F.data.startswith("resolvegrp:grp:"))
async def resolve_pick_group(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split(":")[2])
    data = await state.get_data()
    name = data["resolve_current_name"]
    ex_id = await db.create_exercise(callback.from_user.id, name, group_id)
    await db.touch_exercise_last_used(ex_id)
    await state.set_state(ResolveFlow.picking)
    await _resolve_current(callback, state, ex_id)
    await callback.answer()


@router.callback_query(StateFilter(ResolveFlow.picking, ResolveFlow.picking_new_group), F.data == "resolve:cancelall")
async def resolve_cancel_all(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    from handlers.settings import show_settings
    await show_settings(callback, state)
    await callback.answer("Отменено")


@router.message(StateFilter(ResolveFlow.picking))
async def resolve_search_text(message: Message, state: FSMContext):
    query = message.text.strip()
    if not query:
        return
    data = await state.get_data()
    name = data["resolve_current_name"]
    candidates = await db.search_exercises(message.from_user.id, query)
    kb = keyboards.exercise_resolve_keyboard(candidates, name, "resolve")
    if candidates:
        text = f"Результаты поиска «{query}» для «{name}»:"
    else:
        text = f"Ничего не нашлось по «{query}». Можно создать новое для «{name}»."
    await message.answer(text, reply_markup=kb)
