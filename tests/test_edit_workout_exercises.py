"""Adding or removing a whole exercise from a past (finished) workout — as
opposed to the pre-existing per-set edit/add/delete, which only ever touched
sets within an exercise that was already there."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

from fsm import EditWorkoutFlow
from handlers import edit_workout

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str = ""):
    message = MagicMock()
    message.delete = AsyncMock()
    message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _make_message(user_id: int, text: str):
    msg = MagicMock()
    msg.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    msg.text = text
    msg.reply = AsyncMock()
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    msg.delete = AsyncMock()
    return msg


async def _make_state(user_id: int, workout_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=MemoryStorage(), key=key)
    await state.set_state(EditWorkoutFlow.viewing)
    await state.update_data(edit_workout_id=workout_id)
    return state


async def _make_finished_workout(db, user_id: int) -> int:
    return await db.create_finished_workout(
        user_id, started_at="2026-01-05T10:00:00", finished_at="2026-01-05T10:30:00"
    )


async def _add_exercise_block(db, user_id: int, workout_id: int, group_id: int, name: str, sets=()):
    ex_id = await db.create_exercise(user_id, name, group_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for weight, reps in sets:
        round_idx = await db.next_round_index(block_id, ex_id)
        await db.add_set(block_id, ex_id, round_idx, 0, weight, reps, None)
    return ex_id, block_id


# ---------- removing a whole exercise ----------


async def test_removing_an_exercise_drops_all_its_sets(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    workout_id = await _make_finished_workout(db, user_id)
    _, block_id = await _add_exercise_block(
        db, user_id, workout_id, group_id, "Жим лёжа", sets=[(100.0, 8), (100.0, 7)]
    )
    state = await _make_state(user_id, workout_id)

    await edit_workout.editw_remove_exercise(_make_callback(user_id, f"editw:rmex:{block_id}"), state)

    assert await db.list_sets_for_block(block_id) == []
    assert await db.list_blocks_for_workout(workout_id) == []


async def test_removing_one_exercise_leaves_others_untouched(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    workout_id = await _make_finished_workout(db, user_id)
    _, bench_block = await _add_exercise_block(
        db, user_id, workout_id, group_id, "Жим лёжа", sets=[(100.0, 8)]
    )
    _, fly_block = await _add_exercise_block(
        db, user_id, workout_id, group_id, "Разведения", sets=[(20.0, 12)]
    )
    state = await _make_state(user_id, workout_id)

    await edit_workout.editw_remove_exercise(_make_callback(user_id, f"editw:rmex:{bench_block}"), state)

    assert await db.list_sets_for_block(bench_block) == []
    assert len(await db.list_sets_for_block(fly_block)) == 1
    blocks = {b["id"] for b in await db.list_blocks_for_workout(workout_id)}
    assert blocks == {fly_block}


async def test_removing_someone_elses_exercise_is_rejected(fresh_db, user_id):
    db = fresh_db
    other_group = await db.create_muscle_group(999, "Грудь")
    other_workout = await _make_finished_workout(db, 999)
    _, block_id = await _add_exercise_block(db, 999, other_workout, other_group, "Жим", [(50.0, 5)])
    state = await _make_state(user_id, other_workout)

    callback = _make_callback(user_id, f"editw:rmex:{block_id}")
    await edit_workout.editw_remove_exercise(callback, state)

    callback.answer.assert_awaited_once_with("Упражнение не найдено", show_alert=True)
    assert len(await db.list_sets_for_block(block_id)) == 1  # untouched


# ---------- adding a wholly new exercise ----------


async def test_add_new_exercise_entry_lists_groups(fresh_db, user_id):
    db = fresh_db
    workout_id = await _make_finished_workout(db, user_id)
    state = await _make_state(user_id, workout_id)

    await edit_workout.editw_new_exercise_start(_make_callback(user_id, "editw:newex"), state)

    assert await state.get_state() == EditWorkoutFlow.adding_exercise_group


async def test_picking_exercise_then_typing_set_creates_the_block(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    ex_id = await db.create_exercise(user_id, "Присед", group_id)
    workout_id = await _make_finished_workout(db, user_id)
    state = await _make_state(user_id, workout_id)

    await edit_workout.editwex_pick_exercise(_make_callback(user_id, f"editwex:ex:{ex_id}"), state)
    assert await state.get_state() == EditWorkoutFlow.adding_set
    assert (await state.get_data())["add_block_id"] is None  # not created yet

    await edit_workout.editw_addset_entered(_make_message(user_id, "140 5"), state)

    blocks = await db.list_blocks_for_workout(workout_id)
    assert len(blocks) == 1
    sets = await db.list_sets_for_block(blocks[0]["id"])
    assert [(s["weight"], s["reps"]) for s in sets] == [(140.0, 5)]


async def test_cancelling_before_a_set_leaves_no_block_behind(fresh_db, user_id):
    """Picking an exercise, then bailing before typing weight/reps, must not
    leave a dangling exercise entry with zero sets."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    ex_id = await db.create_exercise(user_id, "Присед", group_id)
    workout_id = await _make_finished_workout(db, user_id)
    state = await _make_state(user_id, workout_id)

    await edit_workout.editwex_pick_exercise(_make_callback(user_id, f"editwex:ex:{ex_id}"), state)
    await edit_workout.editw_back(_make_callback(user_id, "editw:back"), state)  # cancel

    assert await db.list_blocks_for_workout(workout_id) == []


async def test_adding_a_template_forks_then_prompts_for_a_set(fresh_db, user_id):
    db = fresh_db
    workout_id = await _make_finished_workout(db, user_id)
    template = await db._find_global_template_by_name("Жим штанги лёжа")
    state = await _make_state(user_id, workout_id)

    await edit_workout.editwex_pick_template(
        _make_callback(user_id, f"editwex:tpladd:{template['id']}"), state
    )
    assert await state.get_state() == EditWorkoutFlow.adding_set

    await edit_workout.editw_addset_entered(_make_message(user_id, "100 8"), state)

    blocks = await db.list_blocks_for_workout(workout_id)
    block_exs = await db.get_block_exercises(blocks[0]["id"])
    assert block_exs[0]["display_name"] == "Жим штанги лёжа"


async def test_search_text_offers_both_own_matches_and_templates(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Жим гантелей лёжа", group_id)
    workout_id = await _make_finished_workout(db, user_id)
    state = await _make_state(user_id, workout_id)
    await state.set_state(EditWorkoutFlow.adding_exercise_group)

    message = _make_message(user_id, "жим")
    await edit_workout.editwex_search_text(message, state)

    kb = message.answer.await_args.kwargs["reply_markup"]
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert any("Жим гантелей лёжа" in t for t in texts)
    assert any(t.startswith("📋") for t in texts)
    assert await state.get_state() == EditWorkoutFlow.adding_exercise_pick


async def test_second_new_exercise_does_not_disturb_the_first(fresh_db, user_id):
    """Adding exercise #2 to a workout that already has one must not touch
    the first exercise's block/sets — the empty-block-on-first-set trick is
    scoped to whichever exercise is actually being added."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Ноги")
    workout_id = await _make_finished_workout(db, user_id)
    _, first_block = await _add_exercise_block(db, user_id, workout_id, group_id, "Присед", [(100.0, 5)])
    ex2_id = await db.create_exercise(user_id, "Выпады", group_id)
    state = await _make_state(user_id, workout_id)

    await edit_workout.editwex_pick_exercise(_make_callback(user_id, f"editwex:ex:{ex2_id}"), state)
    await edit_workout.editw_addset_entered(_make_message(user_id, "40 10"), state)

    assert [(s["weight"], s["reps"]) for s in await db.list_sets_for_block(first_block)] == [(100.0, 5)]
    blocks = await db.list_blocks_for_workout(workout_id)
    assert len(blocks) == 2


async def test_removing_an_exercise_takes_back_the_badges_its_sets_unlocked(fresh_db, user_id):
    """Находка 32: значок «Клуб 140» за единственный сет на 150 кг оставался в
    профиле навсегда, если убрать упражнение целиком, — хотя удаление того же
    сета кнопкой на экране сета его корректно снимало."""
    import achievement_sync

    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    workout_id = await _make_finished_workout(db, user_id)
    _, block_id = await _add_exercise_block(
        db, user_id, workout_id, group_id, "Жим лёжа", sets=[(150.0, 3)]
    )
    await achievement_sync.resync(user_id)
    assert "club140" in set(await db.list_achievement_codes(user_id))
    state = await _make_state(user_id, workout_id)

    await edit_workout.editw_remove_exercise(_make_callback(user_id, f"editw:rmex:{block_id}"), state)

    assert "club140" not in set(await db.list_achievement_codes(user_id))
