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
import i18n
import keyboards
import seed_data
import ui
from fsm import ExerciseManage

router = Router(name="exercises")


def _group_display_name(name: str) -> str:
    """Group name for display: preset groups (`muscle_groups.user_id IS NULL`)
    localize through `seed_data.localized_muscle_group_name` the same way a
    template exercise's name does (see `_template_display_name` below) —
    the row itself stays Russian forever, only the render picks a language.
    A user's own custom group has no slug and comes back unchanged."""
    return formatting.format_group(seed_data.localized_muscle_group_name(name, i18n.get_lang()))


async def _groups_payload(user_id: int):
    groups = await db.list_muscle_groups(user_id)
    b = InlineKeyboardBuilder()
    for g in groups:
        b.button(text=_group_display_name(g["name"]), callback_data=f"exm:grp:{g['id']}")
    b.button(text=i18n.t("btn.all_templates"), callback_data="exm:grp:all")
    b.adjust(2)
    b.row(InlineKeyboardButton(text=i18n.t("exercises.btn.new_group"), callback_data="exm:newgroup"))
    b.row(InlineKeyboardButton(text=i18n.t("btn.back"), callback_data="exm:back"))
    text = i18n.t("exercises.groups.intro")
    return text, b.as_markup()


async def show_exercise_groups(callback: CallbackQuery, state: FSMContext):
    # Entering the exercises menu properly means any exercise card opened from
    # now on belongs to this flow again, not to wherever "⬅️ Назад" pointed
    # while jumping in from the AI-тренер chat (see send_exercise_card).
    await state.update_data(exm_from_ai=False)
    await state.set_state(ExerciseManage.picking_group)
    text, kb = await _groups_payload(callback.from_user.id)
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
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


def _template_display_name(template) -> str:
    """A catalog template (`is_template=1`) is one shared row per exercise,
    never forked per user — so unlike an owned exercise, its `name`/
    `display_name` columns stay the Russian canonical text forever (see
    `seed_data.localized_muscle_group_name` docstring for the same pattern on
    groups). Showing `template['name']`/`template['display_name']` verbatim
    to an English reader would leak that Russian catalog key onto the
    screen — `db.fork_exercise_from_template` avoids exactly this by writing
    a localized `display_name` into the fork; here there is no fork yet, so
    we localize at render time instead."""
    return seed_data.localized_exercise_name(template["name"], i18n.get_lang())


def _localized_template_row(template) -> dict:
    """`template` with `name`/`display_name` swapped for the localized label,
    for reuse through `_exercise_info_text`/`_send_template_preview`, which
    otherwise read the Russian canonical text straight off the row."""
    data = dict(template)
    localized = _template_display_name(template)
    data["name"] = localized
    data["display_name"] = localized
    return data


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
    b.row(InlineKeyboardButton(text=i18n.t("exercises.btn.new_exercise"), callback_data="exm:newex"))
    if group is not None and group["user_id"] is not None:
        b.row(
            InlineKeyboardButton(
                text=i18n.t("exercises.btn.archive_group"), callback_data=f"exm:archivegrpask:{group_id}"
            )
        )
    b.row(
        InlineKeyboardButton(text=i18n.t("exercises.btn.archive"), callback_data="exm:archivelist"),
        InlineKeyboardButton(text=i18n.t("btn.back"), callback_data="exm:backgroups"),
    )
    title = _group_display_name(group["name"]) if group is not None else i18n.t("exercises.all_title")
    title_html = f"<b>{escape(title)}</b>"
    if exercises:
        text = f"{title_html}\n\n{i18n.t('exercises.list.intro')}"
    else:
        text = f"{title_html}\n\n{i18n.t('exercises.list.empty')}"
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
    text = i18n.t("exercises.new.prompt_with_templates" if has_group else "exercises.new.prompt_no_group")
    await ui.safe_edit(
        callback, text, reply_markup=keyboards.new_exercise_entry_keyboard("exm", show_templates=has_group)
    )
    await callback.answer()


def _localized_templates(templates) -> list[dict]:
    """Template rows for a keyboard, with `display_name` swapped for the
    render-time localized label — see `_template_display_name`."""
    return [{"id": t["id"], "display_name": _template_display_name(t)} for t in templates]


@router.callback_query(StateFilter(ExerciseManage.creating_exercise_name), F.data == "exm:templates")
async def exm_templates(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    templates = await db.list_templates_in_group(data["exm_group_id"])
    kb = keyboards.templates_keyboard(_localized_templates(templates), prefix="exm", back_cb="newback")
    text = i18n.t("exercises.templates.pick" if templates else "exercises.templates.empty")
    await ui.safe_edit(callback, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(StateFilter(ExerciseManage.creating_exercise_name), F.data == "exm:newback")
async def exm_new_back(callback: CallbackQuery, state: FSMContext):
    await ui.safe_edit(
        callback,
        i18n.t("exercises.new.prompt_with_templates"),
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
        await callback.answer(i18n.t("exercises.template_not_found"), show_alert=True)
        return
    text = _exercise_info_text(_localized_template_row(template), with_created=False)
    kb = keyboards.template_preview_keyboard(template_id)
    # Media lookup stays on the raw (Russian) template name — that's the
    # catalog key `exercise_media` is built on, not what gets shown.
    images = exercise_media.get_images(template["name"])
    with suppress(TelegramBadRequest):
        await callback.message.delete()
    # Rich-сообщение (Bot API 10.2, InputRichMessage) пробовали вместо
    # photo+caption — фиксить обрезку описания на 1024 символах. Живой
    # прогон: Telegram Web принимает отправку без ошибки, но фото молча
    # не показывает — хуже старого поведения, а не деградирует к нему,
    # и это никаким try/except на стороне бота не поймать. Откатили.
    await _send_template_preview(callback.message, _localized_template_row(template), text, kb, images)
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
            groups, prefix="exmnewgrp", extra_buttons=[(i18n.t("btn.cancel"), "exm:cancel")]
        )
        await answerer.answer(
            i18n.t("exercises.name.pick_group", name=escape(name)), reply_markup=kb, parse_mode="HTML"
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
        text = i18n.t("exercises.name.revived") + "\n\n" + text
    await answerer.answer(text, reply_markup=kb, parse_mode="HTML")


def _suspicious_name_reason(name: str) -> str | None:
    """None if `name` looks like a plausible exercise name; otherwise a short
    phrase for the "are you sure?" prompt explaining why it doesn't — either a
    stray message (too long) or something with no letters at all ("50 12", a
    logged set typed while the bot was waiting for a name instead)."""
    if len(name) > config.MAX_EXERCISE_NAME_LENGTH:
        return i18n.t("exercises.name_reason.too_long", n=len(name))
    if not any(ch.isalpha() for ch in name):
        return i18n.t("exercises.name_reason.no_letters")
    return None


@router.message(StateFilter(ExerciseManage.creating_exercise_name), F.text)
async def exm_new_exercise_name_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.reply(i18n.t("exercises.name.empty"))
        return
    reason = _suspicious_name_reason(name)
    if reason:
        await state.update_data(exm_pending_long_name=name)
        kb = keyboards.yes_no_keyboard(
            yes_cb="exm:longname:yes", no_cb="exm:longname:no",
            yes_text=i18n.t("exercises.btn.confirm_create"), no_text=i18n.t("exercises.btn.retype"),
        )
        await message.reply(
            i18n.t("exercises.name.confirm_create", name=escape(name), reason=reason),
            reply_markup=kb, parse_mode="HTML",
        )
        return
    await _exm_finish_new_exercise_name(message, state, message.from_user.id, name)


@router.callback_query(StateFilter(ExerciseManage.creating_exercise_name), F.data == "exm:longname:yes")
async def exm_new_exercise_longname_confirmed(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    name = data.get("exm_pending_long_name")
    if not name:
        await callback.answer(i18n.t("exercises.name.lost_retype"), show_alert=True)
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
        i18n.t("exercises.new.prompt_with_templates"),
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
        await callback.answer(i18n.t("exercises.name.lost_restart"), show_alert=True)
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
        await callback.answer(i18n.t("exercises.group.cant_archive"), show_alert=True)
        return
    kb = keyboards.yes_no_keyboard(
        yes_cb=f"exm:archivegrpyes:{group_id}",
        no_cb="exm:backlist",
        yes_text=i18n.t("exercises.btn.archive_yes"),
        no_text=i18n.t("btn.cancel"),
    )
    await ui.safe_edit(
        callback,
        i18n.t("exercises.group.archive_confirm", group=escape(_group_display_name(group["name"]))),
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("exm:archivegrpyes:"))
async def exm_archive_group(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split(":")[2])
    group = await db.get_muscle_group(group_id)
    if group is None or group["user_id"] != callback.from_user.id:
        await callback.answer(i18n.t("exercises.group.cant_archive"), show_alert=True)
        return
    await db.archive_muscle_group(group_id)
    await callback.answer(i18n.t("exercises.group.archived"))
    await show_exercise_groups(callback, state)


def _exercise_info_text(ex, with_created: bool = True, group_name: str | None = None) -> str:
    # The name is the card's heading, not a labelled field — a "Name:" in front
    # of it only says what is already obvious from it being first and bold.
    info = [f"<b>{escape(ex['name'])}</b>"]
    if group_name:
        info.append(i18n.t("exercises.info.group", group=escape(_group_display_name(group_name))))
    if ex["equipment"]:
        info.append(i18n.t("exercises.info.equipment", equipment=ex["equipment"]))
    if ex["unilateral"]:
        info.append(i18n.t("exercises.info.unilateral"))
    if ex["attachment"]:
        info.append(i18n.t("exercises.info.attachment", attachment=ex["attachment"]))
    if with_created:
        # format_date_ru — уже локализованный формат (i18n.t внутри), несмотря
        # на имя: «06.08.2026 (чт)» по-русски, «06.08.2026 (Thu)» по-английски.
        created = dt.datetime.fromisoformat(ex["created_at"])
        info.append(i18n.t("exercises.info.created", date=formatting.format_date_ru(created)))
    description = exercise_descriptions.effective_description(ex)
    if description:
        info.append(f"\n{escape(description)}")
    return "\n".join(info)


async def _exercise_group_name(ex) -> str | None:
    if not ex["primary_group_id"]:
        return None
    group = await db.get_muscle_group(ex["primary_group_id"])
    return group["name"] if group else None


async def _send_template_preview(message, template, text: str, kb, images: list[str]) -> None:
    """Предпросмотр шаблона: ОБЕ позиции упражнения плюс кнопки.

    Раньше отправлялось images[0] — одна картинка из двух, вторая молча
    выбрасывалась. Причина была техническая: у медиагруппы не может быть
    инлайн-клавиатуры, а тут нужны «Добавить» и «Назад», поэтому брали
    answer_photo, который держит и то и другое, но показывает один кадр. Для
    упражнения это половина смысла: пара — начальное и конечное положение, и
    без второго кадра не видно самого движения.

    Решение то же, каким уже сделана карточка упражнения (см.
    _send_exercise_images и _render_exercise_card): картинки уходят
    медиагруппой с описанием в подписи, а кнопки — следующим сообщением, где
    остаётся только название. Название там не для красоты: фото уезжает вверх
    при прокрутке, и голое «Управление» не говорит, к чему кнопки.
    """
    if not images:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
        return
    if len(images) == 1:
        await message.answer_photo(
            FSInputFile(images[0]), caption=text, reply_markup=kb, parse_mode="HTML"
        )
        return
    media = [
        InputMediaPhoto(
            media=FSInputFile(path),
            caption=formatting.clamp_caption(text) if i == 0 else None,
            parse_mode="HTML" if i == 0 else None,
        )
        for i, path in enumerate(images)
    ]
    await message.answer_media_group(media)
    await message.answer(
        f"<b>{escape(template['display_name'])}</b>", reply_markup=kb, parse_mode="HTML"
    )


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
    b.button(text=i18n.t("exercises.btn.progress"), callback_data=f"prog:ex:{ex['id']}:m")
    b.button(text=i18n.t("exercises.btn.edit"), callback_data=f"exm:editmenu:{ex['id']}")
    b.button(text=i18n.t("exercises.btn.share"), callback_data=f"share:ex:{ex['id']}")
    b.button(text=i18n.t("exercises.btn.archive_ex"), callback_data=f"exm:archiveask:{ex['id']}")
    b.button(text=i18n.t("btn.back"), callback_data=back_cb)
    b.adjust(2, 2, 1)
    # Even when the details went out as a photo caption, the button screen keeps
    # the name: the photo can scroll out of view, and a bare "Manage:" doesn't
    # say which exercise the buttons act on.
    text = (
        _exercise_info_text(ex, group_name=group_name)
        if with_info
        else f"<b>{escape(ex['display_name'])}</b>\n{i18n.t('exercises.manage_hint')}"
    )
    return text, b.as_markup()


def _exercise_edit_menu_keyboard(ex) -> InlineKeyboardMarkup:
    """The "✏️ Edit" drill-down: renaming, changing group, description and
    photo are all edits — grouping them behind one button keeps the card
    itself down to Progress/Edit/Archive/Back. Same ✏️ on all four (they're
    all "edit this field"), two per row; "Delete photo" keeps its own 🗑,
    same as elsewhere in the app, since it's a delete, not an edit."""
    if ex["description"]:
        description_label = i18n.t("exercises.btn.edit_description")
    elif exercise_descriptions.catalog_description(ex):
        # A template default is already shown above — this writes a personal
        # override, so "Add" (as if nothing were there) would be misleading.
        description_label = i18n.t("exercises.btn.own_description")
    else:
        description_label = i18n.t("exercises.btn.description")
    b = InlineKeyboardBuilder()
    b.button(text=i18n.t("exercises.btn.name"), callback_data=f"exm:editname:{ex['id']}")
    b.button(text=i18n.t("exercises.btn.group"), callback_data=f"exm:editgroup:{ex['id']}")
    b.button(text=description_label, callback_data=f"exm:editdesc:{ex['id']}")
    b.button(text=i18n.t("exercises.btn.photo"), callback_data=f"exm:addphoto:{ex['id']}")
    b.button(text=i18n.t("exercises.btn.merge"), callback_data=f"exm:mergestart:{ex['id']}")
    if ex["custom_photo_file_id"]:
        b.button(text=i18n.t("exercises.btn.delete_photo"), callback_data=f"exm:delphotoask:{ex['id']}")
        b.button(text=i18n.t("btn.back"), callback_data=f"exm:ex:{ex['id']}")
        b.adjust(2, 2, 1, 1, 1)
    else:
        b.button(text=i18n.t("btn.back"), callback_data=f"exm:ex:{ex['id']}")
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
        await ui.alert_exercise_not_found(callback)
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
        await ui.alert_exercise_not_found(callback)
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
        await ui.alert_exercise_not_found(callback)
        return
    await state.update_data(exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.awaiting_photo)
    await ui.safe_edit(
        callback,
        i18n.t("exercises.photo.prompt"),
        reply_markup=keyboards.cancel_keyboard(f"exm:editmenu:{ex_id}"),
    )
    await callback.answer()


@router.message(StateFilter(ExerciseManage.awaiting_photo))
async def exm_photo_entered(message: Message, state: FSMContext):
    if not message.photo:
        await message.reply(i18n.t("exercises.photo.need_photo"))
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
        await ui.alert_exercise_not_found(callback)
        return
    kb = keyboards.yes_no_keyboard(
        yes_cb=f"exm:delphotoyes:{ex_id}",
        no_cb=f"exm:editmenu:{ex_id}",
        yes_text=i18n.t("btn.delete"),
        no_text=i18n.t("btn.cancel"),
    )
    await ui.safe_edit(callback, i18n.t("exercises.photo.delete_confirm", name=escape(ex["name"])), reply_markup=kb)
    await callback.answer()


@router.callback_query(StateFilter(ExerciseManage.picking_exercise), F.data.startswith("exm:delphotoyes:"))
async def exm_delete_photo(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await ui.alert_exercise_not_found(callback)
        return
    await db.delete_exercise_photo(ex_id)
    await callback.answer(i18n.t("exercises.photo.removed"))
    ex = await db.get_exercise(ex_id)
    has_images = await _send_exercise_images(callback.message, ex, state)
    text, kb = await _exercise_detail_payload(ex, state, with_info=not has_images)
    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")


def _merge_target_keyboard(source_id: int, candidates) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    items = [(f"exm:mergepick:{ex['id']}", ex["display_name"]) for ex in candidates if ex["id"] != source_id]
    for row in keyboards.named_buttons(items):
        b.row(*row)
    b.row(InlineKeyboardButton(text=i18n.t("btn.cancel"), callback_data=f"exm:editmenu:{source_id}"))
    return b.as_markup()


@router.callback_query(F.data.startswith("exm:mergestart:"))
async def exm_merge_start(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await ui.alert_exercise_not_found(callback)
        return
    await state.update_data(exm_merge_source_id=ex_id)
    await state.set_state(ExerciseManage.picking_merge_target)
    candidates = await db.search_exercises(callback.from_user.id, "")
    text = i18n.t("exercises.merge.pick_target", name=escape(ex["display_name"]))
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
        i18n.t("exercises.search.results", query=escape(query)) if candidates
        else i18n.t("exercises.search.empty", query=escape(query))
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
        await ui.alert_exercise_not_found(callback)
        return
    kb = keyboards.yes_no_keyboard(
        yes_cb=f"exm:mergeyes:{target_id}",
        no_cb=f"exm:mergestart:{source_id}",
        yes_text=i18n.t("exercises.btn.merge_confirm"),
        no_text=i18n.t("btn.cancel"),
    )
    await ui.safe_edit(
        callback,
        i18n.t(
            "exercises.merge.confirm",
            source=escape(source["display_name"]), target=escape(target["display_name"]),
        ),
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(StateFilter(ExerciseManage.picking_merge_target), F.data.startswith("exm:mergeyes:"))
async def exm_merge_confirm(callback: CallbackQuery, state: FSMContext):
    target_id = int(callback.data.split(":")[2])
    data = await state.get_data()
    source_id = data.get("exm_merge_source_id")
    if source_id is None:
        await ui.alert_exercise_not_found(callback)
        return
    outcome = await db.merge_exercises(callback.from_user.id, keep_id=target_id, drop_id=source_id)
    if outcome != db.MERGE_OK:
        # Причину называем: «не получилось» — ровно тот ответ, после которого
        # человек жмёт ту же кнопку ещё раз.
        await callback.answer(
            {
                db.MERGE_TARGET_ARCHIVED: i18n.t("exercises.merge.error_archived"),
                db.MERGE_IN_ACTIVE_WORKOUT: i18n.t("exercises.merge.error_active_workout"),
            }.get(outcome, i18n.t("exercises.merge.error_generic")),
            show_alert=True,
        )
        return
    await state.update_data(exm_merge_source_id=None)
    await state.set_state(ExerciseManage.picking_exercise)
    await callback.answer(i18n.t("exercises.merge.done"))
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
    items += [(f"exm:tpladd:{t['id']}", f"📋 {_template_display_name(t)}") for t in templates]
    for row in keyboards.named_buttons(items):
        b.row(*row)
    b.row(InlineKeyboardButton(text=i18n.t("exercises.btn.new_exercise"), callback_data="exm:newex"))
    b.row(InlineKeyboardButton(text=i18n.t("btn.back"), callback_data="exm:backlist"))
    text = (
        i18n.t("exercises.search.results", query=escape(query)) if (results or templates)
        else i18n.t("exercises.search.empty", query=escape(query))
    )
    await message.answer(text, reply_markup=b.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("exm:editname:"))
async def exm_edit_name(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await ui.alert_exercise_not_found(callback)
        return
    await state.update_data(exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.editing_name)
    await ui.safe_edit(
        callback,
        i18n.t("exercises.rename.prompt", name=escape(ex["name"])),
        reply_markup=keyboards.cancel_keyboard(f"exm:editmenu:{ex_id}"),
        parse_mode="HTML",
    )
    await callback.answer()


async def _exm_finish_rename(answerer, state: FSMContext, ex_id: int, name: str):
    ok = await db.update_exercise_name(ex_id, name)
    if not ok:
        await answerer.answer(i18n.t("exercises.rename.taken"))
        return
    await state.set_state(ExerciseManage.picking_exercise)
    ex = await db.get_exercise(ex_id)
    text, kb = await _exercise_detail_payload(ex, state)
    await answerer.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(StateFilter(ExerciseManage.editing_name), F.text)
async def exm_name_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.reply(i18n.t("exercises.name.empty"))
        return
    reason = _suspicious_name_reason(name)
    if reason:
        await state.update_data(exm_pending_long_rename=name)
        kb = keyboards.yes_no_keyboard(
            yes_cb="exm:longrename:yes", no_cb="exm:longrename:no",
            yes_text=i18n.t("exercises.btn.confirm_rename"), no_text=i18n.t("exercises.btn.retype"),
        )
        await message.reply(
            i18n.t("exercises.name.confirm_rename", name=escape(name), reason=reason),
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
        await callback.answer(i18n.t("exercises.name.lost_retype"), show_alert=True)
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
        i18n.t("exercises.rename.prompt", name=escape(ex["name"])),
        reply_markup=keyboards.cancel_keyboard(f"exm:editmenu:{ex_id}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("exm:editgroup:"))
async def exm_edit_group(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await ui.alert_exercise_not_found(callback)
        return
    await state.update_data(exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.editing_group)
    groups = await db.list_muscle_groups(callback.from_user.id)
    kb = keyboards.groups_keyboard(
        groups, prefix="exmeditgrp", extra_buttons=[(i18n.t("btn.cancel"), f"exm:editmenu:{ex_id}")]
    )
    current = await _exercise_group_name(ex)
    current_line = (
        i18n.t("exercises.group.current", group=escape(_group_display_name(current))) + "\n\n"
        if current else ""
    )
    await ui.safe_edit(
        callback,
        f"{current_line}{i18n.t('exercises.group.pick_new', name=escape(ex['display_name']))}",
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
        await callback.answer(i18n.t("exercises.group.pick_specific"), show_alert=True)
        return
    group_id = int(raw)
    data = await state.get_data()
    ex_id = data.get("exm_exercise_id")
    ex = await db.get_exercise(ex_id) if ex_id else None
    if ex is None or ex["user_id"] != callback.from_user.id:
        await ui.alert_exercise_not_found(callback)
        return
    await db.update_exercise_group(ex_id, group_id)
    await state.set_state(ExerciseManage.picking_exercise)
    ex = await db.get_exercise(ex_id)
    await callback.answer(i18n.t("exercises.group.moved"))
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
        await ui.alert_exercise_not_found(callback)
        return
    await state.update_data(exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.editing_description)
    current = (
        i18n.t("exercises.description.current", text=escape(ex["description"])) if ex["description"] else ""
    )
    await ui.safe_edit(
        callback,
        i18n.t("exercises.description.prompt", name=escape(ex["display_name"]), current=current),
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
            i18n.t(
                "exercises.description.too_long",
                max=config.MAX_EXERCISE_DESCRIPTION_LENGTH, n=len(description),
            )
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
        await ui.alert_exercise_not_found(callback)
        return
    kb = keyboards.yes_no_keyboard(
        yes_cb=f"exm:archiveyes:{ex_id}",
        no_cb=f"exm:ex:{ex_id}",
        yes_text=i18n.t("exercises.btn.archive_yes"),
        no_text=i18n.t("btn.cancel"),
    )
    await ui.safe_edit(
        callback,
        i18n.t("exercises.archive.confirm", name=escape(ex["name"])),
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(StateFilter(ExerciseManage.picking_exercise), F.data.startswith("exm:archiveyes:"))
async def exm_archive_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await ui.alert_exercise_not_found(callback)
        return
    await db.archive_exercise(ex_id)
    await callback.answer(i18n.t("exercises.archive.done"))
    await _show_exercise_list(callback, state)


@router.callback_query(F.data == "exm:archivelist")
async def exm_archive_list(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ExerciseManage.picking_exercise)
    exercises = await db.list_archived_exercises(callback.from_user.id)
    b = InlineKeyboardBuilder()
    items = [(f"exm:unarchive:{ex['id']}", ex["display_name"]) for ex in exercises]
    for row in keyboards.named_buttons(items):
        b.row(*row)
    b.row(InlineKeyboardButton(text=i18n.t("btn.back"), callback_data="exm:backgroups"))
    text = i18n.t("exercises.archive.list" if exercises else "exercises.archive.empty")
    await ui.safe_edit(callback, text, reply_markup=b.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("exm:unarchive:"))
async def exm_unarchive_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    ex = await db.get_exercise(ex_id)
    if ex is None or ex["user_id"] != callback.from_user.id:
        await ui.alert_exercise_not_found(callback)
        return
    await db.unarchive_exercise(ex_id)
    await callback.answer(i18n.t("exercises.archive.restored"))
    await exm_archive_list(callback, state)


@router.callback_query(F.data == "exm:newgroup")
async def exm_new_group(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ExerciseManage.new_group_name)
    await ui.safe_edit(
        callback,
        i18n.t("exercises.new_group.prompt"),
        reply_markup=keyboards.cancel_keyboard("exm:backgroups"),
    )
    await callback.answer()


@router.message(StateFilter(ExerciseManage.new_group_name), F.text)
async def exm_new_group_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.reply(i18n.t("exercises.new_group.empty"))
        return
    await db.create_muscle_group(message.from_user.id, name)
    # One screen, not three: the group list itself shows the new group, so the
    # separate "Группа «X» создана." and the placeholder it used to edit were
    # both just litter left in the chat.
    text, kb = await _groups_payload(message.from_user.id)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
    await state.set_state(ExerciseManage.picking_group)
