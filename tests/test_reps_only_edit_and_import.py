"""«Только повторы» (seed_data.is_reps_only) — и в правке прошлой тренировки,
и в импорте истории, а не только в живом трекере и /v1.

Правка в боте: «40 8» на подтягиваниях — отказ с подсказкой про вариант
«с весом» (тот же текст, что у трекера), «8» — подход 0×8.
Импорт: подход с весом в «только повторы» с вариантом ложится в копию
варианта (по идентичности или заведённую импортом — тогда её снимает
«Отменить импорт»); без варианта — пишется как раньше."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import db
import i18n
import seed_data
from fsm import EditWorkoutFlow
from handlers import csv_import, edit_workout

pytestmark = pytest.mark.asyncio

PULL = "Подтягивания"
WEIGHTED = "Подтягивания с весом"
REFUSAL = seed_data.reps_only_weight_refusal(PULL, "ru")


async def _fork(user_id: int, name: str) -> int:
    cur = await db.conn().execute(
        "SELECT id FROM exercises WHERE is_template = 1 AND user_id IS NULL AND name = ?", (name,)
    )
    return await db.fork_exercise_from_template(user_id, (await cur.fetchone())["id"])


async def _copies(user_id: int, original_name: str) -> list:
    cur = await db.conn().execute(
        "SELECT * FROM exercises WHERE user_id = ? AND is_template = 0 AND original_name = ?",
        (user_id, original_name),
    )
    return await cur.fetchall()


# ---------- правка прошлой тренировки в боте ----------


def _message(user_id: int, text: str):
    msg = MagicMock()
    msg.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    msg.text = text
    msg.chat = SimpleNamespace(id=user_id)
    msg.message_id = 500
    msg.reply = AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=501))
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    msg.delete = AsyncMock()
    return msg


async def _past_pullups(user_id: int, sets=((0, 8),)) -> tuple[int, int, int, list[int]]:
    pull = await _fork(user_id, PULL)
    wid = await db.create_finished_workout(user_id, "2026-01-05T10:00:00", "2026-01-05T11:00:00")
    block = await db.create_block(wid, "single")
    await db.add_block_exercise(block, pull, 0)
    ids = [await db.add_set(block, pull, i + 1, 0, w, r) for i, (w, r) in enumerate(sets)]
    return wid, block, pull, ids


async def _state(user_id: int, workout_id: int, flow_state, **data) -> FSMContext:
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(flow_state)
    await state.update_data(edit_workout_id=workout_id, **data)
    return state


async def _pull_sets(block_id: int, ex_id: int) -> list[tuple]:
    return [
        (s["weight"], s["reps"]) for s in await db.list_sets_for_block(block_id) if s["exercise_id"] == ex_id
    ]


async def test_edit_set_refuses_weight_and_takes_bare_reps_as_zero(fresh_db, user_id):
    # Старый подход с поясом (до запрета) — «8» делает его 0×8, а не 10×8.
    wid, block, pull, [set_id] = await _past_pullups(user_id, sets=[(10, 6)])
    state = await _state(user_id, wid, EditWorkoutFlow.editing_set, edit_set_id=set_id)
    with i18n.use_lang("ru"):
        refused = _message(user_id, "40 8")
        await edit_workout.editw_editset_entered(refused, state)
        assert refused.reply.await_args.args[0] == REFUSAL
        assert await _pull_sets(block, pull) == [(10, 6)]

        await edit_workout.editw_editset_entered(_message(user_id, "8"), state)
    assert await _pull_sets(block, pull) == [(0, 8)]


async def test_add_set_refuses_weight_and_does_not_carry_old_weight(fresh_db, user_id):
    wid, block, pull, _ = await _past_pullups(user_id, sets=[(10, 6)])
    state = await _state(
        user_id, wid, EditWorkoutFlow.adding_set, add_block_id=block, add_exercise_id=pull
    )
    with i18n.use_lang("ru"):
        refused = _message(user_id, "40 8")
        await edit_workout.editw_addset_entered(refused, state)
        assert refused.reply.await_args.args[0] == REFUSAL
        assert await _pull_sets(block, pull) == [(10, 6)]

        await edit_workout.editw_addset_entered(_message(user_id, "8"), state)
    assert await _pull_sets(block, pull) == [(10, 6), (0, 8)]


async def test_typed_set_on_exercise_screen_refuses_weight(fresh_db, user_id):
    wid, block, pull, _ = await _past_pullups(user_id)
    state = await _state(
        user_id, wid, EditWorkoutFlow.viewing_exercise, edit_block_id=block, edit_exercise_id=pull
    )
    with i18n.use_lang("en"):
        refused = _message(user_id, "40 8")
        await edit_workout.editw_typed_set(refused, state)
        assert refused.reply.await_args.args[0] == seed_data.reps_only_weight_refusal(PULL, "en")
        assert await _pull_sets(block, pull) == [(0, 8)]

        await edit_workout.editw_typed_set(_message(user_id, "9"), state)
    assert await _pull_sets(block, pull) == [(0, 8), (0, 9)]


async def test_weighted_exercise_still_takes_weight_in_edit(fresh_db, user_id):
    variant = await _fork(user_id, WEIGHTED)
    wid = await db.create_finished_workout(user_id, "2026-01-05T10:00:00", "2026-01-05T11:00:00")
    block = await db.create_block(wid, "single")
    await db.add_block_exercise(block, variant, 0)
    state = await _state(
        user_id, wid, EditWorkoutFlow.viewing_exercise, edit_block_id=block, edit_exercise_id=variant
    )
    with i18n.use_lang("ru"):
        await edit_workout.editw_typed_set(_message(user_id, "20 5"), state)
    assert await _pull_sets(block, variant) == [(20, 5)]


# ---------- импорт ----------


def _session(date: str, entries: list[dict]) -> dict:
    return {"date": date, "start": None, "title": None, "entries": entries}


async def _blocks(workout_id: int) -> list[list[tuple[int, list[tuple]]]]:
    """Блоки тренировки: [(exercise_id, [(вес, повторы, раунд)])] по порядку."""
    out = []
    for block in await db.list_blocks_for_workout(workout_id):
        sets = await db.list_sets_for_block(block["id"])
        out.append([
            (be["exercise_id"], [
                (s["weight"], s["reps"], s["round_index"]) for s in sets if s["exercise_id"] == be["exercise_id"]
            ])
            for be in await db.get_block_exercises(block["id"])
        ])
    return out


async def test_import_routes_weighted_sets_to_a_variant_it_creates_and_undo_removes_it(fresh_db, user_id):
    pull = await _fork(user_id, PULL)
    bench = await _fork(user_id, "Жим штанги лёжа")
    workouts = [
        _session("2025-05-01", [
            {"name": "Pull Up", "sets": [[None, 12, None], [10, 6, None], [0, 10, None], [15, 4, None]],
             "note": "пояс"},
            {"name": "Bench", "sets": [[80, 8, None]]},
        ]),
        _session("2025-05-03", [{"name": "Pull Up", "sets": [[20, 3, None], [20, 3, None]]}]),
    ]
    decisions = {"Pull Up": {"kind": "existing", "id": pull}, "Bench": {"kind": "existing", "id": bench}}

    result = await csv_import.run_import(user_id, workouts, decisions, "hevy")

    [variant] = await _copies(user_id, WEIGHTED)
    assert variant["display_name"] == WEIGHTED
    assert variant["import_batch_id"] == result["batch_id"]
    assert result["sets"] == 7
    cur = await db.conn().execute(
        "SELECT id FROM workouts WHERE user_id = ? ORDER BY started_at", (user_id,)
    )
    mixed, heavy = [r["id"] for r in await cur.fetchall()]
    # Смешанная: без веса остаются на месте (с заметкой), с весом — блок следом.
    assert await _blocks(mixed) == [
        [(pull, [(0, 12, 1), (0, 10, 2)])],
        [(variant["id"], [(10, 6, 1), (15, 4, 2)])],
        [(bench, [(80, 8, 1)])],
    ]
    assert await db.get_workout_exercise_note(mixed, pull) == "пояс"
    # Целиком с весом — сразу вариант.
    assert await _blocks(heavy) == [[(variant["id"], [(20, 3, 1), (20, 3, 2)])]]
    cur = await db.conn().execute("SELECT COUNT(*) FROM sets WHERE exercise_id = ? AND weight > 0", (pull,))
    assert (await cur.fetchone())[0] == 0

    await csv_import.undo_import(user_id, result["batch_id"])
    assert await _copies(user_id, WEIGHTED) == []
    assert await db.get_exercise(pull) is not None


async def test_import_reuses_existing_variant_and_keeps_superset(fresh_db, user_id):
    pull = await _fork(user_id, PULL)
    variant = await _fork(user_id, WEIGHTED)
    dips = await _fork(user_id, "Отжимания на брусьях")
    workouts = [_session("2025-06-01", [
        {"name": "Pull Up", "sets": [[10, 6, None], [10, 5, None]], "superset": "s1"},
        {"name": "Dip", "sets": [[0, 12, None], [0, 10, None]], "superset": "s1"},
    ])]
    decisions = {"Pull Up": {"kind": "existing", "id": pull}, "Dip": {"kind": "existing", "id": dips}}

    result = await csv_import.run_import(user_id, workouts, decisions, "hevy")

    assert [r["id"] for r in await _copies(user_id, WEIGHTED)] == [variant]
    assert (await db.get_exercise(variant))["import_batch_id"] is None
    cur = await db.conn().execute("SELECT id FROM workouts WHERE user_id = ?", (user_id,))
    wid = (await cur.fetchone())["id"]
    blocks = await db.list_blocks_for_workout(wid)
    assert [b["type"] for b in blocks] == ["superset"]
    assert await _blocks(wid) == [[
        (variant, [(10, 6, 1), (10, 5, 2)]),
        (dips, [(0, 12, 1), (0, 10, 2)]),
    ]]
    # Отмена импорта не трогает копию, которая была до него.
    await csv_import.undo_import(user_id, result["batch_id"])
    assert await db.get_exercise(variant) is not None


async def test_import_without_a_variant_writes_weight_as_before(fresh_db, user_id):
    pushups = await _fork(user_id, "Отжимания от пола")
    workouts = [_session("2025-07-01", [{"name": "Push Up", "sets": [[0, 12, None], [10, 8, None]]}])]

    await csv_import.run_import(user_id, workouts, {"Push Up": {"kind": "existing", "id": pushups}}, "csv")

    cur = await db.conn().execute("SELECT id FROM workouts WHERE user_id = ?", (user_id,))
    wid = (await cur.fetchone())["id"]
    assert await _blocks(wid) == [[(pushups, [(0, 12, 1), (10, 8, 2)])]]
    assert await _copies(user_id, WEIGHTED) == []
