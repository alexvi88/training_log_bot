"""Leaving to the menu mid-workout must not destroy the remaining program plan.

Covers three things:
1. `_clear_state_keep_workout` (every "🏠 Меню" tap, /start, the persistent
   keyboard's buttons) now keeps `planned_blocks`/`exercise_targets`/
   `confirmed_weights` alongside the rest of the workout scaffolding.
2. `_reset_new_workout_scaffold` (starting a brand-new workout) still wipes
   all of it, including those three keys.
3. Even a *full* `state.clear()` (handlers/sharing.py's shared-link preview)
   isn't fatal: `_enter_live` can rebuild the remaining plan from the DB, since
   `workouts.routine_id` says which routine the session came from.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

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
    return ex_ids, routine_id


async def test_menu_tap_mid_workout_keeps_the_remaining_program_plan(fresh_db, user_id):
    db = fresh_db
    await _start_program(db, user_id, state := await _state(user_id), [
        ("Жим лёжа", "4x8"), ("Разводка", "3x12"), ("Брусья", None),
    ])
    before_plan = (await state.get_data())["planned_blocks"]
    before_targets = (await state.get_data())["exercise_targets"]
    assert before_plan  # sanity: two exercises still queued after opening the first

    await workout._clear_state_keep_workout(state)

    data = await state.get_data()
    assert data["planned_blocks"] == before_plan
    assert data["exercise_targets"] == before_targets
    # The rest of the scaffolding this function has always preserved still works.
    assert data["workout_id"] is not None


async def test_new_workout_scaffold_reset_still_wipes_the_plan(fresh_db, user_id):
    db = fresh_db
    await _start_program(db, user_id, state := await _state(user_id), [
        ("Жим лёжа", "4x8"), ("Разводка", "3x12"),
    ])
    assert (await state.get_data())["planned_blocks"]

    await workout._reset_new_workout_scaffold(state)

    data = await state.get_data()
    assert data["planned_blocks"] is None
    assert data["exercise_targets"] is None
    assert data["confirmed_weights"] is None
    # AI-тренер state is deliberately exempt — untouched by this reset.
    assert "ai_history" not in data or data.get("ai_history") is None


async def test_full_state_clear_rebuilds_the_plan_from_the_routine_on_next_enter(fresh_db, user_id):
    """handlers/sharing.py's bare `state.clear()` (or any future one) loses the
    FSM plan outright — `_enter_live` must be able to get it back from the DB."""
    db = fresh_db
    (bench, fly, dips), routine_id = await _start_program(
        db, user_id, state := await _state(user_id),
        [("Жим лёжа", "4x8"), ("Разводка", "3x12"), ("Брусья", None)],
    )
    data = await state.get_data()
    workout_id, block_id = data["workout_id"], data["open_blocks"][bench]
    await db.append_set(block_id, bench, 0, 100, 8)

    await state.clear()  # simulates handlers/sharing.py's open_shared

    cb = _make_callback(user_id, "menu:resume_workout")
    await workout._enter_live(cb, state, workout_id)

    rebuilt = (await state.get_data())["planned_blocks"]
    # First exercise (bench) is already open/logged-into, so only the other two remain.
    assert rebuilt == [
        {"exercise_ids": [fly], "targets": {fly: "3x12"}},
        {"exercise_ids": [dips], "targets": {dips: None}},
    ]


async def test_rebuild_excludes_exercises_already_touched_this_workout(fresh_db, user_id):
    db = fresh_db
    (bench, fly), routine_id = await _start_program(
        db, user_id, state := await _state(user_id),
        [("Жим лёжа", "4x8"), ("Разводка", "3x12")],
    )
    data = await state.get_data()
    workout_id, block_id = data["workout_id"], data["open_blocks"][bench]
    await db.append_set(block_id, bench, 0, 100, 8)

    plan = await workout._rebuild_planned_blocks_from_routine(workout_id, routine_id)
    assert plan == [{"exercise_ids": [fly], "targets": {fly: "3x12"}}]


async def test_rebuild_not_used_when_a_plan_is_already_in_the_fsm(fresh_db, user_id):
    """A deliberately-trimmed plan (see live_plan_skip) — even an empty one —
    must never be silently replaced by a DB rebuild."""
    db = fresh_db
    (bench, fly), routine_id = await _start_program(
        db, user_id, state := await _state(user_id),
        [("Жим лёжа", "4x8"), ("Разводка", "3x12")],
    )
    data = await state.get_data()
    workout_id = data["workout_id"]
    await state.update_data(planned_blocks=[])  # user skipped/took everything

    cb = _make_callback(user_id, "menu:resume_workout")
    await workout._enter_live(cb, state, workout_id)

    assert (await state.get_data())["planned_blocks"] == []


async def test_no_rebuild_for_a_workout_without_a_routine(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Присед", group_id)
    workout_id = await db.create_workout(user_id)  # no routine_id
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)

    state = await _state(user_id)
    cb = _make_callback(user_id, "menu:resume_workout")
    await workout._enter_live(cb, state, workout_id)

    assert (await state.get_data()).get("planned_blocks") is None


# ---------- находка 5: правка программы по ходу тренировки ----------


async def test_editing_the_program_mid_workout_adds_the_new_exercise_to_the_plan(fresh_db, user_id):
    """Раньше план был снимком со старта дня: добавил упражнение в программу, а
    «📋 Другое из плана» его не знало до конца тренировки."""
    db = fresh_db
    (bench, fly, dips), routine_id = await _start_program(
        db, user_id, state := await _state(user_id),
        [("Жим лёжа", "4x8"), ("Разводка", "3x12"), ("Брусья", None)],
    )
    group_id = await db.create_muscle_group(user_id, "Трицепс")
    pushdown = await db.create_exercise(user_id, "Разгибания на блоке", group_id)
    await db.add_routine_exercise(routine_id, pushdown, 3, "3x15")

    note = await workout.resync_plan_with_routine(state, routine_id)

    planned = (await state.get_data())["planned_blocks"]
    assert pushdown in [i for b in planned for i in b["exercise_ids"]]
    assert "Разгибания на блоке" in note


async def test_removing_an_exercise_from_the_program_drops_it_from_the_plan(fresh_db, user_id):
    db = fresh_db
    (bench, fly, dips), routine_id = await _start_program(
        db, user_id, state := await _state(user_id),
        [("Жим лёжа", "4x8"), ("Разводка", "3x12"), ("Брусья", None)],
    )
    entries = await db.list_routine_exercises(routine_id)
    await db.remove_routine_exercise(next(e["id"] for e in entries if e["exercise_id"] == dips))

    note = await workout.resync_plan_with_routine(state, routine_id)

    planned = (await state.get_data())["planned_blocks"]
    assert dips not in [i for b in planned for i in b["exercise_ids"]]
    assert "Брусья" in note


async def test_a_new_set_scheme_reaches_the_running_plan(fresh_db, user_id):
    db = fresh_db
    (bench, fly, dips), routine_id = await _start_program(
        db, user_id, state := await _state(user_id),
        [("Жим лёжа", "4x8"), ("Разводка", "3x12"), ("Брусья", None)],
    )
    entries = await db.list_routine_exercises(routine_id)
    await db.set_routine_exercise_target(
        next(e["id"] for e in entries if e["exercise_id"] == fly), "5x5"
    )

    await workout.resync_plan_with_routine(state, routine_id)

    planned = (await state.get_data())["planned_blocks"]
    targets = {int(k): v for b in planned for k, v in (b.get("targets") or {}).items()}
    assert targets[fly] == "5x5"


async def test_an_exercise_dropped_from_the_plan_by_hand_does_not_come_back(fresh_db, user_id):
    """«Убрать из плана» — про сломанный тренажёр здесь и сейчас, а не про
    программу. Правка программы не должна возвращать его в очередь."""
    db = fresh_db
    (bench, fly, dips), routine_id = await _start_program(
        db, user_id, state := await _state(user_id),
        [("Жим лёжа", "4x8"), ("Разводка", "3x12"), ("Брусья", None)],
    )
    await state.update_data(
        planned_blocks=[
            b for b in (await state.get_data())["planned_blocks"]
            if dips not in b["exercise_ids"]
        ],
        plan_skipped_ids=[dips],
    )

    await workout.resync_plan_with_routine(state, routine_id)

    planned = (await state.get_data())["planned_blocks"]
    assert dips not in [i for b in planned for i in b["exercise_ids"]]


async def test_editing_someone_elses_program_leaves_the_running_plan_alone(fresh_db, user_id):
    db = fresh_db
    (bench, fly, dips), routine_id = await _start_program(
        db, user_id, state := await _state(user_id),
        [("Жим лёжа", "4x8"), ("Разводка", "3x12"), ("Брусья", None)],
    )
    before = (await state.get_data())["planned_blocks"]
    other_routine = await db.create_routine(user_id, "Pull day")

    assert await workout.resync_plan_with_routine(state, other_routine) is None
    assert (await state.get_data())["planned_blocks"] == before


async def test_the_exercise_being_done_right_now_is_not_pushed_back_into_the_plan(fresh_db, user_id):
    """Найдено живым прогоном: первое упражнение программы открыто, но ещё ни
    одного подхода в нём нет. Пересборка считала «сделанным» только записанное
    и возвращала открытое упражнение в очередь — плюс писала «добавил Жим
    штанги лёжа» на ровном месте, стоило открыть редактор программы."""
    db = fresh_db
    (bench, fly, dips), routine_id = await _start_program(
        db, user_id, state := await _state(user_id),
        [("Жим лёжа", "4x8"), ("Разводка", "3x12"), ("Брусья", None)],
    )
    # _begin_routine_workout уже открыл первое упражнение — блок есть, подходов нет.
    assert await db.list_exercise_ids_for_workout((await state.get_data())["workout_id"]) == []

    note = await workout.resync_plan_with_routine(state, routine_id)

    planned = (await state.get_data())["planned_blocks"]
    assert bench not in [i for b in planned for i in b["exercise_ids"]]
    assert note is None  # ничего не менялось — и сказать нечего


async def test_an_already_done_exercise_is_swept_out_of_the_plan(fresh_db, user_id):
    """Самолечение: в очереди не место тому, что в этой тренировке уже открывали.
    В обычном ходе вещей оно туда и не попадает, но состояние живёт в FSM
    неделями, и одна кривая запись иначе осталась бы там навсегда."""
    db = fresh_db
    (bench, fly, dips), routine_id = await _start_program(
        db, user_id, state := await _state(user_id),
        [("Жим лёжа", "4x8"), ("Разводка", "3x12"), ("Брусья", None)],
    )
    planned = (await state.get_data())["planned_blocks"]
    # Открытое первым упражнение каким-то образом снова оказалось в очереди.
    await state.update_data(
        planned_blocks=[{"exercise_ids": [bench], "targets": {bench: "4x8"}}] + planned
    )

    note = await workout.resync_plan_with_routine(state, routine_id)

    planned = (await state.get_data())["planned_blocks"]
    assert bench not in [i for b in planned for i in b["exercise_ids"]]
    assert [fly, dips] == [i for b in planned for i in b["exercise_ids"]]
    assert note is None  # программу не меняли — и говорить не о чем
