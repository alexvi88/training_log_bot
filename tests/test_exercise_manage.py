import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import config
from fsm import ExerciseManage
from handlers import exercises

pytestmark = pytest.mark.asyncio


def _make_message(user_id: int, text: str):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.text = text
    message.answer = AsyncMock()
    message.reply = AsyncMock()
    return message


def _make_upload_message(user_id: int, photo_file_id: str | None):
    """A Message mock for the awaiting_photo handler: either carries a photo
    (largest size last, matching Telegram's ordering) or none at all."""
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.chat = SimpleNamespace(id=user_id)
    message.photo = [SimpleNamespace(file_id=photo_file_id)] if photo_file_id else None
    message.reply = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=500))
    message.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=501))
    next_media_id = iter(range(1, 100))

    async def _answer_media_group(*args, **kwargs):
        return [SimpleNamespace(message_id=next(next_media_id)) for _ in range(2)]

    message.answer_media_group = AsyncMock(side_effect=_answer_media_group)
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    message.bot = bot
    return message


async def _make_state(user_id: int, **extra_data) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(ExerciseManage.picking_exercise)
    await state.update_data(exm_group_id=None)
    if extra_data:
        await state.update_data(**extra_data)
    return state


async def test_typing_in_exercise_list_searches_instead_of_being_ignored(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Bench press", group_id)
    await db.create_exercise(user_id, "Triceps pushdown", group_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    message = _make_message(user_id, "bench")

    await exercises.exm_search_text(message, state)

    kb = message.answer.await_args.kwargs["reply_markup"]
    button_texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "Bench press" in button_texts
    assert not any("Triceps" in t for t in button_texts)
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "exm:newex" in callback_datas


async def test_typing_no_match_in_exercise_list_shows_empty_state(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Bench press", group_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    message = _make_message(user_id, "squat")

    await exercises.exm_search_text(message, state)

    sent_text = message.answer.await_args.args[0]
    assert "Ничего не нашлось" in sent_text


def _make_exercise_callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=500))
    message.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=501))
    next_media_id = iter(range(1, 100))

    async def _answer_media_group(*args, **kwargs):
        return [SimpleNamespace(message_id=next(next_media_id)) for _ in range(2)]

    message.answer_media_group = AsyncMock(side_effect=_answer_media_group)
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    message.bot = bot
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id)
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def test_viewing_second_exercise_deletes_first_exercises_images(fresh_db, user_id):
    """_send_exercise_images (handlers/exercises.py) used to leave every
    previously sent media group in the chat as the user paged through
    exercise detail screens. Viewing a new exercise must clean up the
    previous one's photos instead of piling them up.
    """
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим гантелей лёжа", group_id)
    press = await db.create_exercise(user_id, "Жим ногами", group_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    callback1 = _make_exercise_callback(user_id, f"exm:ex:{bench}")
    await exercises.exm_pick_exercise(callback1, state)

    assert callback1.message.answer_media_group.await_count == 1
    assert callback1.bot.delete_message.await_count == 0

    callback2 = _make_exercise_callback(user_id, f"exm:ex:{press}")
    callback2.message.bot = callback1.message.bot  # same chat/bot across taps
    callback2.bot = callback1.bot
    await exercises.exm_pick_exercise(callback2, state)

    # The first exercise's two photo messages must be deleted before the
    # second exercise's photos are sent.
    assert callback1.bot.delete_message.await_count == 2
    deleted_ids = {c.args[1] for c in callback1.bot.delete_message.await_args_list}
    assert deleted_ids == {1, 2}
    assert callback2.message.answer_media_group.await_count == 1


async def test_returning_to_exercise_list_clears_pending_images(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим гантелей лёжа", group_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    callback = _make_exercise_callback(user_id, f"exm:ex:{bench}")
    await exercises.exm_pick_exercise(callback, state)
    assert callback.bot.delete_message.await_count == 0

    back_callback = _make_exercise_callback(user_id, "exm:backlist")
    back_callback.message.bot = callback.message.bot
    back_callback.bot = callback.bot
    await exercises.exm_back_to_list(back_callback, state)

    assert callback.bot.delete_message.await_count == 2


# ---------- template preview (doesn't add until confirmed) ----------


async def _template_id(db, group_name: str, exercise_name: str) -> int:
    groups = await db.list_muscle_groups(None, global_only=True)
    group_id = next(g["id"] for g in groups if g["name"] == group_name)
    templates = await db.list_templates_in_group(group_id)
    return next(t["id"] for t in templates if t["name"] == exercise_name)


async def test_tapping_template_previews_without_adding_it(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    template_id = await _template_id(db, "Спина", "Тяга гантели в наклоне")

    state = await _make_state(user_id, exm_group_id=group_id)
    await state.set_state(ExerciseManage.creating_exercise_name)
    callback = _make_exercise_callback(user_id, f"exm:tpl:{template_id}")

    await exercises.exm_preview_template(callback, state)

    assert callback.message.answer_photo.await_count == 1
    kb = callback.message.answer_photo.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"exm:tpladd:{template_id}" in callback_datas
    # not added yet
    assert await db.count_user_exercises(user_id) == 0


async def test_confirming_template_add_forks_and_shows_full_card(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    template_id = await _template_id(db, "Спина", "Тяга гантели в наклоне")

    state = await _make_state(user_id, exm_group_id=group_id)
    await state.set_state(ExerciseManage.creating_exercise_name)
    callback = _make_exercise_callback(user_id, f"exm:tpladd:{template_id}")

    await exercises.exm_add_template(callback, state)

    assert await db.count_user_exercises(user_id) == 1
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(cb.startswith("prog:ex:") for cb in callback_datas)
    assert any(cb.startswith("exm:archiveask:") for cb in callback_datas)


# ---------- groups screen layout ----------


async def test_groups_screen_gives_new_group_and_back_their_own_full_row(fresh_db, user_id):
    """Muscle groups and "📋 Все" pack two per row, but "➕ Новая группа" and
    "⬅️ Назад" are wide, one-per-row buttons — pairing them up made them look
    like just another pair of group buttons."""
    await fresh_db.create_muscle_group(user_id, "Грудь")
    await fresh_db.create_muscle_group(user_id, "Спина")

    _text, kb = await exercises._groups_payload(user_id)
    rows = kb.inline_keyboard

    assert len(rows[-2]) == 1 and rows[-2][0].text == "➕ Новая группа"
    assert len(rows[-1]) == 1 and rows[-1][0].text == "⬅️ Назад"


# ---------- archive / unarchive ----------


async def test_archive_list_shows_empty_state_with_nothing_archived(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Жим лёжа", group_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    callback = _make_exercise_callback(user_id, "exm:archivelist")

    await exercises.exm_archive_list(callback, state)

    text = callback.message.answer.await_args.args[0]
    assert "пусто" in text.lower()


async def test_archive_list_offers_archived_exercises(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    await db.archive_exercise(ex_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    callback = _make_exercise_callback(user_id, "exm:archivelist")

    await exercises.exm_archive_list(callback, state)

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"exm:unarchive:{ex_id}" in callback_datas


async def test_unarchive_restores_exercise_to_normal_list(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    await db.archive_exercise(ex_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    callback = _make_exercise_callback(user_id, f"exm:unarchive:{ex_id}")

    await exercises.exm_unarchive_exercise(callback, state)

    ex = await db.get_exercise(ex_id)
    assert ex["is_archived"] == 0
    assert ex_id in {e["id"] for e in await db.list_user_exercises(user_id)}
    assert ex_id not in {e["id"] for e in await db.list_archived_exercises(user_id)}


async def test_unarchive_rejects_someone_elses_exercise(fresh_db, user_id):
    db = fresh_db
    other_group = await db.create_muscle_group(999, "Грудь")
    other_ex = await db.create_exercise(999, "Жим лёжа", other_group)
    await db.archive_exercise(other_ex)

    state = await _make_state(user_id)
    callback = _make_exercise_callback(user_id, f"exm:unarchive:{other_ex}")

    await exercises.exm_unarchive_exercise(callback, state)

    callback.answer.assert_awaited_once_with("Упражнение не найдено", show_alert=True)
    ex = await db.get_exercise(other_ex)
    assert ex["is_archived"] == 1


# ---------- rename cancel returns to the exercise card ----------


async def test_edit_name_cancel_button_points_to_edit_menu(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    callback = _make_exercise_callback(user_id, f"exm:editname:{ex_id}")
    await exercises.exm_edit_name(callback, state)

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert callback_datas == [f"exm:editmenu:{ex_id}"]


async def test_cancelling_rename_shows_exercise_card_not_list(fresh_db, user_id):
    """Tapping cancel on the rename screen must land back on that exercise's
    card, not the group's exercise list — and it must reset the FSM state so
    a later message isn't mistaken for a rename."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    edit_callback = _make_exercise_callback(user_id, f"exm:editname:{ex_id}")
    await exercises.exm_edit_name(edit_callback, state)
    assert await state.get_state() == ExerciseManage.editing_name

    cancel_callback = _make_exercise_callback(user_id, f"exm:ex:{ex_id}")
    cancel_callback.message.bot = edit_callback.message.bot
    cancel_callback.bot = edit_callback.bot
    await exercises.exm_pick_exercise(cancel_callback, state)

    text = cancel_callback.message.answer.await_args.args[0]
    assert "Жим лёжа" in text
    assert await state.get_state() == ExerciseManage.picking_exercise


async def test_cancelling_rename_returns_to_the_edit_menu_not_the_card(fresh_db, user_id):
    """"❌ Отмена" on the rename prompt now points at exm:editmenu:, not the
    full card — cancelling an edit should drop you back where you picked it
    from, and reset the FSM state the same way exm:ex: does."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    edit_callback = _make_exercise_callback(user_id, f"exm:editname:{ex_id}")
    await exercises.exm_edit_name(edit_callback, state)
    assert await state.get_state() == ExerciseManage.editing_name

    cancel_callback = _make_exercise_callback(user_id, f"exm:editmenu:{ex_id}")
    cancel_callback.message.bot = edit_callback.message.bot
    cancel_callback.bot = edit_callback.bot
    await exercises.exm_edit_menu(cancel_callback, state)

    assert await state.get_state() == ExerciseManage.picking_exercise
    kb = cancel_callback.message.answer.await_args.kwargs["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "✏️ Название" in labels


# ---------- muscle group edit ----------


async def test_edit_group_button_shows_group_picker(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    other_group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    callback = _make_exercise_callback(user_id, f"exm:editgroup:{ex_id}")
    await exercises.exm_edit_group(callback, state)

    assert await state.get_state() == ExerciseManage.editing_group
    text = callback.message.answer.await_args.args[0]
    assert "ГРУДЬ" in text  # the group is always shown uppercase
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"exmeditgrp:grp:{other_group_id}" in callback_datas
    assert f"exm:editmenu:{ex_id}" in callback_datas


async def test_picking_a_group_moves_the_exercise_and_returns_to_its_card(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    other_group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)

    state = await _make_state(user_id, exm_group_id=group_id, exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.editing_group)
    callback = _make_exercise_callback(user_id, f"exmeditgrp:grp:{other_group_id}")

    await exercises.exm_edit_group_picked(callback, state)

    ex = await db.get_exercise(ex_id)
    assert ex["primary_group_id"] == other_group_id
    assert await state.get_state() == ExerciseManage.picking_exercise
    callback.answer.assert_awaited_once_with("Группа изменена")
    text = callback.message.answer.await_args.args[0]
    assert "Группа: СПИНА" in text


async def test_edit_group_rejects_someone_elses_exercise(fresh_db, user_id):
    db = fresh_db
    other_group = await db.create_muscle_group(999, "Грудь")
    other_ex = await db.create_exercise(999, "Жим лёжа", other_group)

    state = await _make_state(user_id)
    callback = _make_exercise_callback(user_id, f"exm:editgroup:{other_ex}")

    await exercises.exm_edit_group(callback, state)

    callback.answer.assert_awaited_once_with("Упражнение не найдено", show_alert=True)


# ---------- exercise photo upload ----------


async def test_add_photo_button_prompts_and_sets_state(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Становая тяга", group_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    callback = _make_exercise_callback(user_id, f"exm:addphoto:{ex_id}")
    await exercises.exm_add_photo(callback, state)

    assert await state.get_state() == ExerciseManage.awaiting_photo
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert callback_datas == [f"exm:editmenu:{ex_id}"]


async def test_sending_photo_stores_it_and_returns_to_card(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Становая тяга", group_id)

    state = await _make_state(user_id, exm_group_id=group_id, exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.awaiting_photo)
    message = _make_upload_message(user_id, "FILE_ID_ABC")

    await exercises.exm_photo_entered(message, state)

    ex = await db.get_exercise(ex_id)
    assert ex["custom_photo_file_id"] == "FILE_ID_ABC"
    assert await state.get_state() == ExerciseManage.picking_exercise
    message.answer_photo.assert_awaited_once()
    assert message.answer_photo.await_args.args[0] == "FILE_ID_ABC"
    caption = message.answer_photo.await_args.kwargs["caption"]
    assert "Становая тяга" in caption
    text = message.answer.await_args.args[0]
    assert "Управление упражнением" in text


async def test_sending_text_instead_of_photo_asks_again(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Становая тяга", group_id)

    state = await _make_state(user_id, exm_group_id=group_id, exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.awaiting_photo)
    message = _make_upload_message(user_id, None)

    await exercises.exm_photo_entered(message, state)

    ex = await db.get_exercise(ex_id)
    assert ex["custom_photo_file_id"] is None
    message.reply.assert_awaited_once()
    assert await state.get_state() == ExerciseManage.awaiting_photo


async def test_custom_photo_overrides_bundled_demo_photos(fresh_db, user_id):
    """"Жим гантелей лёжа" has bundled demo photos — a custom upload must win."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим гантелей лёжа", group_id)
    await db.set_exercise_photo(ex_id, "CUSTOM_FILE_ID")

    state = await _make_state(user_id, exm_group_id=group_id)
    callback = _make_exercise_callback(user_id, f"exm:ex:{ex_id}")
    await exercises.exm_pick_exercise(callback, state)

    callback.message.answer_photo.assert_awaited_once()
    assert callback.message.answer_photo.await_args.args[0] == "CUSTOM_FILE_ID"
    callback.message.answer_media_group.assert_not_awaited()


# ---------- own exercise description (same role as exercise_descriptions.py for templates) ----------


async def test_card_offers_add_description_for_a_plain_custom_exercise():
    ex = {"id": 1, "name": "pull down", "display_name": "pull down", "description": None, "custom_photo_file_id": None}
    kb = exercises._exercise_edit_menu_keyboard(ex)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "✏️ Описание" in labels


async def test_card_offers_write_own_when_a_template_default_already_shows():
    ex = {"id": 1, "name": "Присед со штангой", "display_name": "Присед со штангой", "description": None, "custom_photo_file_id": None}
    kb = exercises._exercise_edit_menu_keyboard(ex)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "✏️ Своё описание" in labels
    assert "✏️ Описание" not in labels


async def test_card_offers_edit_description_once_the_user_has_one():
    ex = {
        "id": 1, "name": "pull down", "display_name": "pull down",
        "description": "Тяни к низу груди", "custom_photo_file_id": None,
    }
    kb = exercises._exercise_edit_menu_keyboard(ex)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "✏️ Изменить описание" in labels


async def test_edit_menu_layout_is_two_buttons_per_row():
    ex = {"id": 1, "name": "pull down", "display_name": "pull down", "description": None, "custom_photo_file_id": None}
    kb = exercises._exercise_edit_menu_keyboard(ex)
    rows = kb.inline_keyboard
    assert [b.text for b in rows[0]] == ["✏️ Название", "✏️ Группа"]
    assert [b.text for b in rows[1]] == ["✏️ Описание", "✏️ Фото"]
    assert [b.text for b in rows[2]] == ["🔀 Объединить с другим"]
    assert [b.text for b in rows[3]] == ["⬅️ Назад"]


async def test_card_layout_is_prog_edit_share_archive_back():
    """Верхний уровень карточки — две пары (Прогресс/Редактировать,
    Поделиться/Архивировать) и Назад своей строкой; конкретные правки
    (название, группа, описание, фото) спрятаны за "Редактировать"."""
    ex = {"id": 1, "name": "pull down", "display_name": "pull down", "description": None, "custom_photo_file_id": None}
    _text, kb = exercises._exercise_detail_view(ex, with_info=False)
    rows = kb.inline_keyboard
    assert [b.text for b in rows[0]] == ["📈 Прогресс", "✏️ Редактировать"]
    assert [b.text for b in rows[1]] == ["📤 Поделиться", "🗑 Архивировать"]
    assert [b.text for row in rows[2:] for b in row] == ["⬅️ Назад"]


async def test_edit_menu_offers_delete_photo_button_when_one_exists():
    ex = {
        "id": 1, "name": "pull down", "display_name": "pull down",
        "description": None, "custom_photo_file_id": "FILE_ID",
    }
    kb = exercises._exercise_edit_menu_keyboard(ex)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "🗑 Удалить фото" in labels
    rows = kb.inline_keyboard
    assert [b.text for b in rows[2]] == ["🔀 Объединить с другим"]
    assert [b.text for b in rows[3]] == ["🗑 Удалить фото"]
    assert [b.text for b in rows[4]] == ["⬅️ Назад"]


async def test_photo_button_label_is_the_same_whether_or_not_one_exists():
    """No more "Добавить"/"Заменить" split — always just "Фото", same ✏️ as
    every other edit button."""
    without_photo = {"id": 1, "name": "pull down", "display_name": "pull down", "description": None, "custom_photo_file_id": None}
    with_photo = {"id": 1, "name": "pull down", "display_name": "pull down", "description": None, "custom_photo_file_id": "FILE_ID"}
    for ex in (without_photo, with_photo):
        kb = exercises._exercise_edit_menu_keyboard(ex)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        assert "✏️ Фото" in labels


async def test_edit_description_button_prompts_and_sets_state(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "pull down", group_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    callback = _make_exercise_callback(user_id, f"exm:editdesc:{ex_id}")
    await exercises.exm_edit_description(callback, state)

    assert await state.get_state() == ExerciseManage.editing_description
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert callback_datas == [f"exm:editmenu:{ex_id}"]


async def test_sending_description_stores_it_and_returns_to_card(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "pull down", group_id)

    state = await _make_state(user_id, exm_group_id=group_id, exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.editing_description)
    message = _make_message(user_id, "Тяни рукоять к низу груди, локти вниз")

    await exercises.exm_description_entered(message, state)

    ex = await db.get_exercise(ex_id)
    assert ex["description"] == "Тяни рукоять к низу груди, локти вниз"
    assert await state.get_state() == ExerciseManage.picking_exercise
    text = message.answer.await_args.args[0]
    assert "Тяни рукоять к низу груди" in text


async def test_sending_dash_clears_the_description(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "pull down", group_id)
    await db.set_exercise_description(ex_id, "Старое описание")

    state = await _make_state(user_id, exm_group_id=group_id, exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.editing_description)
    message = _make_message(user_id, "-")

    await exercises.exm_description_entered(message, state)

    ex = await db.get_exercise(ex_id)
    assert ex["description"] is None


async def test_own_description_overrides_template_default_in_the_card_text(fresh_db, user_id):
    """"Присед со штангой" has a built-in template description — a personal
    override must win over it, not just add to it."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Присед со штангой", group_id)
    default_text = exercises._exercise_info_text(await db.get_exercise(ex_id))
    first_step = "1. Установите штангу"
    assert first_step in default_text  # sanity check: the template default was actually shown

    await db.set_exercise_description(ex_id, "Моя версия — колени наружу")
    overridden_text = exercises._exercise_info_text(await db.get_exercise(ex_id))

    assert "Моя версия — колени наружу" in overridden_text
    # A bare "1." also matches the "Создано: 11.08.2026" line the card carries,
    # so this checks for the actual template step text going away, not a digit.
    assert first_step not in overridden_text  # the numbered template steps are gone, not appended


async def test_created_date_is_shown_russian_style_not_iso(fresh_db, user_id):
    """Regression: the card used to print the raw ISO prefix ("Создано:
    2026-08-06") while every other date in the bot reads "06.08.2026 (чт)"."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим штанги лёжа", group_id)

    text = exercises._exercise_info_text(await db.get_exercise(ex_id))

    assert "Создано: 2026-" not in text
    assert re.search(r"Создано: \d{2}\.\d{2}\.\d{4} \(\w+\)", text)


# ---------- creating an exercise from "📋 Все" (no group selected) ----------


async def test_new_exercise_from_all_asks_for_the_group_after_the_name(fresh_db, user_id):
    """"📋 Все" used to be a dead end: no "➕ Новое упражнение" at all, so the
    only way to add one was backing out and guessing which group it belongs to."""
    state = await _make_state(user_id)  # exm_group_id is None — the "Все" view
    message = _make_message(user_id, "Barbell Row")

    await exercises.exm_new_exercise_name_entered(message, state)

    assert await state.get_state() == ExerciseManage.new_exercise_group
    assert (await state.get_data())["exm_new_name"] == "Barbell Row"
    # Nothing created yet — the group question comes first.
    assert not [e for e in await fresh_db.list_user_exercises(user_id)]
    sent = message.answer.await_args
    text = sent.args[0] if sent.args else sent.kwargs["text"]
    assert "Barbell Row" in text and "группу мышц" in text


_STRAY_MESSAGE = "Саня я буквально сейчас иду в зал купить protein bar по дороге"


async def test_absurdly_long_name_asks_for_confirmation_instead_of_creating(fresh_db, user_id):
    """A stray message typed while the bot happened to be waiting for an exercise
    name (e.g. meant for someone else in the chat) shouldn't silently become a
    logged exercise — but it might genuinely be a long name, so ask first."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    state = await _make_state(user_id, exm_group_id=group_id)
    await state.set_state(ExerciseManage.creating_exercise_name)
    message = _make_message(user_id, _STRAY_MESSAGE)

    await exercises.exm_new_exercise_name_entered(message, state)

    assert await db.count_user_exercises(user_id) == 0
    message.reply.assert_awaited_once()
    assert await state.get_state() == ExerciseManage.creating_exercise_name
    assert (await state.get_data())["exm_pending_long_name"] == _STRAY_MESSAGE


async def test_a_logged_set_typed_as_a_name_asks_for_confirmation(fresh_db, user_id):
    """A set like "50 12" typed while the bot was waiting for an exercise name
    (meant to log a set on an already-open exercise, or a stray "5x5"-style
    program note) shouldn't silently become an exercise called "50 12"."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    state = await _make_state(user_id, exm_group_id=group_id)
    await state.set_state(ExerciseManage.creating_exercise_name)
    message = _make_message(user_id, "50 12")

    await exercises.exm_new_exercise_name_entered(message, state)

    assert await db.count_user_exercises(user_id) == 0
    message.reply.assert_awaited_once()
    assert (await state.get_data())["exm_pending_long_name"] == "50 12"
    text = message.reply.await_args.args[0]
    assert "ни одной буквы" in text


async def test_confirming_a_long_name_creates_the_exercise(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    state = await _make_state(
        user_id, exm_group_id=group_id, exm_pending_long_name=_STRAY_MESSAGE,
    )
    await state.set_state(ExerciseManage.creating_exercise_name)
    callback = _make_exercise_callback(user_id, "exm:longname:yes")

    await exercises.exm_new_exercise_longname_confirmed(callback, state)

    ex = await db.get_exercise((await state.get_data())["exm_exercise_id"])
    assert ex["name"] == _STRAY_MESSAGE


async def test_declining_a_long_name_reprompts_without_creating(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    state = await _make_state(
        user_id, exm_group_id=group_id, exm_pending_long_name=_STRAY_MESSAGE,
    )
    await state.set_state(ExerciseManage.creating_exercise_name)
    callback = _make_exercise_callback(user_id, "exm:longname:no")

    await exercises.exm_new_exercise_longname_declined(callback, state)

    assert await db.count_user_exercises(user_id) == 0
    assert (await state.get_data())["exm_pending_long_name"] is None
    assert await state.get_state() == ExerciseManage.creating_exercise_name


async def test_absurdly_long_rename_asks_for_confirmation(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "pull down", group_id)

    state = await _make_state(user_id, exm_group_id=group_id, exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.editing_name)
    message = _make_message(user_id, _STRAY_MESSAGE)

    await exercises.exm_name_entered(message, state)

    ex = await db.get_exercise(ex_id)
    assert ex["name"] == "pull down"
    message.reply.assert_awaited_once()
    assert (await state.get_data())["exm_pending_long_rename"] == _STRAY_MESSAGE


async def test_confirming_a_long_rename_applies_it(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "pull down", group_id)

    state = await _make_state(
        user_id, exm_group_id=group_id, exm_exercise_id=ex_id, exm_pending_long_rename=_STRAY_MESSAGE,
    )
    await state.set_state(ExerciseManage.editing_name)
    callback = _make_exercise_callback(user_id, "exm:longrename:yes")

    await exercises.exm_rename_longname_confirmed(callback, state)

    ex = await db.get_exercise(ex_id)
    assert ex["name"] == _STRAY_MESSAGE


async def test_declining_a_long_rename_keeps_the_old_name(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "pull down", group_id)

    state = await _make_state(
        user_id, exm_group_id=group_id, exm_exercise_id=ex_id, exm_pending_long_rename=_STRAY_MESSAGE,
    )
    await state.set_state(ExerciseManage.editing_name)
    callback = _make_exercise_callback(user_id, "exm:longrename:no")

    await exercises.exm_rename_longname_declined(callback, state)

    ex = await db.get_exercise(ex_id)
    assert ex["name"] == "pull down"
    assert (await state.get_data())["exm_pending_long_rename"] is None


async def test_picking_the_group_creates_the_exercise(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    state = await _make_state(user_id)
    await state.set_state(ExerciseManage.new_exercise_group)
    await state.update_data(exm_new_name="Barbell Row")

    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.data = f"exmnewgrp:grp:{group_id}"
    callback.answer = AsyncMock()
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=user_id)
    msg.message_id = 7
    msg.text = "экран"
    msg.delete = AsyncMock()
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=8))
    callback.message = msg

    await exercises.exm_new_exercise_group_picked(callback, state)

    created = [e for e in await db.list_user_exercises(user_id) if e["display_name"] == "Barbell Row"]
    assert len(created) == 1
    assert created[0]["primary_group_id"] == group_id
    assert await state.get_state() == ExerciseManage.picking_exercise


async def test_group_creation_leaves_a_single_screen(fresh_db, user_id):
    """It used to send "Группа «X» создана." plus a placeholder it then edited —
    two stray messages per group."""
    state = await _make_state(user_id)
    await state.set_state(ExerciseManage.new_group_name)
    message = _make_message(user_id, "Предплечья")

    await exercises.exm_new_group_entered(message, state)

    assert message.answer.await_count == 1
    text = message.answer.await_args.args[0]
    assert "выбери группу мышц" in text
    groups = {g["name"] for g in await fresh_db.list_muscle_groups(user_id)}
    assert "Предплечья" in groups


async def test_an_overlong_description_is_refused_not_stored(fresh_db, user_id):
    """Описание уезжает в подпись к фото, а у подписи лимит 1024, не 4096.
    Проверки не было нигде, и карточка упражнения с фото после длинного описания
    падала при каждом открытии — пока человек не догадается сократить текст,
    которого он больше не видит.

    Отказ, а не молчаливая обрезка: человек написал это осознанно, и вернуть ему
    обрубок хуже, чем сказать, сколько лишнего.
    """
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "pull down", group_id)

    state = await _make_state(user_id, exm_group_id=group_id, exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.editing_description)
    message = _make_message(user_id, "я" * (config.MAX_EXERCISE_DESCRIPTION_LENGTH + 1))

    await exercises.exm_description_entered(message, state)

    assert (await db.get_exercise(ex_id))["description"] is None
    # Состояние остаётся: можно переписать, не открывая карточку заново.
    assert await state.get_state() == ExerciseManage.editing_description
    reply = message.answer.await_args.args[0]
    assert str(config.MAX_EXERCISE_DESCRIPTION_LENGTH) in reply
    assert str(config.MAX_EXERCISE_DESCRIPTION_LENGTH + 1) in reply


async def test_a_description_at_the_limit_still_goes_through(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "pull down", group_id)

    state = await _make_state(user_id, exm_group_id=group_id, exm_exercise_id=ex_id)
    await state.set_state(ExerciseManage.editing_description)
    text = "я" * config.MAX_EXERCISE_DESCRIPTION_LENGTH
    await exercises.exm_description_entered(_make_message(user_id, text), state)

    assert (await db.get_exercise(ex_id))["description"] == text


async def test_long_name_warning_declines_symbol_count_correctly():
    """Раньше было жёсткое «81 символов» через f-строку — теперь
    plural_ru согласует число со словом («81 символ», «82 символа»)."""
    reason_81 = exercises._suspicious_name_reason("я" * 81)
    assert reason_81 == "длинновато для упражнения (81 символ)"
    reason_82 = exercises._suspicious_name_reason("я" * 82)
    assert reason_82 == "длинновато для упражнения (82 символа)"
    reason_85 = exercises._suspicious_name_reason("я" * 85)
    assert reason_85 == "длинновато для упражнения (85 символов)"
