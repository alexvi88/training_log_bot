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


async def test_finishing_exercise_with_pending_confirm_does_not_write_to_wrong_block(
    fresh_db, user_id
):
    """A "555кг? Записываем?" prompt for exercise A is still unanswered when the
    user taps "закончить упражнение" on A (B stays open, superset case). A's
    block_id is dropped from open_blocks by finishing it; if the pending
    confirmation weren't discarded too, answering "Да" afterwards would look up
    a block_id that no longer exists and crash (or, worse, silently resolve to
    whatever block ends up at that key)."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group_id)
    row = await db.create_exercise(user_id, "Row", group_id)

    workout_id = await db.create_workout(user_id)
    bench_block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(bench_block, bench, 0)
    row_block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(row_block, row, 0)

    state = await _make_state(
        user_id,
        open_exercises=[bench, row],
        open_blocks={bench: bench_block, row: row_block},
        active_exercise_id=bench,
        pending_weight_confirm={
            "exercise_id": bench, "sets": [[555.0, 5, None]], "source": "text",
            "chat_id": user_id, "message_id": 42, "prompt_message_id": 43,
        },
    )
    await state.update_data(workout_id=workout_id)
    await state.set_state(WorkoutFlow.logging_set)
    finish_callback = _make_callback(user_id, "live:finish_exercise")

    await workout.live_finish_exercise(finish_callback, state)

    data = await state.get_data()
    assert data.get("pending_weight_confirm") is None
    assert bench not in (data.get("open_blocks") or {})

    # Tapping the now-stale "Да, записать" button must not crash and must not
    # write anything — the prompt it answers no longer applies to anything.
    confirm_callback = _make_callback(user_id, "live:wconf:yes")
    await workout.live_weight_confirm(confirm_callback, state)

    assert await db.list_sets_for_block(bench_block) == []
    assert await db.list_sets_for_block(row_block) == []
    confirm_callback.answer.assert_awaited()


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


# ---------- "Составить программу с AI" on the fresh-workout picker ----------


async def _picker_extra_callbacks(db, user_id, monkeypatch) -> list[str]:
    """Render the first picker screen of a fresh workout and return the callback
    data of its buttons. _refresh_live is stubbed — the keyboard is the subject
    here, not the live tracker it gets rendered into."""
    captured = {}

    async def fake_refresh_live(bot, state, user, workout_id, hint, kb):
        captured["kb"] = kb

    monkeypatch.setattr(workout, "_refresh_live", fake_refresh_live)

    workout_id = await db.create_workout(user_id)
    state = await _make_state(user_id, open_exercises=[], active_exercise_id=None)
    await state.update_data(workout_id=workout_id)
    callback = _make_callback(user_id, "menu:start_workout")

    await workout._picker_screen_groups(callback, state, show_program_button=True)

    return [b.callback_data for row in captured["kb"].inline_keyboard for b in row]


async def test_picker_offers_building_a_program_when_the_user_has_none(
    fresh_db, user_id, monkeypatch
):
    monkeypatch.setattr(workout.ai_trainer, "is_configured", lambda: True)

    callbacks = await _picker_extra_callbacks(fresh_db, user_id, monkeypatch)

    assert "ai:buildprog" in callbacks


async def test_picker_hides_the_ai_program_button_once_a_program_exists(
    fresh_db, user_id, monkeypatch
):
    """With programs saved, "🗂 Выбрать программу" already leads somewhere useful
    — a second program button would just crowd a screen meant for training."""
    monkeypatch.setattr(workout.ai_trainer, "is_configured", lambda: True)
    await fresh_db.create_routine(user_id, "Верх/низ")

    callbacks = await _picker_extra_callbacks(fresh_db, user_id, monkeypatch)

    assert "ai:buildprog" not in callbacks
    assert "rt:manage" in callbacks


async def test_picker_hides_the_ai_program_button_when_the_trainer_is_off(
    fresh_db, user_id, monkeypatch
):
    monkeypatch.setattr(workout.ai_trainer, "is_configured", lambda: False)

    callbacks = await _picker_extra_callbacks(fresh_db, user_id, monkeypatch)

    assert "ai:buildprog" not in callbacks


# ---------- recently-trained programs shown above the muscle groups ----------


async def test_picker_shows_a_button_for_a_recently_trained_program(fresh_db, user_id, monkeypatch):
    """Someone running a split shouldn't have to detour through 🗂 Программы to
    find today's day — the program they've actually trained by this month is
    right there above the muscle groups."""
    routine_id = await fresh_db.create_routine(user_id, "Ноги", program_name="Верх/низ")
    await fresh_db.create_workout(user_id, routine_id=routine_id)

    callbacks = await _picker_extra_callbacks(fresh_db, user_id, monkeypatch)

    assert f"rt:pgm:{routine_id}" in callbacks


async def test_picker_ignores_programs_not_trained_recently(fresh_db, user_id, monkeypatch):
    import datetime as dt

    routine_id = await fresh_db.create_routine(user_id, "Ноги", program_name="Старое")
    stale = (dt.datetime.now() - dt.timedelta(days=workout.RECENT_PROGRAM_DAYS + 5)).isoformat()
    await fresh_db.create_workout(user_id, started_at=stale, routine_id=routine_id)

    callbacks = await _picker_extra_callbacks(fresh_db, user_id, monkeypatch)

    assert f"rt:pgm:{routine_id}" not in callbacks


async def test_picker_offers_no_more_than_the_recent_program_cap(fresh_db, user_id, monkeypatch):
    for i in range(workout.MAX_RECENT_PROGRAM_BUTTONS + 2):
        routine_id = await fresh_db.create_routine(user_id, f"День {i}", program_name=f"Программа {i}")
        await fresh_db.create_workout(user_id, routine_id=routine_id)

    callbacks = await _picker_extra_callbacks(fresh_db, user_id, monkeypatch)

    assert len([c for c in callbacks if c.startswith("rt:pgm:")]) == workout.MAX_RECENT_PROGRAM_BUTTONS


async def test_picker_shows_no_recent_programs_without_any_routine_backed_workouts(
    fresh_db, user_id, monkeypatch
):
    """A from-scratch workout (no routine_id) shouldn't manufacture a program button."""
    await fresh_db.create_routine(user_id, "Ноги", program_name="Верх/низ")
    await fresh_db.create_workout(user_id)  # started fresh, not from that routine

    callbacks = await _picker_extra_callbacks(fresh_db, user_id, monkeypatch)

    assert not [c for c in callbacks if c.startswith("rt:pgm:")]
