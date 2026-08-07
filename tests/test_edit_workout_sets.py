"""Editing sets on a finished workout must clean up after itself: a block
emptied by deleting its last set shouldn't linger as a "подходов нет" ghost
row forever, and any cached AI-trainer comment must be dropped so it gets
regenerated against the new numbers instead of describing stale ones."""
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
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _make_message(user_id: int, text: str):
    msg = MagicMock()
    msg.from_user = SimpleNamespace(id=user_id, username="tester")
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
    set_ids = []
    for weight, reps in sets:
        round_idx = await db.next_round_index(block_id, ex_id)
        set_ids.append(await db.add_set(block_id, ex_id, round_idx, 0, weight, reps, None))
    return ex_id, block_id, set_ids


async def test_deleting_the_only_set_keeps_the_exercise_open(fresh_db, user_id):
    """Deleting the last set must not bounce the user back to the exercise
    list — the block stays (empty) and its "Подходов нет" state is reachable,
    so the user can immediately type a replacement set."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Бицепс")
    workout_id = await _make_finished_workout(db, user_id)
    ex_id, block_id, [set_id] = await _add_exercise_block(
        db, user_id, workout_id, group_id, "Подъём на бицепс", sets=[(20.0, 10)]
    )
    state = await _make_state(user_id, workout_id)
    await state.set_state(EditWorkoutFlow.viewing_exercise)
    await state.update_data(edit_block_id=block_id, edit_exercise_id=ex_id)

    await edit_workout.editw_delset(_make_callback(user_id, f"editw:delset:{set_id}"), state)

    assert [b["id"] for b in await db.list_blocks_for_workout(workout_id)] == [block_id]
    payload = await edit_workout._exercise_screen_payload(workout_id, block_id, ex_id)
    assert payload is not None
    text, _kb = payload
    assert "Подходов нет" in text
    assert await state.get_state() == EditWorkoutFlow.viewing_exercise


async def test_leaving_to_top_level_reaps_the_emptied_block(fresh_db, user_id):
    """The block left behind by the fix above must not linger forever — once
    the user walks away to the top-level list, it's finally reaped."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Бицепс")
    workout_id = await _make_finished_workout(db, user_id)
    ex_id, block_id, [set_id] = await _add_exercise_block(
        db, user_id, workout_id, group_id, "Подъём на бицепс", sets=[(20.0, 10)]
    )
    state = await _make_state(user_id, workout_id)
    await state.set_state(EditWorkoutFlow.viewing_exercise)
    await state.update_data(edit_block_id=block_id, edit_exercise_id=ex_id)
    await edit_workout.editw_delset(_make_callback(user_id, f"editw:delset:{set_id}"), state)

    await edit_workout.editw_to_top(_make_callback(user_id, "editw:top"), state)

    assert await db.list_blocks_for_workout(workout_id) == []


async def test_deleting_one_of_two_sets_keeps_the_block(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Бицепс")
    workout_id = await _make_finished_workout(db, user_id)
    _, block_id, [set_id, _] = await _add_exercise_block(
        db, user_id, workout_id, group_id, "Подъём на бицепс", sets=[(20.0, 10), (20.0, 8)]
    )
    state = await _make_state(user_id, workout_id)

    await edit_workout.editw_delset(_make_callback(user_id, f"editw:delset:{set_id}"), state)

    blocks = await db.list_blocks_for_workout(workout_id)
    assert len(blocks) == 1
    assert len(await db.list_sets_for_block(blocks[0]["id"])) == 1


async def test_deleting_a_set_clears_cached_ai_comment(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Бицепс")
    workout_id = await _make_finished_workout(db, user_id)
    _, _, [set_id, _] = await _add_exercise_block(
        db, user_id, workout_id, group_id, "Подъём на бицепс", sets=[(20.0, 10), (20.0, 8)]
    )
    await db.set_workout_ai_comment(workout_id, "старый комментарий")
    state = await _make_state(user_id, workout_id)

    await edit_workout.editw_delset(_make_callback(user_id, f"editw:delset:{set_id}"), state)

    workout = await db.get_workout(workout_id)
    assert workout["ai_comment"] is None


async def test_editing_a_set_clears_cached_ai_comment(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Бицепс")
    workout_id = await _make_finished_workout(db, user_id)
    _, _, [set_id] = await _add_exercise_block(
        db, user_id, workout_id, group_id, "Подъём на бицепс", sets=[(20.0, 10)]
    )
    await db.set_workout_ai_comment(workout_id, "старый комментарий")
    state = await _make_state(user_id, workout_id)
    await state.update_data(edit_set_id=set_id)

    await edit_workout.editw_editset_entered(_make_message(user_id, "22.5 8"), state)

    workout = await db.get_workout(workout_id)
    assert workout["ai_comment"] is None


# ---------- two-level edit screen (exercise list → that exercise's sets) ----------


async def _seed_workout(db, user_id: int, n_sets: int = 3):
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Становая", group_id)
    workout_id = await db.create_finished_workout(
        user_id, "2026-01-05T10:00:00", "2026-01-05T11:00:00"
    )
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for i in range(n_sets):
        await db.add_set(block_id, ex_id, i, 0, 190.0, 5)
    return workout_id, block_id, ex_id


async def test_top_level_lists_exercises_not_every_set(fresh_db, user_id):
    """5 exercises × 4 sets used to render 30+ single-column rows."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    workout_id = await db.create_finished_workout(
        user_id, "2026-01-05T10:00:00", "2026-01-05T11:00:00"
    )
    for n in range(5):
        ex_id = await db.create_exercise(user_id, f"Упражнение {n}", group_id)
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        for i in range(4):
            await db.add_set(block_id, ex_id, i, 0, 100.0, 8)

    _text, kb = await edit_workout._edit_screen_payload(workout_id)
    rows = kb.inline_keyboard
    # 5 exercises + "новое упражнение" + "изменить дату" + "готово"
    assert len(rows) == 8
    assert all(len(r) == 1 for r in rows)
    labels = [r[0].text for r in rows]
    assert "Упражнение 0" in labels


async def test_exercise_level_lists_its_sets_without_repeating_the_name(fresh_db, user_id):
    db = fresh_db
    workout_id, block_id, ex_id = await _seed_workout(db, user_id)

    text, kb = await edit_workout._exercise_screen_payload(workout_id, block_id, ex_id)

    assert "Становая" in text
    set_buttons = [
        b for row in kb.inline_keyboard for b in row if b.callback_data.startswith("editw:set:")
    ]
    assert len(set_buttons) == 3
    assert set_buttons[0].text == "1 - 190×5"
    assert "Становая" not in set_buttons[0].text


async def test_picking_an_exercise_moves_into_the_set_level_state(fresh_db, user_id):
    """The set buttons only exist on the second level, so the FSM has to be in
    viewing_exercise there — otherwise editw:set:* has no matching handler."""
    db = fresh_db
    workout_id, block_id, ex_id = await _seed_workout(db, user_id)
    state = await _make_state(user_id, workout_id)
    callback = _make_callback(user_id, f"editw:ex:{block_id}:{ex_id}")

    await edit_workout.editw_pick_exercise(callback, state)

    assert await state.get_state() == EditWorkoutFlow.viewing_exercise
    data = await state.get_data()
    assert data["edit_block_id"] == block_id
    assert data["edit_exercise_id"] == ex_id


async def test_typed_set_on_exercise_screen_is_logged(fresh_db, user_id):
    db = fresh_db
    workout_id, block_id, ex_id = await _seed_workout(db, user_id, n_sets=1)
    state = await _make_state(user_id, workout_id)
    await state.set_state(EditWorkoutFlow.viewing_exercise)
    await state.update_data(edit_block_id=block_id, edit_exercise_id=ex_id)

    await edit_workout.editw_typed_set(_make_message(user_id, "200 3"), state)

    sets = await db.list_sets_for_block(block_id)
    assert len(sets) == 2
    assert (sets[-1]["weight"], sets[-1]["reps"]) == (200.0, 3)


async def test_deleting_a_set_returns_to_the_exercise_not_the_list(fresh_db, user_id):
    db = fresh_db
    workout_id, block_id, ex_id = await _seed_workout(db, user_id)
    sets = await db.list_sets_for_block(block_id)
    state = await _make_state(user_id, workout_id)
    await state.set_state(EditWorkoutFlow.viewing_exercise)
    await state.update_data(edit_block_id=block_id, edit_exercise_id=ex_id)
    callback = _make_callback(user_id, f"editw:delset:{sets[0]['id']}")

    await edit_workout.editw_delset(callback, state)

    assert await state.get_state() == EditWorkoutFlow.viewing_exercise
    assert len(await db.list_sets_for_block(block_id)) == 2


async def test_removing_an_exercise_asks_first_and_says_how_much_goes(fresh_db, user_id):
    db = fresh_db
    workout_id, block_id, ex_id = await _seed_workout(db, user_id)
    state = await _make_state(user_id, workout_id)
    await state.update_data(edit_block_id=block_id, edit_exercise_id=ex_id)
    callback = _make_callback(user_id, f"editw:rmexask:{block_id}")

    await edit_workout.editw_remove_exercise_confirm(callback, state)

    # Nothing destroyed yet — it only asked.
    assert len(await db.list_sets_for_block(block_id)) == 3
    sent = callback.message.answer.await_args
    text = sent.args[0] if sent.args else sent.kwargs["text"]
    # Творительный падеж после «вместе с»: «3 подходами», а не именительное
    # «3 подхода» (находка 24 — plural_ru звали с формами не того падежа).
    # Находка 31: слово «сет» само по себе запрещено словарём TONE_OF_VOICE —
    # корень заменён на «подход» целиком, а не только падеж.
    assert "Становая" in text and "вместе с 3 подходами" in text


async def test_removing_an_exercise_uses_instrumental_case_for_one_set(fresh_db, user_id):
    """Regression for находка 24: with a single set the phrase must read
    "с 1 подходом", not the nominative "с 1 подход" that plural_ru(1, ("подход", ...))
    used to produce when the tuple was borrowed from a different context."""
    db = fresh_db
    workout_id, block_id, ex_id = await _seed_workout(db, user_id, n_sets=1)
    state = await _make_state(user_id, workout_id)
    await state.update_data(edit_block_id=block_id, edit_exercise_id=ex_id)
    callback = _make_callback(user_id, f"editw:rmexask:{block_id}")

    await edit_workout.editw_remove_exercise_confirm(callback, state)

    sent = callback.message.answer.await_args
    text = sent.args[0] if sent.args else sent.kwargs["text"]
    assert "вместе с 1 подходом" in text
