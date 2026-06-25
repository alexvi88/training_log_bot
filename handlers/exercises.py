"""CRUD/browsing for muscle groups and exercises (the "⚙️ Упражнения" menu)."""

from html import escape

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
import keyboards
from fsm import ExerciseManage

router = Router(name="exercises")


async def show_exercise_groups(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ExerciseManage.picking_group)
    groups = await db.list_muscle_groups(callback.from_user.id)
    b = InlineKeyboardBuilder()
    for g in groups:
        b.button(text=g["name"], callback_data=f"exm:grp:{g['id']}")
    b.button(text="📋 Все", callback_data="exm:grp:all")
    b.button(text="➕ Новая группа", callback_data="exm:newgroup")
    b.button(text="⬅️ Назад", callback_data="exm:back")
    b.adjust(2)
    await callback.message.edit_text("⚙️ Упражнения — выбери группу мышц:", reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "exm:back")
async def exm_back(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)


@router.callback_query(StateFilter(ExerciseManage.picking_group), F.data.startswith("exm:grp:"))
async def exm_pick_group(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.split(":")[2]
    group_id = None if raw == "all" else int(raw)
    await state.update_data(exm_group_id=group_id)
    await _show_exercise_list(callback, state)


async def _show_exercise_list(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ExerciseManage.picking_exercise)
    data = await state.get_data()
    group_id = data["exm_group_id"]
    if group_id is None:
        exercises = await db.list_user_exercises(callback.from_user.id)
        group = None
    else:
        exercises = await db.list_user_exercises_in_group(callback.from_user.id, group_id)
        group = await db.get_muscle_group(group_id)
    b = InlineKeyboardBuilder()
    items = [(f"exm:ex:{ex['id']}", ex["display_name"]) for ex in exercises]
    for row in keyboards.numbered_buttons(items):
        b.row(*row)
    if group is not None:
        b.row(InlineKeyboardButton(text="➕ Новое упражнение", callback_data="exm:newex"))
        if group["user_id"] is not None:
            b.row(InlineKeyboardButton(text="🗑 Архивировать группу", callback_data=f"exm:archivegrp:{group_id}"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="exm:backgroups"))
    title = group["name"] if group is not None else "Все упражнения"
    if exercises:
        names = [ex["display_name"] for ex in exercises]
        text = f"{title}\n\nТвои упражнения:\n" + keyboards.numbered_list(names)
    else:
        text = f"{title}\n\nПока нет своих упражнений в этой группе."
    await callback.message.edit_text(text, reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "exm:backgroups")
async def exm_back_to_groups(callback: CallbackQuery, state: FSMContext):
    await show_exercise_groups(callback, state)


@router.callback_query(StateFilter(ExerciseManage.picking_exercise), F.data == "exm:newex")
async def exm_new_exercise(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ExerciseManage.creating_exercise_name)
    await callback.message.edit_text(
        "Напиши название нового упражнения, или выбери из шаблонов:",
        reply_markup=keyboards.new_exercise_entry_keyboard("exm"),
    )
    await callback.answer()


@router.callback_query(StateFilter(ExerciseManage.creating_exercise_name), F.data == "exm:templates")
async def exm_templates(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    templates = await db.list_templates_in_group(data["exm_group_id"])
    kb = keyboards.templates_keyboard(templates, prefix="exm", back_cb="newback")
    if templates:
        names = [t["display_name"] for t in templates]
        text = "Шаблоны — выбери подходящий:\n" + keyboards.numbered_list(names)
    else:
        text = "Для этой группы пока нет шаблонов."
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(StateFilter(ExerciseManage.creating_exercise_name), F.data == "exm:newback")
async def exm_new_back(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "Напиши название нового упражнения, или выбери из шаблонов:",
        reply_markup=keyboards.new_exercise_entry_keyboard("exm"),
    )
    await callback.answer()


@router.callback_query(StateFilter(ExerciseManage.creating_exercise_name), F.data == "exm:cancel")
async def exm_new_cancel(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ExerciseManage.picking_exercise)
    await _show_exercise_list(callback, state)


@router.callback_query(StateFilter(ExerciseManage.creating_exercise_name), F.data.startswith("exm:tpl:"))
async def exm_pick_template(callback: CallbackQuery, state: FSMContext):
    template_id = int(callback.data.split(":")[2])
    ex_id = await db.fork_exercise_from_template(callback.from_user.id, template_id)
    await state.update_data(exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.picking_exercise)
    ex = await db.get_exercise(ex_id)
    text, kb = _exercise_detail_view(ex)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(StateFilter(ExerciseManage.creating_exercise_name))
async def exm_new_exercise_name_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.reply("Название не может быть пустым")
        return
    data = await state.get_data()
    ex_id = await db.create_exercise(message.from_user.id, name, data["exm_group_id"])
    await state.update_data(exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.picking_exercise)
    ex = await db.get_exercise(ex_id)
    text, kb = _exercise_detail_view(ex)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("exm:archivegrp:"))
async def exm_archive_group(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split(":")[2])
    group = await db.get_muscle_group(group_id)
    if group is None or group["user_id"] is None:
        await callback.answer("Эту группу нельзя архивировать", show_alert=True)
        return
    await db.archive_muscle_group(group_id)
    await callback.answer("Группа архивирована")
    await show_exercise_groups(callback, state)


def _exercise_detail_view(ex):
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Название", callback_data=f"exm:editname:{ex['id']}")
    b.button(text="🗑 Архивировать", callback_data=f"exm:archive:{ex['id']}")
    b.button(text="⬅️ Назад", callback_data="exm:backlist")
    b.adjust(1)
    info = [f"Название: <b>{escape(ex['name'])}</b>"]
    if ex["equipment"]:
        info.append(f"Оснастка: {ex['equipment']}")
    if ex["unilateral"]:
        info.append("Одной рукой/ногой: да")
    if ex["attachment"]:
        info.append(f"Хват/насадка: {ex['attachment']}")
    info.append(f"Создано: {ex['created_at'][:10]}")
    return "\n".join(info), b.as_markup()


@router.callback_query(StateFilter(ExerciseManage.picking_exercise), F.data.startswith("exm:ex:"))
async def exm_pick_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    await state.update_data(exm_exercise_id=ex_id)
    ex = await db.get_exercise(ex_id)
    text, kb = _exercise_detail_view(ex)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("exm:editname:"))
async def exm_edit_name(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    await state.update_data(exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.editing_name)
    ex = await db.get_exercise(ex_id)
    await callback.message.edit_text(
        f"Текущее название: <code>{escape(ex['name'])}</code>\n\nНапиши новое название упражнения:",
        reply_markup=keyboards.cancel_keyboard("exm:backlist"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(StateFilter(ExerciseManage.editing_name))
async def exm_name_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.reply("Название не может быть пустым")
        return
    data = await state.get_data()
    ex_id = data["exm_exercise_id"]
    ok = await db.update_exercise_name(ex_id, name)
    if not ok:
        await message.reply("У тебя уже есть упражнение с таким названием.")
        return
    await state.set_state(ExerciseManage.picking_exercise)
    ex = await db.get_exercise(ex_id)
    text, kb = _exercise_detail_view(ex)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "exm:backlist")
async def exm_back_to_list(callback: CallbackQuery, state: FSMContext):
    await _show_exercise_list(callback, state)


@router.callback_query(F.data.startswith("exm:archive:"))
async def exm_archive_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    await db.archive_exercise(ex_id)
    await callback.answer("Упражнение архивировано")
    await _show_exercise_list(callback, state)


@router.callback_query(F.data == "exm:newgroup")
async def exm_new_group(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ExerciseManage.new_group_name)
    await callback.message.edit_text(
        "Напиши название новой группы мышц:",
        reply_markup=keyboards.cancel_keyboard("exm:backgroups"),
    )
    await callback.answer()


@router.message(StateFilter(ExerciseManage.new_group_name))
async def exm_new_group_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.reply("Название не может быть пустым")
        return
    await db.create_muscle_group(message.from_user.id, name)
    await message.answer(f"Группа «{name}» создана.")
    fake_cb_message = await message.answer("⚙️ Упражнения")
    groups = await db.list_muscle_groups(message.from_user.id)
    b = InlineKeyboardBuilder()
    for g in groups:
        b.button(text=g["name"], callback_data=f"exm:grp:{g['id']}")
    b.button(text="📋 Все", callback_data="exm:grp:all")
    b.button(text="➕ Новая группа", callback_data="exm:newgroup")
    b.button(text="⬅️ Назад", callback_data="exm:back")
    b.adjust(2)
    await fake_cb_message.edit_text("⚙️ Упражнения — выбери группу мышц:", reply_markup=b.as_markup())
    await state.set_state(ExerciseManage.picking_group)
