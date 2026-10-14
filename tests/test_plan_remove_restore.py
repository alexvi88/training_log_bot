"""Упражнение из плана взяли в тренировку и убрали — оно снова в остатке плана
на своём месте в порядке дня, как будто его и не брали.

В трекере бота «убрать из тренировки» — закрыть упражнение без единого подхода
(«Завершить упражнение»: пустой блок стирается) или заменить пустое на
похожее. Раньше очередь плана худела в момент открытия и обратно не росла:
открыл из плана, передумал — упражнение пропадало из «📋 Другое из плана»
насовсем, хотя сделано не было (владелец: «не надо так»).
"""
import pytest

from handlers import routines, workout
from tests.test_planned_order import _go_idle, _last_text, _make_callback, _routine_for, _state

pytestmark = pytest.mark.asyncio


def _ids(planned):
    return [b["exercise_ids"] for b in planned]


async def _program(db, user_id):
    routine, ex_ids = await _routine_for(
        db, user_id, [("Упражнение А", "4x8"), ("Упражнение Б", "3x12"), ("Упражнение В", "3x10")],
    )
    state = await _state(user_id)
    await routines._begin_routine_workout(_make_callback(user_id), state, routine)
    return state, ex_ids


async def test_picked_from_plan_and_closed_empty_returns_in_place(fresh_db, user_id):
    db = fresh_db
    state, (bench, pulldown, dips) = await _program(db, user_id)
    await workout.live_plan_pick(_make_callback(user_id, "live:plan:pick:1"), state)
    assert _ids((await state.get_data())["planned_blocks"]) == [[bench], [dips]]

    cb = await _go_idle(user_id, state, done=False)

    data = await state.get_data()
    assert _ids(data["planned_blocks"]) == [[bench], [pulldown], [dips]]
    assert data["planned_blocks"][1]["targets"] == {pulldown: "3x12"}
    assert "Программа пройдена" not in (_last_text(cb) or "")


async def test_sets_undone_then_closed_returns_in_place(fresh_db, user_id):
    """С подходами, снятыми «Удалить последний», — то же: подходов нет, значит,
    упражнения в тренировке нет."""
    db = fresh_db
    state, (bench, pulldown, dips) = await _program(db, user_id)
    await workout.live_plan_pick(_make_callback(user_id, "live:plan:pick:0"), state)
    await _go_idle(user_id, state)  # жим сделан
    await workout.live_plan_pick(_make_callback(user_id, "live:plan:pick:0"), state)  # тяга
    data = await state.get_data()
    await db.append_set(data["open_blocks"][pulldown], pulldown, 0, 40, 12)
    await workout.live_undo(_make_callback(user_id, "live:undo"), state)

    await _go_idle(user_id, state, done=False)

    assert _ids((await state.get_data())["planned_blocks"]) == [[pulldown], [dips]]


async def test_done_exercise_stays_off_the_plan(fresh_db, user_id):
    db = fresh_db
    state, (bench, pulldown, dips) = await _program(db, user_id)
    await workout.live_plan_pick(_make_callback(user_id, "live:plan:pick:1"), state)
    await _go_idle(user_id, state)
    assert _ids((await state.get_data())["planned_blocks"]) == [[bench], [dips]]


async def test_manual_pick_then_closed_empty_returns_in_place(fresh_db, user_id):
    """Взял упражнение плана через «➕ Упражнение», а не из 📋, — то же самое."""
    db = fresh_db
    state, (bench, pulldown, dips) = await _program(db, user_id)
    await workout._on_exercise_chosen(_make_callback(user_id), state, dips)
    assert _ids((await state.get_data())["planned_blocks"]) == [[bench], [pulldown]]

    await _go_idle(user_id, state, done=False)

    assert _ids((await state.get_data())["planned_blocks"]) == [[bench], [pulldown], [dips]]


async def test_last_plan_item_closed_empty_is_not_program_complete(fresh_db, user_id):
    db = fresh_db
    routine, (bench,) = await _routine_for(db, user_id, [("Жим лёжа", "4x8")])
    state = await _state(user_id)
    await routines._begin_routine_workout(_make_callback(user_id), state, routine)
    data = await state.get_data()
    assert data["planned_blocks"] == [] and data["plan_completes_on_close"]

    cb = await _go_idle(user_id, state, done=False)

    data = await state.get_data()
    assert _ids(data["planned_blocks"]) == [[bench]]
    assert not data["plan_completes_on_close"]
    assert "Программа пройдена" not in (_last_text(cb) or "")


async def test_substitute_covers_the_plan_item_and_gives_it_back_when_removed(fresh_db, user_id):
    """Замена на похожее закрывает пункт плана: убрал замену — в остатке снова
    пункт плана (тяга), а не замена."""
    db = fresh_db
    state, (bench, pulldown, dips) = await _program(db, user_id)
    row = await db.create_exercise(user_id, "Упражнение Г", None)
    await workout.live_plan_pick(_make_callback(user_id, "live:plan:pick:1"), state)

    await workout._swap_active_exercise(_make_callback(user_id), state, row)
    assert _ids((await state.get_data())["planned_blocks"]) == [[bench], [dips]]

    await _go_idle(user_id, state, done=False)
    assert _ids((await state.get_data())["planned_blocks"]) == [[bench], [pulldown], [dips]]


async def test_substitute_done_keeps_the_plan_item_covered(fresh_db, user_id):
    db = fresh_db
    state, (bench, pulldown, dips) = await _program(db, user_id)
    row = await db.create_exercise(user_id, "Упражнение Г", None)
    await workout.live_plan_pick(_make_callback(user_id, "live:plan:pick:1"), state)
    await workout._swap_active_exercise(_make_callback(user_id), state, row)

    await _go_idle(user_id, state)

    assert _ids((await state.get_data())["planned_blocks"]) == [[bench], [dips]]


async def test_swapping_to_another_plan_item_gives_the_first_back(fresh_db, user_id):
    """Заменил пустое на то, что и так стоит в плане, — оно закрывает свой пункт,
    а прежнее сразу возвращается в остаток."""
    db = fresh_db
    state, (bench, pulldown, dips) = await _program(db, user_id)
    await workout.live_plan_pick(_make_callback(user_id, "live:plan:pick:1"), state)

    await workout._swap_active_exercise(_make_callback(user_id), state, dips)

    assert _ids((await state.get_data())["planned_blocks"]) == [[bench], [pulldown]]


async def test_restore_rejoins_a_superset_partner_in_original_order():
    """Суперсет взят целиком, одно его упражнение убрали — вернулось одно; убрали
    второе — встало к первому в исходном порядке, блок снова целый."""
    block = {"exercise_ids": [1, 2], "targets": {1: "3x8", 2: "3x12"}}
    order = {"1": 0, "2": 1, "3": 2, "4": 3}
    planned = [{"exercise_ids": [3]}, {"exercise_ids": [4]}]
    taken = workout._plan_take(workout._plan_take({}, block, 1), block, 2)

    planned = workout._plan_restore(planned, taken["2"], order)
    assert _ids(planned) == [[2], [3], [4]]
    planned = workout._plan_restore(planned, taken["1"], order)
    assert _ids(planned) == [[1, 2], [3], [4]]
    assert planned[0]["targets"] == {1: "3x8", 2: "3x12"}


async def test_restore_puts_the_item_between_its_neighbours_after_resync_appends():
    """Пункт, дописанный правкой программы посреди тренировки, стоит в конце и
    получает номер после всех выданных — вернувшееся встаёт перед ним."""
    order = workout._number_plan([{"exercise_ids": [1]}, {"exercise_ids": [2]}, {"exercise_ids": [3]}], {})
    planned = [{"exercise_ids": [1]}, {"exercise_ids": [3]}, {"exercise_ids": [9]}]
    order = workout._number_plan(planned, order)
    assert order["9"] == 3

    restored = workout._plan_restore(planned, {"ex_id": 2, "block": {"exercise_ids": [2]}}, order)

    assert _ids(restored) == [[1], [2], [3], [9]]
