"""CRUD/browsing for muscle groups and exercises (the "⚙️ Упражнения" menu)."""

import datetime as dt
from contextlib import suppress
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import db
import exercise_descriptions
import exercise_media
import formatting
import keyboards
import ui
from fsm import ExerciseManage

router = Router(name="exercises")


async def _groups_payload(user_id: int):
    groups = await db.list_muscle_groups(user_id)
    b = InlineKeyboardBuilder()
    for g in groups:
        b.button(text=formatting.format_group(g["name"]), callback_data=f"exm:grp:{g['id']}")
    b.button(text="📋 Все", callback_data="exm:grp:all")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="➕ Новая группа", callback_data="exm:newgroup"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="exm:back"))
    return "⚙️ Упражнения — выбери группу мышц:", b.as_markup()


async def show_exercise_groups(callback: CallbackQuery, state: FSMContext):
    # Entering the exercises menu properly means any exercise card opened from
    # now on belongs to this flow again, not to wherever "⬅️ Назад" pointed
    # while jumping in from the AI-тренер chat (see send_exercise_card).
    await state.update_data(exm_from_ai=False)
    await state.set_state(ExerciseManage.picking_group)
    text, kb = await _groups_payload(callback.from_user.id)
    await ui.safe_edit(callback, text, reply_markup=kb)
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


def _exercise_list_label(ex) -> str:
    """Marks each exercise button with what its card actually has to show:
    📝 for a text description."""
    has_description = bool(exercise_descriptions.effective_description(ex))
    return f"📝 {ex['display_name']}" if has_description else ex["display_name"]


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
    items = [(f"exm:ex:{ex['id']}", _exercise_list_label(ex)) for ex in exercises]
    for row in keyboards.named_buttons(items):
        b.row(*row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"exm:page:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"exm:page:{page + 1}"))
    if nav:
        b.row(*nav)
    # Offered under "📋 Все" too: not having it there made that screen a dead end
    # for anyone who browsed all exercises, didn't find theirs, and had no way to
    # add it without backing out and guessing a group.
    b.row(InlineKeyboardButton(text="➕ Новое упражнение", callback_data="exm:newex"))
    if group is not None and group["user_id"] is not None:
        b.row(InlineKeyboardButton(text="🗑 Архивировать группу", callback_data=f"exm:archivegrpask:{group_id}"))
    b.row(
        InlineKeyboardButton(text="🗄 Архив", callback_data="exm:archivelist"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="exm:backgroups"),
    )
    title = formatting.format_group(group["name"]) if group is not None else "ВСЕ УПРАЖНЕНИЯ"
    title_html = f"<b>{escape(title)}</b>"
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
    data = await state.get_data()
    has_group = data.get("exm_group_id") is not None
    await state.set_state(ExerciseManage.creating_exercise_name)
    text = (
        "Напиши название нового упражнения или выбери из шаблонов:"
        if has_group
        else "Напиши название нового упражнения — группу мышц выберешь следом:"
    )
    await ui.safe_edit(
        callback, text, reply_markup=keyboards.new_exercise_entry_keyboard("exm", show_templates=has_group)
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
        "Напиши название нового упражнения или выбери из шаблонов:",
        reply_markup=keyboards.new_exercise_entry_keyboard("exm"),
    )
    await callback.answer()


@router.callback_query(
    StateFilter(ExerciseManage.creating_exercise_name, ExerciseManage.new_exercise_group),
    F.data == "exm:cancel",
)
async def exm_new_cancel(callback: CallbackQuery, state: FSMContext):
    await state.update_data(exm_new_name=None)
    await state.set_state(ExerciseManage.picking_exercise)
    await _show_exercise_list(callback, state)


@router.callback_query(StateFilter(ExerciseManage.creating_exercise_name), F.data.startswith("exm:tpl:"))
async def exm_preview_template(callback: CallbackQuery, state: FSMContext):
    """Tapping a template previews it (photo + info) — it isn't added until
    the user confirms with "➕ Добавить", since they may just want a look at
    what the exercise is before deciding."""
    template_id = int(callback.data.split(":")[2])
    template = await db.get_exercise(template_id)
    if template is None:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    text = _exercise_info_text(template, with_created=False)
    kb = keyboards.template_preview_keyboard(template_id)
    images = exercise_media.get_images(template["name"])
    with suppress(TelegramBadRequest):
        await callback.message.delete()
    if images:
        await callback.message.answer_photo(
            FSInputFile(images[0]), caption=text, reply_markup=kb, parse_mode="HTML"
        )
    else:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(
    StateFilter(ExerciseManage.creating_exercise_name, ExerciseManage.picking_exercise),
    F.data.startswith("exm:tpladd:"),
)
async def exm_add_template(callback: CallbackQuery, state: FSMContext):
    template_id = int(callback.data.split(":")[2])
    ex_id = await db.fork_exercise_from_template(callback.from_user.id, template_id)
    await state.update_data(exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.picking_exercise)
    ex = await db.get_exercise(ex_id)
    with suppress(TelegramBadRequest):
        await callback.message.delete()
    has_images = await _send_exercise_images(callback.message, ex, state)
    text, kb = await _exercise_detail_payload(ex, state, with_info=not has_images)
    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


async def _exm_finish_new_exercise_name(answerer, state: FSMContext, user_id: int, name: str):
    data = await state.get_data()
    group_id = data.get("exm_group_id")
    if group_id is None:
        # Reached from "📋 Все", where no group is selected — ask for it now that
        # the name is known, instead of refusing to offer creation at all.
        await state.update_data(exm_new_name=name)
        await state.set_state(ExerciseManage.new_exercise_group)
        groups = await db.list_muscle_groups(user_id)
        kb = keyboards.groups_keyboard(
            groups, prefix="exmnewgrp", extra_buttons=[("❌ Отмена", "exm:cancel")]
        )
        await answerer.answer(
            f"«{escape(name)}» — выбери группу мышц:", reply_markup=kb, parse_mode="HTML"
        )
        return
    # create_exercise переиспользует строку с таким же именем, в том числе
    # архивную, и возвращает её из архива — это правильно (иначе история
    # разошлась бы на два упражнения), но молча выглядит так, будто «новое»
    # упражнение почему-то приехало с чужим прошлым.
    revived = await db.find_exercise_by_display_name(user_id, db.build_display_name(name))
    was_archived = bool(revived and revived["is_archived"])
    ex_id = await db.create_exercise(user_id, name, group_id)
    await state.update_data(exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.picking_exercise)
    ex = await db.get_exercise(ex_id)
    text, kb = await _exercise_detail_payload(ex, state)
    if was_archived:
        text = (
            "🗄 Такое упражнение у тебя уже было — вернул из архива вместе с "
            "историей и рекордами.\n\n" + text
        )
    await answerer.answer(text, reply_markup=kb, parse_mode="HTML")


def _suspicious_name_reason(name: str) -> str | None:
    """None if `name` looks like a plausible exercise name; otherwise a short
    Russian phrase for the "are you sure?" prompt explaining why it doesn't —
    either a stray message (too long) or something with no letters at all
    ("50 12", a logged set typed while the bot was waiting for a name instead)."""
    if len(name) > config.MAX_EXERCISE_NAME_LENGTH:
        n = len(name)
        word = formatting.plural_ru(n, ("символ", "символа", "символов"))
        return f"длинновато для упражнения ({n} {word})"
    if not any(ch.isalpha() for ch in name):
        return "в названии нет ни одной буквы — не похоже на упражнение"
    return None


@router.message(StateFilter(ExerciseManage.creating_exercise_name), F.text)
async def exm_new_exercise_name_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.reply("Название не может быть пустым")
        return
    reason = _suspicious_name_reason(name)
    if reason:
        await state.update_data(exm_pending_long_name=name)
        kb = keyboards.yes_no_keyboard(
            yes_cb="exm:longname:yes", no_cb="exm:longname:no",
            yes_text="✅ Да, создать", no_text="✏️ Написать заново",
        )
        await message.reply(
            f"«{escape(name)}» — {reason}. Всё верно, создать такое?",
            reply_markup=kb, parse_mode="HTML",
        )
        return
    await _exm_finish_new_exercise_name(message, state, message.from_user.id, name)


@router.callback_query(StateFilter(ExerciseManage.creating_exercise_name), F.data == "exm:longname:yes")
async def exm_new_exercise_longname_confirmed(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    name = data.get("exm_pending_long_name")
    if not name:
        await callback.answer("Название потерялось, напиши заново", show_alert=True)
        return
    await state.update_data(exm_pending_long_name=None)
    with suppress(TelegramBadRequest):
        await callback.message.delete()
    await _exm_finish_new_exercise_name(callback.message, state, callback.from_user.id, name)
    await callback.answer()


@router.callback_query(StateFilter(ExerciseManage.creating_exercise_name), F.data == "exm:longname:no")
async def exm_new_exercise_longname_declined(callback: CallbackQuery, state: FSMContext):
    await state.update_data(exm_pending_long_name=None)
    with suppress(TelegramBadRequest):
        await callback.message.delete()
    await callback.message.answer(
        "Напиши название нового упражнения или выбери из шаблонов:",
        reply_markup=keyboards.new_exercise_entry_keyboard("exm"),
    )
    await callback.answer()


@router.callback_query(
    StateFilter(ExerciseManage.new_exercise_group), F.data.startswith("exmnewgrp:grp:")
)
async def exm_new_exercise_group_picked(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split(":")[2])
    data = await state.get_data()
    name = data.get("exm_new_name")
    if not name:
        await callback.answer("Название потерялось, начни заново", show_alert=True)
        await show_exercise_groups(callback, state)
        return
    ex_id = await db.create_exercise(callback.from_user.id, name, group_id)
    await state.update_data(exm_exercise_id=ex_id, exm_new_name=None)
    await state.set_state(ExerciseManage.picking_exercise)
    ex = await db.get_exercise(ex_id)
    text, kb = await _exercise_detail_payload(ex, state)
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


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
        f"Архивировать группу «{escape(formatting.format_group(group['name']))}»? "
        "Просто уберём все её упражнения из списка выбора — история тренировок с ними останется.",
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


def _exercise_info_text(ex, with_created: bool = True, group_name: str | None = None) -> str:
    # The name is the card's heading, not a labelled field — "Название:" in front
    # of it only says what is already obvious from it being first and bold.
    info = [f"<b>{escape(ex['name'])}</b>"]
    if group_name:
        info.append(f"Группа: {escape(formatting.format_group(group_name))}")
    if ex["equipment"]:
        info.append(f"Оснастка: {ex['equipment']}")
    if ex["unilateral"]:
        info.append("Одной рукой/ногой: да")
    if ex["attachment"]:
        info.append(f"Хват/насадка: {ex['attachment']}")
    if with_created:
        # Везде в боте дата по-русски («06.08.2026 (чт)»), а не ISO-обрубок.
        created = dt.datetime.fromisoformat(ex["created_at"])
        info.append(f"Создано: {formatting.format_date_ru(created)}")
    description = exercise_descriptions.effective_description(ex)
    if description:
        info.append(f"\n{escape(description)}")
    return "\n".join(info)


async def _exercise_group_name(ex) -> str | None:
    if not ex["primary_group_id"]:
        return None
    group = await db.get_muscle_group(ex["primary_group_id"])
    return group["name"] if group else None


async def _exercise_detail_payload(ex, state: FSMContext, with_info: bool = True):
    """_exercise_detail_view with the exercise's group name looked up for it and
    "⬅️ Назад" pointed wherever this card was actually reached from: closing it
    to reveal the AI-тренер reply underneath, rather than the exercises list."""
    data = await state.get_data()
    back_cb = "ai:closecard" if data.get("exm_from_ai") else "exm:backlist"
    return _exercise_detail_view(
        ex, with_info=with_info, group_name=await _exercise_group_name(ex), back_cb=back_cb
    )


def _exercise_detail_view(
    ex, with_info: bool = True, group_name: str | None = None, back_cb: str = "exm:backlist"
):
    b = InlineKeyboardBuilder()
    b.button(text="📈 Прогресс", callback_data=f"prog:ex:{ex['id']}:m")
    b.button(text="✏️ Редактировать", callback_data=f"exm:editmenu:{ex['id']}")
    b.button(text="📤 Поделиться", callback_data=f"share:ex:{ex['id']}")
    b.button(text="🗑 Архивировать", callback_data=f"exm:archiveask:{ex['id']}")
    b.button(text="⬅️ Назад", callback_data=back_cb)
    b.adjust(2, 2, 1)
    # Even when the details went out as a photo caption, the button screen keeps
    # the name: the photo can scroll out of view, and a bare "Управление
    # упражнением:" doesn't say which exercise the buttons act on.
    text = (
        _exercise_info_text(ex, group_name=group_name)
        if with_info
        else f"<b>{escape(ex['display_name'])}</b>\nУправление упражнением:"
    )
    return text, b.as_markup()


def _exercise_edit_menu_keyboard(ex) -> InlineKeyboardMarkup:
    """The "✏️ Редактировать" drill-down: renaming, changing group, description
    and photo are all edits — grouping them behind one button keeps the card
    itself down to Прогресс/Редактировать/Архивировать/Назад. Same ✏️ on all
    four (they're all "edit this field"), two per row; "Удалить фото" keeps
    its own 🗑, same as elsewhere in the app, since it's a delete, not an edit."""
    if ex["description"]:
        description_label = "✏️ Изменить описание"
    elif exercise_descriptions.catalog_description(ex):
        # A template default is already shown above — this writes a personal
        # override, so "Добавить" (as if nothing were there) would be misleading.
        description_label = "✏️ Своё описание"
    else:
        description_label = "✏️ Описание"
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Название", callback_data=f"exm:editname:{ex['id']}")
    b.button(text="✏️ Группа", callback_data=f"exm:editgroup:{ex['id']}")
    b.button(text=description_label, callback_data=f"exm:editdesc:{ex['id']}")
    b.button(text="✏️ Фото", callback_data=f"exm:addphoto:{ex['id']}")
    b.button(text="🔀 Объединить с другим", callback_data=f"exm:mergestart:{ex['id']}")
    if ex["custom_photo_file_id"]:
        b.button(text="🗑 Удалить фото", callback_data=f"exm:delphotoask:{ex['id']}")
        b.button(text="⬅️ Назад", callback_data=f"exm:ex:{ex['id']}")
        b.adjust(2, 2, 1, 1, 1)
    else:
        b.button(text="⬅️ Назад", callback_data=f"exm:ex:{ex['id']}")
        b.adjust(2, 2, 1, 1)
    return b.as_markup()


async def _exercise_edit_menu_payload(ex):
    """Same info text as the card, but with the edit-submenu keyboard — what
    "✏️ Редактировать" swaps to, and where every edit's "❌ Отмена" now returns
    (see exm:editmenu: callers below) instead of the full card."""
    group_name = await _exercise_group_name(ex)
    return _exercise_info_text(ex, group_name=group_name), _exercise_edit_menu_keyboard(ex)


@router.callback_query(F.data.startswith("exm:editmenu:"))
async def exm_edit_menu(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    # Also reachable as a "❌ Отмена" target from editing_name/editing_group/
    # editing_description/awaiting_photo — reset out of whichever of those got
    # us here, or a later typed message would be mistaken for another attempt.
    await state.set_state(ExerciseManage.picking_exercise)
    text, kb = await _exercise_edit_menu_payload(ex)
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


async def _send_exercise_images(message: Message, ex, state: FSMContext) -> bool:
    """Sends the exercise's reference photo(s) with the exercise info as
    caption. A user-uploaded custom photo takes priority over the bundled
    demo photos. Returns whether any were sent."""
    await _clear_exercise_media(message.bot, message.chat.id, state)
    group_name = await _exercise_group_name(ex)
    if ex["custom_photo_file_id"]:
        sent = await message.answer_photo(
            ex["custom_photo_file_id"],
            caption=formatting.clamp_caption(_exercise_info_text(ex, group_name=group_name)),
            parse_mode="HTML",
        )
        await state.update_data(exm_media_msg_ids=[sent.message_id])
        return True
    images = exercise_media.get_images_for(ex)
    if not images:
        return False
    media = [
        InputMediaPhoto(
            media=FSInputFile(images[0]),
            caption=formatting.clamp_caption(_exercise_info_text(ex, group_name=group_name)),
            parse_mode="HTML",
        )
    ]
    media += [InputMediaPhoto(media=FSInputFile(p)) for p in images[1:]]
    sent = await message.answer_media_group(media)
    await state.update_data(exm_media_msg_ids=[m.message_id for m in sent])
    return True


async def send_exercise_card(message: Message, state: FSMContext, user_id: int, ex_id: int) -> bool:
    """Posts the exercise card as a new message instead of editing one in place.

    Used from screens that must survive the jump — the AI-trainer chat, where
    editing would swallow the answer the exercise was mentioned in. Returns
    False if the exercise isn't the user's (or is gone).
    """
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != user_id:
        return False
    await state.set_state(ExerciseManage.picking_exercise)
    await state.update_data(exm_exercise_id=ex_id)
    has_images = await _send_exercise_images(message, ex, state)
    text, kb = await _exercise_detail_payload(ex, state, with_info=not has_images)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
    return True


async def _render_exercise_card(callback: CallbackQuery, state: FSMContext, ex_id: int) -> None:
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    await state.update_data(exm_exercise_id=ex_id)
    has_images = await _send_exercise_images(callback.message, ex, state)
    text, kb = await _exercise_detail_payload(ex, state, with_info=not has_images)
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(
    StateFilter(
        ExerciseManage.picking_exercise, ExerciseManage.editing_name,
        ExerciseManage.editing_group, ExerciseManage.editing_description,
        ExerciseManage.awaiting_photo,
    ),
    F.data.startswith("exm:ex:"),
)
async def exm_pick_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    await state.set_state(ExerciseManage.picking_exercise)
    await _render_exercise_card(callback, state, ex_id)


@router.callback_query(StateFilter(ExerciseManage.picking_exercise), F.data.startswith("exm:addphoto:"))
async def exm_add_photo(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    await state.update_data(exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.awaiting_photo)
    await ui.safe_edit(
        callback,
        "Пришли фото упражнения:",
        reply_markup=keyboards.cancel_keyboard(f"exm:editmenu:{ex_id}"),
    )
    await callback.answer()


@router.message(StateFilter(ExerciseManage.awaiting_photo))
async def exm_photo_entered(message: Message, state: FSMContext):
    if not message.photo:
        await message.reply("Пришли именно фото")
        return
    data = await state.get_data()
    ex_id = data["exm_exercise_id"]
    await db.set_exercise_photo(ex_id, message.photo[-1].file_id)
    await state.set_state(ExerciseManage.picking_exercise)
    ex = await db.get_exercise(ex_id)
    has_images = await _send_exercise_images(message, ex, state)
    text, kb = await _exercise_detail_payload(ex, state, with_info=not has_images)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(StateFilter(ExerciseManage.picking_exercise), F.data.startswith("exm:delphotoask:"))
async def exm_delete_photo_confirm(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id or not ex["custom_photo_file_id"]:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    kb = keyboards.yes_no_keyboard(
        yes_cb=f"exm:delphotoyes:{ex_id}",
        no_cb=f"exm:editmenu:{ex_id}",
        yes_text="🗑 Удалить",
        no_text="❌ Отмена",
    )
    await ui.safe_edit(callback, f"Удалить фото «{escape(ex['name'])}»?", reply_markup=kb)
    await callback.answer()


@router.callback_query(StateFilter(ExerciseManage.picking_exercise), F.data.startswith("exm:delphotoyes:"))
async def exm_delete_photo(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    await db.delete_exercise_photo(ex_id)
    await callback.answer("Фото удалено")
    ex = await db.get_exercise(ex_id)
    has_images = await _send_exercise_images(callback.message, ex, state)
    text, kb = await _exercise_detail_payload(ex, state, with_info=not has_images)
    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")


def _merge_target_keyboard(source_id: int, candidates) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    items = [(f"exm:mergepick:{ex['id']}", ex["display_name"]) for ex in candidates if ex["id"] != source_id]
    for row in keyboards.named_buttons(items):
        b.row(*row)
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"exm:editmenu:{source_id}"))
    return b.as_markup()


@router.callback_query(F.data.startswith("exm:mergestart:"))
async def exm_merge_start(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    await state.update_data(exm_merge_source_id=ex_id)
    await state.set_state(ExerciseManage.picking_merge_target)
    candidates = await db.search_exercises(callback.from_user.id, "")
    text = (
        f"С каким упражнением объединить «{escape(ex['display_name'])}»?\n"
        "Вся история (подходы, тренировки, программы) перейдёт в выбранное, "
        f"а «{escape(ex['display_name'])}» удалится. Выбери из списка или напиши название для поиска:"
    )
    await ui.safe_edit(callback, text, reply_markup=_merge_target_keyboard(ex_id, candidates), parse_mode="HTML")
    await callback.answer()


@router.message(StateFilter(ExerciseManage.picking_merge_target), F.text)
async def exm_merge_search_text(message: Message, state: FSMContext):
    query = message.text.strip()
    if not query:
        return
    data = await state.get_data()
    source_id = data["exm_merge_source_id"]
    candidates = await db.search_exercises(message.from_user.id, query)
    candidates = [ex for ex in candidates if ex["id"] != source_id]
    text = (
        f"Результаты поиска «{escape(query)}»:" if candidates
        else f"Ничего не нашлось по «{escape(query)}»."
    )
    await message.answer(text, reply_markup=_merge_target_keyboard(source_id, candidates))


@router.callback_query(StateFilter(ExerciseManage.picking_merge_target), F.data.startswith("exm:mergepick:"))
async def exm_merge_pick(callback: CallbackQuery, state: FSMContext):
    target_id = int(callback.data.split(":")[2])
    data = await state.get_data()
    source_id = data.get("exm_merge_source_id")
    source = await db.get_exercise(source_id) if source_id else None
    target = await db.get_exercise(target_id)
    if source is None or target is None or target["user_id"] != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    kb = keyboards.yes_no_keyboard(
        yes_cb=f"exm:mergeyes:{target_id}",
        no_cb=f"exm:mergestart:{source_id}",
        yes_text="🔀 Объединить",
        no_text="❌ Отмена",
    )
    await ui.safe_edit(
        callback,
        f"Объединить «{escape(source['display_name'])}» с «{escape(target['display_name'])}»?\n"
        f"Вся история «{escape(source['display_name'])}» перейдёт в «{escape(target['display_name'])}», "
        f"а «{escape(source['display_name'])}» будет удалено. Отменить это действие нельзя.",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(StateFilter(ExerciseManage.picking_merge_target), F.data.startswith("exm:mergeyes:"))
async def exm_merge_confirm(callback: CallbackQuery, state: FSMContext):
    target_id = int(callback.data.split(":")[2])
    data = await state.get_data()
    source_id = data.get("exm_merge_source_id")
    if source_id is None:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    outcome = await db.merge_exercises(callback.from_user.id, keep_id=target_id, drop_id=source_id)
    if outcome != db.MERGE_OK:
        # Причину называем: «не получилось» — ровно тот ответ, после которого
        # человек жмёт ту же кнопку ещё раз.
        await callback.answer(
            {
                db.MERGE_TARGET_ARCHIVED: (
                    "Это упражнение в архиве — вся история уехала бы туда же. "
                    "Верни его из «🗄 Архив» и попробуй снова."
                ),
                db.MERGE_IN_ACTIVE_WORKOUT: (
                    "Одно из этих упражнений сейчас в открытой тренировке. "
                    "Заверши её и объединяй."
                ),
            }.get(outcome, "Не получилось объединить"),
            show_alert=True,
        )
        return
    await state.update_data(exm_merge_source_id=None)
    await state.set_state(ExerciseManage.picking_exercise)
    await callback.answer("Упражнения объединены")
    await _render_exercise_card(callback, state, target_id)


@router.callback_query(F.data.startswith("prog:card:"))
async def prog_show_exercise_card(callback: CallbackQuery, state: FSMContext):
    """Jump straight to the exercise's management card from its progress screen,
    whatever state/flow got the user to that progress screen in the first place.

    `exm_from_ai` снимается здесь по той же причине, что и в
    show_exercise_groups: флаг означает «карточку открыли из чата тренера, и
    „Назад" должно просто закрыть её, обнажив ответ». После захода в прогресс
    ответа тренера рядом уже нет, а флаг оставался — и «Назад» удаляло
    сообщение, не открывая взамен ничего.
    """
    ex_id = int(callback.data.split(":")[2])
    await state.update_data(exm_from_ai=False)
    await state.set_state(ExerciseManage.picking_exercise)
    await _render_exercise_card(callback, state, ex_id)


@router.message(StateFilter(ExerciseManage.picking_exercise), F.text)
async def exm_search_text(message: Message, state: FSMContext):
    """Typing while browsing the exercise list searches instead of being silently dropped."""
    query = message.text.strip()
    if not query:
        return
    results = await db.search_exercises(message.from_user.id, query)
    templates = await db.search_exercise_templates(message.from_user.id, query)
    b = InlineKeyboardBuilder()
    items = [(f"exm:ex:{ex['id']}", ex["display_name"]) for ex in results]
    items += [(f"exm:tpladd:{t['id']}", f"📋 {t['display_name']}") for t in templates]
    for row in keyboards.named_buttons(items):
        b.row(*row)
    b.row(InlineKeyboardButton(text="➕ Новое упражнение", callback_data="exm:newex"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="exm:backlist"))
    text = (
        f"Результаты поиска «{escape(query)}»:" if (results or templates)
        else f"Ничего не нашлось по «{escape(query)}»."
    )
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
        reply_markup=keyboards.cancel_keyboard(f"exm:editmenu:{ex_id}"),
        parse_mode="HTML",
    )
    await callback.answer()


async def _exm_finish_rename(answerer, state: FSMContext, ex_id: int, name: str):
    ok = await db.update_exercise_name(ex_id, name)
    if not ok:
        await answerer.answer("У тебя уже есть упражнение с таким названием.")
        return
    await state.set_state(ExerciseManage.picking_exercise)
    ex = await db.get_exercise(ex_id)
    text, kb = await _exercise_detail_payload(ex, state)
    await answerer.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(StateFilter(ExerciseManage.editing_name), F.text)
async def exm_name_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.reply("Название не может быть пустым")
        return
    reason = _suspicious_name_reason(name)
    if reason:
        await state.update_data(exm_pending_long_rename=name)
        kb = keyboards.yes_no_keyboard(
            yes_cb="exm:longrename:yes", no_cb="exm:longrename:no",
            yes_text="✅ Да, переименовать", no_text="✏️ Написать заново",
        )
        await message.reply(
            f"«{escape(name)}» — {reason}. Всё верно, переименовать?",
            reply_markup=kb, parse_mode="HTML",
        )
        return
    data = await state.get_data()
    await _exm_finish_rename(message, state, data["exm_exercise_id"], name)


@router.callback_query(StateFilter(ExerciseManage.editing_name), F.data == "exm:longrename:yes")
async def exm_rename_longname_confirmed(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    name = data.get("exm_pending_long_rename")
    if not name:
        await callback.answer("Название потерялось, напиши заново", show_alert=True)
        return
    await state.update_data(exm_pending_long_rename=None)
    with suppress(TelegramBadRequest):
        await callback.message.delete()
    await _exm_finish_rename(callback.message, state, data["exm_exercise_id"], name)
    await callback.answer()


@router.callback_query(StateFilter(ExerciseManage.editing_name), F.data == "exm:longrename:no")
async def exm_rename_longname_declined(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(exm_pending_long_rename=None)
    ex_id = data["exm_exercise_id"]
    ex = await db.get_exercise(ex_id)
    with suppress(TelegramBadRequest):
        await callback.message.delete()
    await callback.message.answer(
        f"Текущее название: <b>{escape(ex['name'])}</b>\n\nНапиши новое название упражнения:",
        reply_markup=keyboards.cancel_keyboard(f"exm:editmenu:{ex_id}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("exm:editgroup:"))
async def exm_edit_group(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    await state.update_data(exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.editing_group)
    groups = await db.list_muscle_groups(callback.from_user.id)
    kb = keyboards.groups_keyboard(
        groups, prefix="exmeditgrp", extra_buttons=[("❌ Отмена", f"exm:editmenu:{ex_id}")]
    )
    current = await _exercise_group_name(ex)
    current_line = (
        f"Текущая группа: <b>{escape(formatting.format_group(current))}</b>\n\n" if current else ""
    )
    await ui.safe_edit(
        callback,
        f"{current_line}Выбери новую группу мышц для «{escape(ex['display_name'])}»:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(
    StateFilter(ExerciseManage.editing_group), F.data.startswith("exmeditgrp:grp:")
)
async def exm_edit_group_picked(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.split(":")[2]
    if raw == "all":
        await callback.answer("Выбери конкретную группу", show_alert=True)
        return
    group_id = int(raw)
    data = await state.get_data()
    ex_id = data.get("exm_exercise_id")
    ex = await db.get_exercise(ex_id) if ex_id else None
    if ex is None or ex["user_id"] != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    await db.update_exercise_group(ex_id, group_id)
    await state.set_state(ExerciseManage.picking_exercise)
    ex = await db.get_exercise(ex_id)
    await callback.answer("Группа изменена")
    text, kb = await _exercise_detail_payload(ex, state)
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("exm:editdesc:"))
async def exm_edit_description(callback: CallbackQuery, state: FSMContext):
    """Own exercises had nowhere to carry a technique description — only catalog
    templates did, via the static exercise_descriptions.py dict. This lets a
    user write one on their own exercise, same as a forked template already shows."""
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    await state.update_data(exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.editing_description)
    current = f"\n\nТекущее описание:\n<i>{escape(ex['description'])}</i>" if ex["description"] else ""
    await ui.safe_edit(
        callback,
        f"Напиши описание/технику выполнения для «{escape(ex['display_name'])}».{current}\n\n"
        "Пришли «-», чтобы убрать своё описание.",
        reply_markup=keyboards.cancel_keyboard(f"exm:editmenu:{ex_id}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(StateFilter(ExerciseManage.editing_description), F.text)
async def exm_description_entered(message: Message, state: FSMContext):
    description = message.text.strip()
    if len(description) > config.MAX_EXERCISE_DESCRIPTION_LENGTH:
        # Не обрезаем молча: человек написал это осознанно, и вернуть ему обрубок
        # хуже, чем сказать, сколько лишнего. Состояние остаётся — можно
        # переписать, не открывая карточку заново.
        await message.answer(
            f"Описание длиннее {config.MAX_EXERCISE_DESCRIPTION_LENGTH} символов "
            f"({len(description)}) — столько не влезает в подпись к фото. "
            "Сократи и пришли снова."
        )
        return
    data = await state.get_data()
    ex_id = data["exm_exercise_id"]
    await db.set_exercise_description(ex_id, None if description == "-" else description)
    await state.set_state(ExerciseManage.picking_exercise)
    ex = await db.get_exercise(ex_id)
    text, kb = await _exercise_detail_payload(ex, state)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(
    StateFilter(
        ExerciseManage.picking_exercise, ExerciseManage.editing_name, ExerciseManage.editing_description,
    ),
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
        "Просто уберём его из списка выбора — история тренировок с ним останется.",
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


@router.callback_query(F.data == "exm:archivelist")
async def exm_archive_list(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ExerciseManage.picking_exercise)
    exercises = await db.list_archived_exercises(callback.from_user.id)
    b = InlineKeyboardBuilder()
    items = [(f"exm:unarchive:{ex['id']}", ex["display_name"]) for ex in exercises]
    for row in keyboards.named_buttons(items):
        b.row(*row)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="exm:backgroups"))
    text = (
        "🗄 <b>Архив</b>\n\nНажми на упражнение, чтобы вернуть его в список."
        if exercises
        else "🗄 <b>Архив</b>\n\nЗдесь пока пусто — архивированные упражнения появятся тут."
    )
    await ui.safe_edit(callback, text, reply_markup=b.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("exm:unarchive:"))
async def exm_unarchive_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await callback.answer("Упражнение не найдено", show_alert=True)
        return
    await db.unarchive_exercise(ex_id)
    await callback.answer("Упражнение возвращено из архива")
    await exm_archive_list(callback, state)


@router.callback_query(F.data == "exm:newgroup")
async def exm_new_group(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ExerciseManage.new_group_name)
    await ui.safe_edit(
        callback,
        "Напиши название новой группы мышц:",
        reply_markup=keyboards.cancel_keyboard("exm:backgroups"),
    )
    await callback.answer()


@router.message(StateFilter(ExerciseManage.new_group_name), F.text)
async def exm_new_group_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.reply("Название не может быть пустым")
        return
    await db.create_muscle_group(message.from_user.id, name)
    # One screen, not three: the group list itself shows the new group, so the
    # separate "Группа «X» создана." and the placeholder it used to edit were
    # both just litter left in the chat.
    text, kb = await _groups_payload(message.from_user.id)
    await message.answer(text, reply_markup=kb)
    await state.set_state(ExerciseManage.picking_group)
