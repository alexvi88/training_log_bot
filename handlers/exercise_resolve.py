"""Shared sub-flow: map a free-typed exercise name to an exercise row.

Used by CSV import (§A3) for every name the resolver (handlers.csv_import.
match_exercise_names) could not place with confidence: похожие свои есть, а
какое из них — решает человек. Walks through each such name one at a time,
then hands control back to the importer via `on_exercises_resolved(event,
state)`.

Ничего не пишет в базу: выбор копится решениями в состоянии диалога
(resolve_decisions, тот же формат, что handlers.csv_import.default_decisions),
а заводит упражнения только «Загрузить» (csv_import.materialize_decisions).
Отмена на любом шаге не оставляет после себя ни упражнений, ни групп.
"""

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import db
import formatting
import i18n
import keyboards
import ui
from fsm import ResolveFlow
from state_scaffold import clear_state_keep_ai

router = Router(name="exercise_resolve")


async def start(event, state: FSMContext, names: list[str]) -> None:
    distinct = list(dict.fromkeys(n for n in names if n))
    await state.update_data(
        resolve_pending=distinct, resolve_decisions={}, resolve_total=len(distinct)
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


async def _candidates_for(user_id: int, name: str, data: dict) -> list:
    """Похожие свои — сначала те, что предложил резолвер (лучшие первыми),
    потом поиск по имени, без повторов."""
    choice = (data.get("imp_choices") or {}).get(name) or {}
    rows = []
    seen = set()
    for ex_id in choice.get("candidates") or []:
        row = await db.get_exercise(ex_id)
        if row is not None and row["user_id"] == user_id and not row["is_archived"] and ex_id not in seen:
            rows.append(row)
            seen.add(ex_id)
    for row in await db.search_exercises(user_id, name):
        if row["id"] not in seen:
            rows.append(row)
            seen.add(row["id"])
    return rows


async def _next(event, state: FSMContext) -> None:
    data = await state.get_data()
    pending = list(data.get("resolve_pending") or [])
    if not pending:
        await _dispatch_done(event, state)
        return
    name = pending[0]
    await state.update_data(resolve_current_name=name)
    await state.set_state(ResolveFlow.picking)
    user_id = event.from_user.id
    candidates = await _candidates_for(user_id, name, data)
    # Каталог спрашиваем наравне со своими: совпавшее с каталогом имя иначе
    # заводилось бы голым — без техники, фото и группы.
    templates = list(await db.search_exercise_templates(user_id, name))
    proposed = ((data.get("imp_choices") or {}).get(name) or {}).get("template")
    if proposed:
        tpl = await db._find_global_template_by_name(proposed)
        if tpl is not None:
            templates = [tpl] + [t for t in templates if t["id"] != tpl["id"]]
    total = data.get("resolve_total") or len(pending)
    position = total - len(pending) + 1
    key = "resolve.progress_similar" if candidates else "resolve.progress"
    text = i18n.t(key, position=position, total=total, name=name)
    kb = keyboards.exercise_resolve_keyboard(
        candidates, name, "resolve", remaining=len(pending) - 1, templates=templates
    )
    await _render(event, text, kb)


async def _resolve_current(event, state: FSMContext, decision: dict) -> None:
    data = await state.get_data()
    name = data["resolve_current_name"]
    decisions = dict(data.get("resolve_decisions") or {})
    decisions[name] = decision
    pending = list(data.get("resolve_pending") or [])
    if pending and pending[0] == name:
        pending.pop(0)
    await state.update_data(resolve_decisions=decisions, resolve_pending=pending)
    await _next(event, state)


@router.callback_query(StateFilter(ResolveFlow.picking), F.data.startswith("resolve:pick:"))
async def resolve_pick(callback: CallbackQuery, state: FSMContext):
    """Своё упражнение: имя из файла ляжет в него и закрепится за ним — в
    следующий раз импорт не переспросит (exercise_aliases)."""
    ex_id = int(callback.data.split(":")[2])
    exercise = await db.get_exercise(ex_id)
    if exercise is None or exercise["user_id"] != callback.from_user.id:
        await callback.answer()
        return
    await _resolve_current(callback, state, {"kind": "existing", "id": ex_id, "chosen": True})
    await callback.answer()


@router.callback_query(StateFilter(ResolveFlow.picking), F.data.startswith("resolve:tpl:"))
async def resolve_pick_template(callback: CallbackQuery, state: FSMContext):
    """Каталожный шаблон: новое упражнение заведётся под ИМЕНЕМ ИЗ ФАЙЛА,
    привязанным к шаблону (группа, техника, фото), — то же правило имён, что у
    резолвера; а если такое упражнение каталога у атлета уже есть — имя
    ляжет в него. Заводится при «Загрузить», не сейчас."""
    template_id = int(callback.data.split(":")[2])
    template = await db.get_exercise(template_id)
    if template is None or not template["is_template"]:
        await callback.answer()
        return
    await _resolve_current(callback, state, {"kind": "template", "template": template["name"]})
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
    await state.set_state(ResolveFlow.picking)
    await _resolve_current(callback, state, {"kind": "new", "group_id": group_id})
    await callback.answer()


@router.callback_query(StateFilter(ResolveFlow.picking), F.data == "resolve:createall")
async def resolve_create_all(callback: CallbackQuery, state: FSMContext):
    """Every remaining name as-is: с шаблоном, если резолвер его нашёл
    (группа и фото оттуда), иначе в группу «Другое» — её можно поменять
    потом в ⚙️ Упражнения. На иностранном файле с десятками имён это разница
    между «перенёс» и «бросил». Заводится при «Загрузить», не сейчас."""
    data = await state.get_data()
    pending = list(data.get("resolve_pending") or [])
    decisions = dict(data.get("resolve_decisions") or {})
    choices = data.get("imp_choices") or {}
    for name in pending:
        proposed = (choices.get(name) or {}).get("template")
        decisions[name] = {"kind": "template", "template": proposed} if proposed else {"kind": "new"}
    await state.update_data(resolve_decisions=decisions, resolve_pending=[])
    other = await db.other_muscle_group_id()
    group = await db.get_muscle_group(other) if other is not None else None
    group_name = formatting.format_group_lower(group["name"]) if group is not None else ""
    await callback.answer(i18n.t("resolve.created_bulk", n=len(pending), group=group_name))
    await _next(callback, state)


@router.callback_query(StateFilter(ResolveFlow.picking, ResolveFlow.picking_new_group), F.data == "resolve:cancelall")
async def resolve_cancel_all(callback: CallbackQuery, state: FSMContext):
    """«Отменить весь ввод» — туда, откуда зашли в импорт (главное меню или
    ⚙️ Настройки), и ничего не остаётся в базе: до «Загрузить» импорт ничего
    не заводит. Переписка с AI-тренером и черновик его программы живут."""
    data = await state.get_data()
    origin = data.get("import_origin", "settings")
    await clear_state_keep_ai(state)
    await callback.answer(i18n.t("resolve.cancelled"))
    from handlers.csv_import import _return_to_origin
    await _return_to_origin(callback, state, origin)


@router.message(StateFilter(ResolveFlow.picking), F.text)
async def resolve_search_text(message: Message, state: FSMContext):
    query = message.text.strip()
    if not query:
        return
    data = await state.get_data()
    name = data["resolve_current_name"]
    candidates = await db.search_exercises(message.from_user.id, query)
    templates = await db.search_exercise_templates(message.from_user.id, query)
    remaining = max(len(data.get("resolve_pending") or []) - 1, 0)
    kb = keyboards.exercise_resolve_keyboard(
        candidates, name, "resolve", remaining=remaining, templates=templates
    )
    if candidates or templates:
        text = i18n.t("resolve.search_results", query=query, name=name)
    else:
        text = i18n.t("resolve.search_empty", query=query, name=name)
    await message.answer(text, reply_markup=kb)
