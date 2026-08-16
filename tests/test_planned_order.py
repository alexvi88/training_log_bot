"""Doing a program's exercises out of order — the "тренажёр занят" case.

The program's order stays the default ("▶️" opens the next one), but the
📋 screen lists everything still owed so any of it can be started now, and
picking a planned exercise by hand takes it off the plan instead of leaving it
to be offered a second time.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import keyboards
from fsm import WorkoutFlow
from handlers import routines, workout

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str = ""):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    ids = iter(range(600, 900))

    async def _answer(*args, **kwargs):
        return SimpleNamespace(message_id=next(ids), chat=SimpleNamespace(id=user_id))

    message.answer = AsyncMock(side_effect=_answer)
    message.delete = AsyncMock()
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(side_effect=_answer)
    bot.edit_message_text = AsyncMock()
    message.bot = bot
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _state(user_id: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


async def _start_program(db, user_id, state, names_with_targets):
    group_id = await db.create_muscle_group(user_id, "Грудь")
    routine_id = await db.create_routine(user_id, "Push day")
    ex_ids = []
    for i, (name, target) in enumerate(names_with_targets):
        ex_id = await db.create_exercise(user_id, name, group_id)
        await db.add_routine_exercise(routine_id, ex_id, i, target)
        ex_ids.append(ex_id)
    routine = await db.get_routine(routine_id)
    await routines._begin_routine_workout(_make_callback(user_id), state, routine)
    # Тренировка по программе начинается с экрана 📋 «С чего начнёшь?» — то, что
    # раньше открывалось само. Здесь берём первое по порядку: эти тесты про
    # порядок ПОСЛЕ старта, а он от способа открыть первое упражнение не зависит.
    await workout.live_plan_pick(_make_callback(user_id, "live:plan:pick:0"), state)
    return ex_ids


def _last_keyboard(callback):
    """The markup of whatever the live tracker was last drawn with."""
    for mock in (callback.bot.edit_message_text, callback.bot.send_message, callback.message.answer):
        if mock.await_args is not None:
            return mock.await_args.kwargs.get("reply_markup")
    return None


def _last_text(callback):
    """The text of whatever the live tracker was last drawn/edited with (the
    tracker itself, not the disposable "🏋️ Тренировка" placeholder message)."""
    for mock in (callback.bot.edit_message_text, callback.bot.send_message):
        if mock.await_args is not None:
            return mock.await_args.kwargs.get("text")
    return None


def _callback_datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _button_texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


async def _go_idle(user_id, state):
    """Finish the exercise that's open, landing on the between-exercises screen."""
    cb = _make_callback(user_id, "live:finish_exercise")
    await workout.live_finish_exercise(cb, state)
    return cb


async def _routine_for(db, user_id, names_with_targets):
    """Программа, но БЕЗ старта тренировки — здесь проверяется как раз то, чем
    тренировка по программе начинается."""
    group_id = await db.create_muscle_group(user_id, "Грудь")
    routine_id = await db.create_routine(user_id, "Push day")
    ex_ids = []
    for i, (name, target) in enumerate(names_with_targets):
        ex_id = await db.create_exercise(user_id, name, group_id)
        await db.add_routine_exercise(routine_id, ex_id, i, target)
        ex_ids.append(ex_id)
    return await db.get_routine(routine_id), ex_ids


async def test_the_first_exercise_is_chosen_too_not_forced(fresh_db, user_id):
    """Порядок был свободным начиная со второго упражнения: первое открывалось
    само, и человек оказывался заперт в том, что стоит в программе номером один,
    — при том что занята в зале бывает ровно та стойка, с которой он собирался
    начать. Тренировка открывается тем же экраном 📋, что и дальше по ходу."""
    db = fresh_db
    routine, (bench, fly) = await _routine_for(db, user_id, [("Жим лёжа", "4x8"), ("Разводка", "3x12")])
    cb = _make_callback(user_id)

    await routines._begin_routine_workout(cb, await _state(user_id), routine)

    assert _button_texts(_last_keyboard(cb)) == ["Жим лёжа", "Разводка", "⬅️ Назад"]
    assert _callback_datas(_last_keyboard(cb))[:2] == ["live:plan:pick:0", "live:plan:pick:1"]
    assert "С чего начнёшь" in _last_text(cb)


async def test_the_start_screen_does_not_offer_removing_anything_yet(fresh_db, user_id):
    """Чего сегодня не будет, выясняется у занятого тренажёра, а не до начала
    тренировки. Между упражнениями кнопка на месте — там это уже решение."""
    db = fresh_db
    routine, _ = await _routine_for(db, user_id, [("Жим лёжа", "4x8"), ("Разводка", "3x12")])
    state = await _state(user_id)
    cb = _make_callback(user_id)
    await routines._begin_routine_workout(cb, state, routine)

    assert "live:plan:rm" not in _callback_datas(_last_keyboard(cb))

    await workout.live_plan_pick(_make_callback(user_id, "live:plan:pick:0"), state)
    await _go_idle(user_id, state)
    mid = _make_callback(user_id, "live:plan")
    await workout.live_plan(mid, state)
    assert "live:plan:rm" in _callback_datas(_last_keyboard(mid))


async def test_starting_out_of_order_opens_the_second_and_keeps_the_first(fresh_db, user_id):
    """Тот самый случай, ради которого экран и появился: стойка для жима занята,
    начинаем с разводки, жим никуда не девается."""
    db = fresh_db
    routine, (bench, fly) = await _routine_for(db, user_id, [("Жим лёжа", "4x8"), ("Разводка", "3x12")])
    state = await _state(user_id)
    await routines._begin_routine_workout(_make_callback(user_id), state, routine)

    await workout.live_plan_pick(_make_callback(user_id, "live:plan:pick:1"), state)

    data = await state.get_data()
    assert await state.get_state() == WorkoutFlow.logging_set
    assert data["open_exercises"] == [fly]
    assert data["planned_blocks"] == [{"exercise_ids": [bench], "targets": {bench: "4x8"}}]


async def test_a_one_exercise_program_opens_straight_away(fresh_db, user_id):
    """Выбирать не из чего — лишний тап там, где решения нет, это не свобода."""
    db = fresh_db
    routine, _ = await _routine_for(db, user_id, [("Жим лёжа", "4x8")])
    state = await _state(user_id)

    await routines._begin_routine_workout(_make_callback(user_id), state, routine)

    assert await state.get_state() == WorkoutFlow.logging_set
    assert len((await state.get_data())["open_exercises"]) == 1


async def test_idle_screen_names_the_next_program_exercise_and_offers_the_rest(fresh_db, user_id):
    db = fresh_db
    await _start_program(db, user_id, state := await _state(user_id), [
        ("Жим лёжа", "4x8"), ("Разводка", "3x12"), ("Отжимания на брусьях", None),
    ])
    cb = await _go_idle(user_id, state)

    kb = _last_keyboard(cb)
    texts = _button_texts(kb)
    assert "▶️ Разводка" in texts
    assert "📋 Другое из плана · ещё 2" in texts
    assert "live:plan" in _callback_datas(kb)


async def test_plan_screen_lists_everything_left_by_name_only(fresh_db, user_id):
    """Строки — на всю ширину и без схемы подходов: посреди тренировки на этом
    экране решают, что делать, а «3x12» рядом с названием только его обрезало."""
    db = fresh_db
    await _start_program(db, user_id, state := await _state(user_id), [
        ("Жим лёжа", "4x8"), ("Разводка", "3x12"), ("Брусья", None),
    ])
    await _go_idle(user_id, state)

    cb = _make_callback(user_id, "live:plan")
    await workout.live_plan(cb, state)
    kb = _last_keyboard(cb)
    assert _button_texts(kb) == ["Разводка", "Брусья", "✕ Убрать из плана", "⬅️ Назад"]
    assert _callback_datas(kb) == [
        "live:plan:pick:0", "live:plan:pick:1", "live:plan:rm", "live:plan:back",
    ]


async def test_removing_is_a_mode_behind_its_own_button(fresh_db, user_id):
    """Крестика в каждой строке больше нет — «убрать» это тот же список за
    отдельной кнопкой, где тап убирает вместо того, чтобы начинать."""
    db = fresh_db
    await _start_program(db, user_id, state := await _state(user_id), [
        ("Жим лёжа", "4x8"), ("Разводка", "3x12"), ("Брусья", None),
    ])
    await _go_idle(user_id, state)

    cb = _make_callback(user_id, "live:plan:rm")
    await workout.live_plan_remove_mode(cb, state)
    kb = _last_keyboard(cb)
    assert "Убрать из плана" in _last_text(cb)
    assert _button_texts(kb) == ["Разводка", "Брусья", "✅ Готово"]
    assert _callback_datas(kb) == ["live:plan:skip:0", "live:plan:skip:1", "live:plan"]


async def test_picking_out_of_order_opens_it_and_keeps_the_rest_queued(fresh_db, user_id):
    """The machine for #2 is taken: start #3 now, #2 stays owed."""
    db = fresh_db
    bench, fly, dips = await _start_program(db, user_id, state := await _state(user_id), [
        ("Жим лёжа", "4x8"), ("Разводка", "3x12"), ("Брусья", "3xМАХ"),
    ])
    await _go_idle(user_id, state)

    await workout.live_plan_pick(_make_callback(user_id, "live:plan:pick:1"), state)

    data = await state.get_data()
    assert await state.get_state() == WorkoutFlow.logging_set
    assert data["open_exercises"] == [dips]
    assert data["planned_blocks"] == [{"exercise_ids": [fly], "targets": {fly: "3x12"}}]
    # The program's scheme follows the exercise, whichever order it's done in.
    assert data["exercise_targets"][dips] == "3xМАХ"


async def test_stale_plan_button_says_so_instead_of_opening_something_else(fresh_db, user_id):
    db = fresh_db
    await _start_program(db, user_id, state := await _state(user_id), [("Жим лёжа", None), ("Разводка", None)])
    await _go_idle(user_id, state)
    await workout.live_next_planned(_make_callback(user_id, "live:next_planned"), state)
    await _go_idle(user_id, state)

    cb = _make_callback(user_id, "live:plan:pick:3")
    await workout.live_plan_pick(cb, state)
    assert cb.answer.await_args.args == ("Это упражнение уже не в плане",)


async def test_manual_pick_of_a_planned_exercise_takes_it_off_the_plan(fresh_db, user_id):
    """Found the third machine free and logged it through "➕ Упражнение" — the
    program shouldn't come back later asking for it again."""
    db = fresh_db
    bench, fly, dips = await _start_program(db, user_id, state := await _state(user_id), [
        ("Жим лёжа", None), ("Разводка", "3x12"), ("Брусья", None),
    ])
    await _go_idle(user_id, state)

    await workout._on_exercise_chosen(_make_callback(user_id), state, fly)

    data = await state.get_data()
    assert data["planned_blocks"] == [{"exercise_ids": [dips], "targets": {dips: None}}]
    assert data["exercise_targets"][fly] == "3x12"


async def test_manual_pick_keeps_the_other_half_of_a_superset(fresh_db, user_id):
    a, b = 41, 42
    state = await _state(user_id)
    await state.update_data(
        workout_id=None, planned_blocks=[{"exercise_ids": [a, b], "targets": {a: "3x10", b: "3x10"}}],
    )
    data = await state.get_data()
    assert workout._drop_planned_exercise(data["planned_blocks"], a) == [
        {"exercise_ids": [b], "targets": {b: "3x10"}}
    ]


async def test_plan_button_hidden_when_only_one_exercise_is_left(fresh_db, user_id):
    """Nothing to choose between — the 📋 screen would just be an extra tap."""
    kb = keyboards.exercise_picker_entry_keyboard(
        has_planned=True, planned_next_name="Жим лёжа", planned_left=1,
    )
    assert "live:plan" not in _callback_datas(kb)
    assert "▶️ Жим лёжа" in _button_texts(kb)


# ---------- skipping a queued exercise (4.5): the machine is broken, not just busy ----------


async def test_skip_drops_the_block_and_redraws_the_plan_screen(fresh_db, user_id):
    db = fresh_db
    await _start_program(db, user_id, state := await _state(user_id), [
        ("Жим лёжа", "4x8"), ("Разводка", "3x12"), ("Брусья", None),
    ])
    await _go_idle(user_id, state)

    cb = _make_callback(user_id, "live:plan:skip:0")
    await workout.live_plan_skip(cb, state)

    data = await state.get_data()
    # Only the skipped block (Разводка, index 0 of what's left) is gone — the
    # workout itself is untouched, and we're still on the 📋 screen, not bumped
    # into logging_set.
    assert len(data["planned_blocks"]) == 1
    assert await state.get_state() == WorkoutFlow.idle
    cb.answer.assert_awaited_once_with("Убрал")
    kb = _last_keyboard(cb)
    # Остались в режиме «убрать»: два сломанных тренажёра — два тапа подряд.
    assert "Разводка" not in _button_texts(kb)
    assert "Брусья" in _button_texts(kb)
    assert _callback_datas(kb) == ["live:plan:skip:0", "live:plan"]


async def test_skip_out_of_range_answers_stale_instead_of_crashing(fresh_db, user_id):
    db = fresh_db
    await _start_program(db, user_id, state := await _state(user_id), [("Жим лёжа", None), ("Разводка", None)])
    await _go_idle(user_id, state)

    cb = _make_callback(user_id, "live:plan:skip:5")
    await workout.live_plan_skip(cb, state)
    assert cb.answer.await_args.args == ("Это упражнение уже не в плане",)


async def test_skipping_the_last_planned_exercise_reaches_the_program_complete_screen(fresh_db, user_id):
    db = fresh_db
    ex_ids = await _start_program(db, user_id, state := await _state(user_id), [("Жим лёжа", None), ("Разводка", None)])
    data = await state.get_data()
    await db.append_set(data["open_blocks"][ex_ids[0]], ex_ids[0], 0, 100, 8)
    await _go_idle(user_id, state)

    cb = _make_callback(user_id, "live:plan:skip:0")
    await workout.live_plan_skip(cb, state)

    assert (await state.get_data())["planned_blocks"] == []
    text = _last_text(cb)
    assert "Программа пройдена" in text
    assert "🏁 Завершить тренировку" in _button_texts(_last_keyboard(cb))


# ---------- находка 20: обычное линейное прохождение тоже должно доходить ----------


async def test_closing_the_last_planned_exercise_in_order_reaches_the_program_complete_screen(
    fresh_db, user_id
):
    """Ровно сценарий из находки 20: делаем по одному упражнению по порядку,
    закрываем каждое обычной кнопкой «Закончить упражнение» (не через
    📋 «Убрать из плана» и без ручного вызова live_next_planned) — после
    последнего должна показаться «🎉 Программа пройдена», а не обычный
    between-exercise экран."""
    db = fresh_db
    bench, fly = await _start_program(
        db, user_id, state := await _state(user_id), [("Жим лёжа", None), ("Разводка", None)],
    )

    # First exercise: log a set, finish it — plan still has one left, so this
    # must land on the ordinary between-exercise screen, not the congrats one.
    data = await state.get_data()
    await db.append_set(data["open_blocks"][bench], bench, 0, 100, 8)
    cb1 = await _go_idle(user_id, state)
    assert "Программа пройдена" not in (_last_text(cb1) or "")
    assert "📋 Программа" not in (_last_text(cb1) or "")  # not the plan list either

    # Second exercise is offered next ("▶️ Разводка") — open and finish it too.
    cb2 = _make_callback(user_id, "live:next_planned")
    await workout.live_next_planned(cb2, state)
    data = await state.get_data()
    await db.append_set(data["open_blocks"][fly], fly, 0, 40, 12)
    cb3 = await _go_idle(user_id, state)

    assert (await state.get_data())["planned_blocks"] == []
    text = _last_text(cb3)
    assert "🎉" in text and "Программа пройдена" in text
    assert "2 упражнения" in text and "2 подхода" in text


async def test_closing_a_non_final_planned_exercise_does_not_show_the_complete_screen(
    fresh_db, user_id
):
    db = fresh_db
    await _start_program(db, user_id, state := await _state(user_id), [
        ("Жим лёжа", None), ("Разводка", None), ("Брусья", None),
    ])
    cb = await _go_idle(user_id, state)
    assert "Программа пройдена" not in (_last_text(cb) or "")
    assert (await state.get_data())["planned_blocks"] != []


# ---------- program-complete moment (4.6 / B9): a real screen, not a grey alert ----------


async def test_next_planned_on_an_empty_plan_shows_the_program_complete_screen(fresh_db, user_id):
    db = fresh_db
    await _start_program(db, user_id, state := await _state(user_id), [("Жим лёжа", "4x8")])
    await _go_idle(user_id, state)  # opens the only planned exercise, plan now empty

    cb = _make_callback(user_id, "live:next_planned")
    await workout.live_next_planned(cb, state)

    assert "Шаблон" not in (_last_text(cb) or "")
    assert "🎉" in _last_text(cb) and "Программа пройдена" in _last_text(cb)
    # No more grey alert text either — the button silently lands on the new screen.
    cb.answer.assert_awaited_once_with()


async def test_plan_screen_on_an_empty_plan_shows_the_program_complete_screen(fresh_db, user_id):
    db = fresh_db
    await _start_program(db, user_id, state := await _state(user_id), [("Жим лёжа", "4x8")])
    await _go_idle(user_id, state)
    await state.update_data(planned_blocks=[])  # already emptied out

    cb = _make_callback(user_id, "live:plan")
    await workout.live_plan(cb, state)

    text = _last_text(cb)
    assert "Программа пройдена" in text
    cb.answer.assert_awaited_once_with()


async def test_program_complete_screen_reports_this_session_numbers(fresh_db, user_id):
    db = fresh_db
    ex_ids = await _start_program(db, user_id, state := await _state(user_id), [("Жим лёжа", "4x8")])
    data = await state.get_data()
    block_id = data["open_blocks"][ex_ids[0]]
    await db.append_set(block_id, ex_ids[0], 0, 100, 8)
    await db.append_set(block_id, ex_ids[0], 0, 100, 8)
    await _go_idle(user_id, state)

    cb = _make_callback(user_id, "live:next_planned")
    await workout.live_next_planned(cb, state)

    text = _last_text(cb)
    assert "1 упражнение" in text
    assert "2 подхода" in text
