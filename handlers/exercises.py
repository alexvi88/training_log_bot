"""CRUD/browsing for muscle groups and exercises (the "⚙️ Упражнения" menu)."""

from contextlib import suppress
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InputMediaPhoto, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import db
import exercise_media
import keyboards
import ui
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
    await ui.safe_edit(callback, "⚙️ Упражнения — выбери группу мышц:", reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "exm:back")
async def exm_back(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)


@router.callback_query(StateFilter(ExerciseManage.picking_group), F.data.startswith("exm:grp:"))
async def exm_pick_group(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.split(":")[2]
    group_id = None if raw == "all" else int(raw)
    await state.update_data(exm_group_id=group_id, exm_page=0)
    await _show_exercise_list(callback, state)


@router.callback_query(StateFilter(ExerciseManage.picking_exercise), F.data.startswith("exm:page:"))
async def exm_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[2])
    await state.update_data(exm_page=page)
    await _show_exercise_list(callback, state)


async def _clear_exercise_media(bot, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    old_ids = data.get("exm_media_msg_ids")
    if not old_ids:
        return
    for mid in old_ids:
        with suppress(TelegramBadRequest):
            await bot.delete_message(chat_id, mid)
    await state.update_data(exm_media_msg_ids=None)


async def _show_exercise_list(callback: CallbackQuery, state: FSMContext):
    await _clear_exercise_media(callback.bot, callback.message.chat.id, state)
    await state.set_state(ExerciseManage.picking_exercise)
    data = await state.get_data()
    group_id = data.get("exm_group_id")
    page = data.get("exm_page", 0)
    offset = page * config.RECENT_EXERCISES_LIMIT
    if group_id is None:
        exercises = await db.list_user_exercises(
            callback.from_user.id, limit=config.RECENT_EXERCISES_LIMIT, offset=offset
        )
        total = await db.count_user_exercises(callback.from_user.id)
        group = None
    else:
        exercises = await db.list_user_exercises_in_group(
            callback.from_user.id, group_id, limit=config.RECENT_EXERCISES_LIMIT, offset=offset
        )
        total = await db.count_user_exercises_in_group(callback.from_user.id, group_id)
        group = await db.get_muscle_group(group_id)
    has_next = offset + len(exercises) < total
    b = InlineKeyboardBuilder()
    items = [(f"exm:ex:{ex['id']}", ex["display_name"]) for ex in exercises]
    for row in keyboards.named_buttons(items):
        b.row(*row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"exm:page:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"exm:page:{page + 1}"))
    if nav:
        b.row(*nav)
    if group is not None:
        b.row(InlineKeyboardButton(text="➕ Новое упражнение", callback_data="exm:newex"))
        if group["user_id"] is not None:
            b.row(InlineKeyboardButton(text="🗑 Архивировать группу", callback_data=f"exm:archivegrpask:{group_id}"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="exm:backgroups"))
    title = group["name"] if group is not None else "Все упражнения"
    title_html = f"<b>{escape(title.upper())}</b>"
    if exercises:
        text = f"{title_html}\n\nТвои упражнения:"
    else:
        text = f"{title_html}\n\nПока нет своих упражнений в этой группе."
    await ui.safe_edit(callback, text, reply_markup=b.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "exm:backgroups")
async def exm_back_to_groups(callback: CallbackQuery, state: FSMContext):
    await show_exercise_groups(callback, state)


@router.callback_query(StateFilter(ExerciseManage.picking_exercise), F.data == "exm:newex")
async def exm_new_exercise(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ExerciseManage.creating_exercise_name)
    await ui.safe_edit(
        callback,
        "Напиши название нового упражнения, или выбери из шаблонов:",
        reply_markup=keyboards.new_exercise_entry_keyboard("exm"),
    )
    await callback.answer()


@router.callback_query(StateFilter(ExerciseManage.creating_exercise_name), F.data == "exm:templates")
async def exm_templates(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    templates = await db.list_templates_in_group(data["exm_group_id"])
    kb = keyboards.templates_keyboard(templates, prefix="exm", back_cb="newback")
    text = "Шаблоны — выбери подходящий:" if templates else "Для этой группы пока нет шаблонов."
    await ui.safe_edit(callback, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(StateFilter(ExerciseManage.creating_exercise_name), F.data == "exm:newback")
async def exm_new_back(callback: CallbackQuery, state: FSMContext):
    await ui.safe_edit(
        callback,
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
    await _send_exercise_images(callback.message, ex, state)
    text, kb = _exercise_detail_view(ex)
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
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


@router.callback_query(F.data.startswith("exm:archivegrpask:"))
async def exm_archive_group_confirm(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split(":")[2])
    group = await db.get_muscle_group(group_id)
    if group is None or group["user_id"] != callback.from_user.id:
        await callback.answer("Эту группу нельзя архивировать", show_alert=True)
        return
    kb = keyboards.yes_no_keyboard(
        yes_cb=f"exm:archivegrpyes:{group_id}",
        no_cb="exm:backlist",
        yes_text="🗑 Архивировать",
        no_text="❌ Отмена",
    )
    await ui.safe_edit(
        callback,
        f"Архивировать группу «{escape(group['name'])}»? "
        "Все упражнения группы пропадут из списков, но история тренировок сохранится.",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("exm:archivegrpyes:"))
async def exm_archive_group(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split(":")[2])
    group = await db.get_muscle_group(group_id)
    if group is None or group["user_id"] != callback.from_user.id:
        await callback.answer("Эту группу нельзя архивировать", show_alert=True)
        return
    await db.archive_muscle_group(group_id)
    await callback.answer("Группа архивирована")
    await show_exercise_groups(callback, state)


def _exercise_detail_view(ex):
    b = InlineKeyboardBuilder()
    b.button(text="📈 Прогресс", callback_data=f"prog:ex:{ex['id']}:m")
    b.button(text="✏️ Название", callback_data=f"exm:editname:{ex['id']}")
    b.button(text="🗑 Архивировать", callback_data=f"exm:archiveask:{ex['id']}")
    b.button(text="⬅️ Назад", callback_data="exm:backlist")
    b.adjust(2)
    info = [f"Название: <b>{escape(ex['name'])}</b>"]
    if ex["equipment"]:
        info.append(f"Оснастка: {ex['equipment']}")
    if ex["unilateral"]:
        info.append("Одной рукой/ногой: да")
    if ex["attachment"]:
        info.append(f"Хват/насадка: {ex['attachment']}")
    info.append(f"Создано: {ex['created_at'][:10]}")
    return "\n".join(info), b.as_markup()


async def _send_exercise_images(message: Message, ex, state: FSMContext) -> None:
    await _clear_exercise_media(message.bot, message.chat.id, state)
    images = exercise_media.get_images(ex["name"])
    if images:
        sent = await message.answer_media_group([InputMediaPhoto(media=FSInputFile(p)) for p in images])
        await state.update_data(exm_media_msg_ids=[m.message_id for m in sent])


@router.callback_query(StateFilter(ExerciseManage.picking_exercise), F.data.startswith("exm:ex:"))
async def exm_pick_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    await state.update_data(exm_exercise_id=ex_id)
    await _send_exercise_images(callback.message, ex, state)
    text, kb = _exercise_detail_view(ex)
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(StateFilter(ExerciseManage.picking_exercise))
async def exm_search_text(message: Message, state: FSMContext):
    """Typing while browsing the exercise list searches instead of being silently dropped."""
    query = message.text.strip()
    if not query:
        return
    data = await state.get_data()
    group_id = data.get("exm_group_id")
    results = await db.search_exercises(message.from_user.id, query)
    b = InlineKeyboardBuilder()
    items = [(f"exm:ex:{ex['id']}", ex["display_name"]) for ex in results]
    for row in keyboards.named_buttons(items):
        b.row(*row)
    if group_id is not None:
        b.row(InlineKeyboardButton(text="➕ Новое упражнение", callback_data="exm:newex"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="exm:backlist"))
    text = f"Результаты поиска «{escape(query)}»:" if results else f"Ничего не нашлось по «{escape(query)}»."
    await message.answer(text, reply_markup=b.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("exm:editname:"))
async def exm_edit_name(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    await state.update_data(exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.editing_name)
    await ui.safe_edit(
        callback,
        f"Текущее название: <b>{escape(ex['name'])}</b>\n\nНапиши новое название упражнения:",
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


@router.callback_query(
    StateFilter(ExerciseManage.picking_exercise, ExerciseManage.editing_name),
    F.data == "exm:backlist",
)
async def exm_back_to_list(callback: CallbackQuery, state: FSMContext):
    await _show_exercise_list(callback, state)


@router.callback_query(StateFilter(ExerciseManage.picking_exercise), F.data.startswith("exm:archiveask:"))
async def exm_archive_exercise_confirm(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    kb = keyboards.yes_no_keyboard(
        yes_cb=f"exm:archiveyes:{ex_id}",
        no_cb=f"exm:ex:{ex_id}",
        yes_text="🗑 Архивировать",
        no_text="❌ Отмена",
    )
    await ui.safe_edit(
        callback,
        f"Архивировать упражнение «{escape(ex['name'])}»? "
        "Оно пропадёт из списков, но история тренировок сохранится.",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(StateFilter(ExerciseManage.picking_exercise), F.data.startswith("exm:archiveyes:"))
async def exm_archive_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    await db.archive_exercise(ex_id)
    await callback.answer("Упражнение архивировано")
    await _show_exercise_list(callback, state)


@router.callback_query(F.data == "exm:newgroup")
async def exm_new_group(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ExerciseManage.new_group_name)
    await ui.safe_edit(
        callback,
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
