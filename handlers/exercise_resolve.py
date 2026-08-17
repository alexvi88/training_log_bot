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
import i18n
import keyboards
import ui
from fsm import ResolveFlow
from state_scaffold import clear_state_keep_ai

router = Router(name="exercise_resolve")


async def start(event, state: FSMContext, names: list[str]) -> None:
    distinct = list(dict.fromkeys(n for n in names if n))
    await state.update_data(
        resolve_pending=distinct, resolve_resolved={}, resolve_total=len(distinct)
    )
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
    # Каталог спрашиваем наравне со своими: ручное добавление и поиск в живой
    # тренировке это уже делают, а импорт — нет, хотя именно он заводит
    # упражнения десятками, и совпавшее с каталогом имя приезжало голым.
    templates = await db.search_exercise_templates(event.from_user.id, name)
    total = data.get("resolve_total") or len(pending)
    position = total - len(pending) + 1
    text = i18n.t("resolve.progress", position=position, total=total, name=name)
    kb = keyboards.exercise_resolve_keyboard(
        candidates, name, "resolve", remaining=len(pending) - 1, templates=templates
    )
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


@router.callback_query(StateFilter(ResolveFlow.picking), F.data.startswith("resolve:tpl:"))
async def resolve_pick_template(callback: CallbackQuery, state: FSMContext):
    """Каталожный шаблон: форкаем его пользователю — вместе с группой, техникой
    и демо-фото — и засчитываем как разрешённое имя."""
    template_id = int(callback.data.split(":")[2])
    ex_id = await db.fork_exercise_from_template(callback.from_user.id, template_id)
    await db.touch_exercise_last_used(ex_id)
    await _resolve_current(callback, state, ex_id)
    await callback.answer()


@router.callback_query(StateFilter(ResolveFlow.picking), F.data == "resolve:create")
async def resolve_create(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    name = data["resolve_current_name"]
    groups = await db.list_muscle_groups(callback.from_user.id)
    kb = keyboards.groups_keyboard(
        groups, prefix="resolvegrp", extra_buttons=[(i18n.t("btn.back"), "resolve:back")]
    )
    await state.set_state(ResolveFlow.picking_new_group)
    await ui.safe_edit(callback, i18n.t("resolve.pick_group", name=name), reply_markup=kb)
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


_BULK_GROUP_NAME = "Другое"


@router.callback_query(StateFilter(ResolveFlow.picking), F.data == "resolve:createall")
async def resolve_create_all(callback: CallbackQuery, state: FSMContext):
    """Create every remaining unmatched name as-is, under «Другое».

    The per-name flow costs a pick plus a muscle-group choice each; on a foreign
    CSV with dozens of new names that's the difference between importing and
    giving up. The group can be changed later in ⚙️ Упражнения.
    """
    data = await state.get_data()
    pending = list(data.get("resolve_pending") or [])
    resolved = dict(data.get("resolve_resolved") or {})
    groups = await db.list_muscle_groups(callback.from_user.id)
    fallback = next((g for g in groups if g["name"] == _BULK_GROUP_NAME), None) or groups[0]
    for name in pending:
        ex_id = await db.create_exercise(callback.from_user.id, name, fallback["id"])
        await db.touch_exercise_last_used(ex_id)
        resolved[name] = ex_id
    await state.update_data(resolve_resolved=resolved, resolve_pending=[])
    await callback.answer(i18n.t("resolve.created_bulk", n=len(pending), group=fallback["name"]))
    await _next(callback, state)


@router.callback_query(StateFilter(ResolveFlow.picking, ResolveFlow.picking_new_group), F.data == "resolve:cancelall")
async def resolve_cancel_all(callback: CallbackQuery, state: FSMContext):
    # Отмена резолва имён не отменяет переписку с AI-тренером и черновик его
    # программы — сохраняем их.
    await clear_state_keep_ai(state)
    from handlers.settings import show_settings
    await show_settings(callback, state)
    await callback.answer(i18n.t("resolve.cancelled"))


@router.message(StateFilter(ResolveFlow.picking), F.text)
async def resolve_search_text(message: Message, state: FSMContext):
    query = message.text.strip()
    if not query:
        return
    data = await state.get_data()
    name = data["resolve_current_name"]
    candidates = await db.search_exercises(message.from_user.id, query)
    remaining = max(len(data.get("resolve_pending") or []) - 1, 0)
    kb = keyboards.exercise_resolve_keyboard(candidates, name, "resolve", remaining=remaining)
    if candidates:
        text = i18n.t("resolve.search_results", query=query, name=name)
    else:
        text = i18n.t("resolve.search_empty", query=query, name=name)
    await message.answer(text, reply_markup=kb)
