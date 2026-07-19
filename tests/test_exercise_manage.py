from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import ExerciseManage
from handlers import exercises

pytestmark = pytest.mark.asyncio


def _make_message(user_id: int, text: str):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.text = text
    message.answer = AsyncMock()
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
    text = callback.message.answer.await_args.args[0]
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(cb.startswith("prog:ex:") for cb in callback_datas)
    assert any(cb.startswith("exm:archiveask:") for cb in callback_datas)


# ---------- rename cancel returns to the exercise card ----------


async def test_edit_name_cancel_button_points_to_exercise_card(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)

    state = await _make_state(user_id, exm_group_id=group_id)
    callback = _make_exercise_callback(user_id, f"exm:editname:{ex_id}")
    await exercises.exm_edit_name(callback, state)

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert callback_datas == [f"exm:ex:{ex_id}"]


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
    assert callback_datas == [f"exm:ex:{ex_id}"]


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
