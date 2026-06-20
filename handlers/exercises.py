"""CRUD/browsing for muscle groups and exercises (the "⚙️ Упражнения" menu)."""

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
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
    group_id = int(callback.data.split(":")[2])
    await state.update_data(exm_group_id=group_id)
    await _show_exercise_list(callback, state)


async def _show_exercise_list(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ExerciseManage.picking_exercise)
    data = await state.get_data()
    group_id = data["exm_group_id"]
    exercises = await db.list_user_exercises_in_group(callback.from_user.id, group_id)
    group = await db.get_muscle_group(group_id)
    b = InlineKeyboardBuilder()
    for ex in exercises:
        b.button(text=ex["display_name"], callback_data=f"exm:ex:{ex['id']}")
    b.button(text="🗑 Архивировать группу", callback_data=f"exm:archivegrp:{group_id}")
    b.button(text="⬅️ Назад", callback_data="exm:backgroups")
    b.adjust(1)
    text = f"{group['name']}\n\nТвои упражнения:" if exercises else \
        f"{group['name']}\n\nПока нет своих упражнений в этой группе."
    await callback.message.edit_text(text, reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "exm:backgroups")
async def exm_back_to_groups(callback: CallbackQuery, state: FSMContext):
    await show_exercise_groups(callback, state)


@router.callback_query(F.data.startswith("exm:archivegrp:"))
async def exm_archive_group(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split(":")[2])
    await db.archive_muscle_group(group_id)
    await callback.answer("Группа архивирована")
    await show_exercise_groups(callback, state)


def _exercise_detail_view(ex):
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Шаг веса", callback_data=f"exm:step:{ex['id']}")
    b.button(text="🗑 Архивировать", callback_data=f"exm:archive:{ex['id']}")
    b.button(text="⬅️ Назад", callback_data="exm:backlist")
    b.adjust(1)
    info = [f"Название: {ex['name']}"]
    if ex["equipment"]:
        info.append(f"Оснастка: {ex['equipment']}")
    if ex["unilateral"]:
        info.append("Одной рукой/ногой: да")
    if ex["attachment"]:
        info.append(f"Хват/насадка: {ex['attachment']}")
    step_label = f"{ex['weight_step']} кг" if ex["weight_step"] is not None else "по умолчанию"
    big_label = f"{ex['weight_step_big']} кг" if ex["weight_step_big"] is not None else "×4 от шага"
    info.append(f"Шаг веса: {step_label} / крупный шаг: {big_label}")
    info.append(f"Создано: {ex['created_at'][:10]}")
    return "\n".join(info), b.as_markup()


@router.callback_query(StateFilter(ExerciseManage.picking_exercise), F.data.startswith("exm:ex:"))
async def exm_pick_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    await state.update_data(exm_exercise_id=ex_id)
    ex = await db.get_exercise(ex_id)
    text, kb = _exercise_detail_view(ex)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("exm:step:"))
async def exm_edit_step(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    await state.update_data(exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.editing_step)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="exm:backlist")
    await callback.message.edit_text(
        "Шаг веса в кг. Один шаг: «2.5». Шаг и крупный шаг: «2.5 10». "
        "«0» — сбросить на дефолт.",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(StateFilter(ExerciseManage.editing_step))
async def exm_step_entered(message: Message, state: FSMContext):
    data = await state.get_data()
    ex_id = data["exm_exercise_id"]
    text = message.text.strip().replace(",", ".")
    if text == "0":
        await db.update_exercise_step(ex_id, None, None)
    else:
        parts = text.split()
        try:
            step = float(parts[0])
            big = float(parts[1]) if len(parts) > 1 else None
            if step <= 0 or (big is not None and big <= 0):
                raise ValueError
        except (ValueError, IndexError):
            await message.reply("Нужно число, например 2.5 или «2.5 10»")
            return
        await db.update_exercise_step(ex_id, step, big)
    await state.set_state(ExerciseManage.picking_exercise)
    ex = await db.get_exercise(ex_id)
    text, kb = _exercise_detail_view(ex)
    await message.answer(text, reply_markup=kb)


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
    b.button(text="➕ Новая группа", callback_data="exm:newgroup")
    b.button(text="⬅️ Назад", callback_data="exm:back")
    b.adjust(2)
    await fake_cb_message.edit_text("⚙️ Упражнения — выбери группу мышц:", reply_markup=b.as_markup())
    await state.set_state(ExerciseManage.picking_group)
