from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import WorkoutFlow
from handlers import workout

pytestmark = pytest.mark.asyncio


def _stub_photo_sends(bot) -> None:
    """The live tracker pins a photo of the active exercise above itself, so any
    bot that renders the logging screen needs the photo-sending calls stubbed."""
    async def _send_media_group(*args, media, **kwargs):
        return [
            SimpleNamespace(message_id=700 + i, photo=[SimpleNamespace(file_id=f"fid_{i}")])
            for i, _ in enumerate(media)
        ]

    bot.send_media_group = AsyncMock(side_effect=_send_media_group)
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=710))


def _make_callback(user_id: int, data: str):
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=999))
    _stub_photo_sends(bot)
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id)
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int, **extra_data) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.picking_exercise)
    await state.update_data(
        workout_id=1, live_chat_id=user_id, live_message_id=1, pending_group_id=None,
        **extra_data,
    )
    return state


def _make_message(user_id: int, text: str):
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=999))
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.bot = bot
    message.text = text
    message.delete = AsyncMock()
    return message


_STRAY_MESSAGE = "Саня я буквально сейчас иду в зал купить protein bar по дороге"


async def test_absurdly_long_exercise_name_asks_for_confirmation(fresh_db, user_id):
    """A stray message typed while the bot happened to be waiting for a new
    exercise name (e.g. meant for someone else in the chat) shouldn't silently
    get recorded as an exercise — but it might genuinely be a long name, so
    ask first instead of blocking it outright."""
    db = fresh_db
    state = await _make_state(user_id)
    await state.set_state(WorkoutFlow.creating_exercise_name)
    message = _make_message(user_id, _STRAY_MESSAGE)

    await workout.new_exercise_name_entered(message, state)

    assert await db.count_user_exercises(user_id) == 0
    assert await state.get_state() == WorkoutFlow.creating_exercise_name
    assert (await state.get_data())["pending_long_exercise_name"] == _STRAY_MESSAGE
    message.bot.send_message.assert_awaited()
    hint = message.bot.send_message.await_args.kwargs["text"]
    assert _STRAY_MESSAGE in hint


async def test_a_logged_set_typed_as_a_name_asks_for_confirmation(fresh_db, user_id):
    """A set like "50 12" typed while the bot was waiting for a new exercise
    name shouldn't silently become an exercise called "50 12"."""
    db = fresh_db
    state = await _make_state(user_id)
    await state.set_state(WorkoutFlow.creating_exercise_name)
    message = _make_message(user_id, "50 12")

    await workout.new_exercise_name_entered(message, state)

    assert await db.count_user_exercises(user_id) == 0
    assert (await state.get_data())["pending_long_exercise_name"] == "50 12"
    hint = message.bot.send_message.await_args.kwargs["text"]
    assert "ни одной буквы" in hint


async def test_confirming_a_long_exercise_name_creates_it(fresh_db, user_id):
    db = fresh_db
    workout_id = await db.create_workout(user_id)
    state = await _make_state(
        user_id, open_exercises=[], open_blocks={}, active_exercise_id=None,
        pending_long_exercise_name=_STRAY_MESSAGE,
    )
    await state.update_data(workout_id=workout_id)
    await state.set_state(WorkoutFlow.creating_exercise_name)
    callback = _make_callback(user_id, "pick:longname:yes")

    await workout.pick_longname_confirmed(callback, state)

    assert await db.count_user_exercises(user_id) == 1
    ex = (await db.list_user_exercises(user_id))[0]
    assert ex["name"] == _STRAY_MESSAGE
    assert await state.get_state() == WorkoutFlow.logging_set


async def test_declining_a_long_exercise_name_does_not_create_it(fresh_db, user_id):
    db = fresh_db
    workout_id = await db.create_workout(user_id)
    state = await _make_state(
        user_id, open_exercises=[], open_blocks={}, active_exercise_id=None,
        pending_long_exercise_name=_STRAY_MESSAGE,
    )
    await state.update_data(workout_id=workout_id)
    await state.set_state(WorkoutFlow.creating_exercise_name)
    callback = _make_callback(user_id, "pick:longname:no")

    await workout.pick_longname_declined(callback, state)

    assert await db.count_user_exercises(user_id) == 0
    assert (await state.get_data())["pending_long_exercise_name"] is None
    assert await state.get_state() == WorkoutFlow.creating_exercise_name


async def test_typing_in_exercise_picker_searches_instead_of_being_ignored(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Bench press", group_id)
    await db.create_exercise(user_id, "Triceps pushdown", group_id)

    state = await _make_state(user_id)
    message = _make_message(user_id, "bench")

    await workout.pick_exercise_search(message, state)

    message.delete.assert_awaited_once()
    kb = message.bot.send_message.await_args.kwargs["reply_markup"]
    button_texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "Bench press" in button_texts
    assert not any("Triceps" in t for t in button_texts)


async def test_typing_on_group_screen_searches_and_enters_exercise_picking(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Bench press", group_id)

    state = await _make_state(user_id)
    await state.set_state(WorkoutFlow.picking_group)  # typing from the muscle-group list
    message = _make_message(user_id, "bench")

    await workout.pick_exercise_search(message, state)

    assert await state.get_state() == WorkoutFlow.picking_exercise
    kb = message.bot.send_message.await_args.kwargs["reply_markup"]
    button_texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "Bench press" in button_texts


async def test_typing_no_match_in_exercise_picker_offers_to_create(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Bench press", group_id)

    state = await _make_state(user_id)
    await state.update_data(pending_group_id=group_id)
    message = _make_message(user_id, "squat")

    await workout.pick_exercise_search(message, state)

    sent_text = message.bot.send_message.await_args.kwargs["text"]
    assert "Ничего не нашлось" in sent_text
    kb = message.bot.send_message.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "pick:new" in callback_datas


async def test_pick_page_advances_to_second_page_and_keeps_remainder(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    for i in range(10):  # RECENT_EXERCISES_LIMIT (8) + 2 left over on page 2
        await db.create_exercise(user_id, f"Exercise {i:02d}", group_id)

    state = await _make_state(user_id, pick_page=0)
    callback = _make_callback(user_id, "pick:page:1")

    await workout.pick_page(callback, state)

    data = await state.get_data()
    assert data["pick_page"] == 1

    # Second page should contain the remaining 2 exercises. With only 2 short names left,
    # they're shown directly on the buttons rather than as a numbered list in the text.
    sent_kwargs = callback.bot.send_message.await_args.kwargs
    button_texts = [
        button.text
        for row in sent_kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert sum(text.startswith("Exercise") for text in button_texts) == 2


async def test_pick_page_first_page_has_no_back_button(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    for i in range(14):
        await db.create_exercise(user_id, f"Exercise {i:02d}", group_id)

    state = await _make_state(user_id, pick_page=1)
    callback = _make_callback(user_id, "pick:page:0")

    await workout.pick_page(callback, state)

    kb = callback.bot.send_message.await_args.kwargs["reply_markup"]
    callback_datas = [
        button.callback_data for row in kb.inline_keyboard for button in row
    ]
    assert "pick:page:-1" not in callback_datas
    assert "pick:page:1" in callback_datas  # next-page button still present


async def test_pick_cancel_on_fresh_empty_workout_discards_it_and_returns_to_menu(fresh_db, user_id):
    """"Назад" right after "Начать тренировку" — before anything was logged —
    should undo the workout the tap created, not drop the user on the same
    "add exercise to begin" screen (see _back_after_cancel)."""
    db = fresh_db
    workout_id = await db.create_workout(user_id)

    state = await _make_state(user_id)
    await state.update_data(workout_id=workout_id)
    await state.set_state(WorkoutFlow.picking_group)
    callback = _make_callback(user_id, "pick:cancel")
    callback.message = MagicMock()
    callback.message.delete = AsyncMock()
    callback.message.answer = AsyncMock(return_value=SimpleNamespace(message_id=999))
    callback.message.text = "some text"
    callback.message.chat = SimpleNamespace(id=user_id)
    callback.message.message_id = 1

    await workout.pick_cancel(callback, state)

    assert await db.get_workout(workout_id) is None
    text = callback.message.answer.await_args.args[0]
    assert "АТЛЕТ" in text  # main menu greeting/onboarding, not the live tracker


async def test_finishing_last_exercise_suggests_what_came_next_last_time(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    triceps = await db.create_exercise(user_id, "Triceps pushdown", group_id)

    # Prior finished workout: bench, then triceps.
    prev_workout = await db.create_workout(user_id)
    b1 = await db.create_block(prev_workout, "single")
    await db.add_block_exercise(b1, bench, 0)
    b2 = await db.create_block(prev_workout, "single")
    await db.add_block_exercise(b2, triceps, 0)
    await db.finish_workout(prev_workout)

    # Current workout: bench just logged and being finished, nothing else open.
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)
    await db.add_set(block_id, bench, 1, 0, 100, 8)

    state = await _make_state(
        user_id, open_exercises=[bench], open_blocks={bench: block_id}, active_exercise_id=bench,
    )
    await state.update_data(workout_id=workout_id)
    await state.set_state(WorkoutFlow.logging_set)
    callback = _make_callback(user_id, "live:finish_exercise")

    await workout.live_finish_exercise(callback, state)

    kb = callback.bot.send_message.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"live:suggest:{triceps}" in callback_datas
    # The name lives on the button itself, so the text isn't asked to repeat it.
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "Triceps pushdown" in texts
    assert "Triceps pushdown" not in callback.bot.send_message.await_args.kwargs["text"]


async def test_finishing_exercise_with_no_sets_deletes_its_empty_block(fresh_db, user_id):
    """Tapping "закончить упражнение" before logging anything shouldn't leave a
    dangling empty block behind — reopening the exercise later would otherwise
    create a second block for it, showing up as a duplicate empty header."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    rdl = await db.create_exercise(user_id, "Румынская тяга", group_id)

    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, rdl, 0)

    state = await _make_state(
        user_id, open_exercises=[rdl], open_blocks={rdl: block_id}, active_exercise_id=rdl,
    )
    await state.update_data(workout_id=workout_id)
    await state.set_state(WorkoutFlow.logging_set)
    callback = _make_callback(user_id, "live:finish_exercise")

    await workout.live_finish_exercise(callback, state)

    assert await db.get_block(block_id) is None
    blocks = await db.list_blocks_for_workout(workout_id)
    assert blocks == []


async def test_tapping_suggestion_jumps_straight_into_logging_it(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    triceps = await db.create_exercise(user_id, "Triceps pushdown", group_id)

    workout_id = await db.create_workout(user_id)
    state = await _make_state(
        user_id, open_exercises=[], open_blocks={}, active_exercise_id=None,
    )
    await state.update_data(workout_id=workout_id)
    await state.set_state(WorkoutFlow.idle)
    callback = _make_callback(user_id, f"live:suggest:{triceps}")

    await workout.live_pick_suggested(callback, state)

    data = await state.get_data()
    assert data["active_exercise_id"] == triceps
    assert data["open_exercises"] == [triceps]
    assert await state.get_state() == WorkoutFlow.logging_set.state


# ---------- template picking previews before adding ----------


def _make_template_callback(user_id: int, data: str):
    message = MagicMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock()
    message.answer_photo = AsyncMock()
    message.answer_media_group = AsyncMock()
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=999))
    _stub_photo_sends(bot)
    message.bot = bot
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id)
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _template_id(db, group_name: str, exercise_name: str) -> int:
    groups = await db.list_muscle_groups(None, global_only=True)
    group_id = next(g["id"] for g in groups if g["name"] == group_name)
    templates = await db.list_templates_in_group(group_id)
    return next(t["id"] for t in templates if t["name"] == exercise_name)


async def test_pick_template_previews_without_adding_it(fresh_db, user_id):
    db = fresh_db
    template_id = await _template_id(db, "Спина", "Тяга гантели в наклоне")

    state = await _make_state(user_id)
    await state.set_state(WorkoutFlow.creating_exercise_name)
    callback = _make_template_callback(user_id, f"pick:tpl:{template_id}")

    await workout.pick_template_preview(callback, state)

    callback.message.answer_photo.assert_awaited_once()
    kb = callback.message.answer_photo.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"pick:tpladd:{template_id}" in callback_datas
    assert await db.count_user_exercises(user_id) == 0


async def test_pick_template_add_forks_and_enters_logging(fresh_db, user_id):
    db = fresh_db
    template_id = await _template_id(db, "Спина", "Тяга гантели в наклоне")

    workout_id = await db.create_workout(user_id)
    state = await _make_state(user_id, open_exercises=[], open_blocks={}, active_exercise_id=None)
    await state.update_data(workout_id=workout_id)
    await state.set_state(WorkoutFlow.creating_exercise_name)
    callback = _make_template_callback(user_id, f"pick:tpladd:{template_id}")

    await workout.pick_template_add(callback, state)

    assert await db.count_user_exercises(user_id) == 1
    callback.message.delete.assert_awaited_once()
    assert await state.get_state() == WorkoutFlow.logging_set.state
    # The photos come from the sticky card above the tracker, not from a separate
    # one-off album that would double up with it.
    callback.message.answer_media_group.assert_not_awaited()
    callback.bot.send_media_group.assert_awaited_once()
