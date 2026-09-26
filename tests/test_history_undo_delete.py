"""«↩️ Вернуть» после удаления тренировки из истории.

Удаление теперь без экрана «Точно?» — отменяет его кнопка под историей
(handlers/history.py: hist_delete / hist_undo_delete). Раз подтверждения нет,
возврат обязан быть полным: те же подходы, заметки, комментарий тренера и
ачивки, которые сняли вместе с тренировкой.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

import achievement_sync
from fsm import HistoryFlow
from handlers import history

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str):
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


async def _state(user_id: int) -> FSMContext:
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(HistoryFlow.browsing)
    return state


async def _workout(db, user_id: int, name: str = "Жим лёжа", weight: float = 500.0):
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, name, group_id)
    workout_id = await db.create_finished_workout(
        user_id, started_at="2026-01-05T10:00:00", finished_at="2026-01-05T11:00:00"
    )
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for _ in range(3):
        round_idx = await db.next_round_index(block_id, ex_id)
        await db.add_set(block_id, ex_id, round_idx, 0, weight, 10, None)
    return workout_id, ex_id


def _sent(callback):
    """Текст и клавиатура последнего экрана — safe_edit шлёт его заново."""
    call = callback.message.answer.await_args
    kb = call.kwargs["reply_markup"]
    return call.args[0], [b.callback_data for row in kb.inline_keyboard for b in row]


async def _dump(db, workout_id: int):
    snap = await db.snapshot_workout(workout_id)
    return snap and {table: sorted(map(str, rows)) for table, rows in snap.items()}


async def test_delete_is_immediate_and_names_the_workout_with_undo(fresh_db, user_id):
    db = fresh_db
    workout_id, _ = await _workout(db, user_id)
    state = await _state(user_id)
    callback = _make_callback(user_id, f"hist:del:{workout_id}")

    await history.hist_delete(callback, state)

    assert await db.get_workout(workout_id) is None
    text, cbs = _sent(callback)
    assert "Удалил тренировку" in text and "Жим лёжа" in text
    assert cbs[0] == f"hist:undo:{workout_id}"


async def test_undo_brings_back_everything_including_badges(fresh_db, user_id):
    db = fresh_db
    workout_id, ex_id = await _workout(db, user_id)
    await db.set_workout_exercise_note(workout_id, ex_id, "болит плечо")
    await db.set_workout_ai_comment(workout_id, "Мощно.")
    await achievement_sync.resync(user_id)
    badges = await db.list_achievement_codes(user_id)
    assert "club220" in badges
    before = await _dump(db, workout_id)
    state = await _state(user_id)

    await history.hist_delete(_make_callback(user_id, f"hist:del:{workout_id}"), state)
    assert "club220" not in await db.list_achievement_codes(user_id)

    undo = _make_callback(user_id, f"hist:undo:{workout_id}")
    await history.hist_undo_delete(undo, state)

    assert await _dump(db, workout_id) == before
    assert await db.list_achievement_codes(user_id) == badges
    undo.answer.assert_awaited_with("Вернул тренировку.")


async def test_second_undo_tap_does_not_duplicate(fresh_db, user_id):
    db = fresh_db
    workout_id, _ = await _workout(db, user_id)
    state = await _state(user_id)
    await history.hist_delete(_make_callback(user_id, f"hist:del:{workout_id}"), state)
    await history.hist_undo_delete(_make_callback(user_id, f"hist:undo:{workout_id}"), state)

    again = _make_callback(user_id, f"hist:undo:{workout_id}")
    await history.hist_undo_delete(again, state)

    assert again.answer.await_args.kwargs.get("show_alert") is True
    assert await db.count_workouts(user_id) == 1


async def test_undo_under_an_older_deletion_is_refused(fresh_db, user_id):
    """Удалил две подряд, тапнул «Вернуть» под первой — снимок уже от второй,
    и вернуть вместо первой вторую было бы враньём."""
    db = fresh_db
    first, _ = await _workout(db, user_id, name="Жим лёжа")
    second, _ = await _workout(db, user_id, name="Присед")
    state = await _state(user_id)
    await history.hist_delete(_make_callback(user_id, f"hist:del:{first}"), state)
    await history.hist_delete(_make_callback(user_id, f"hist:del:{second}"), state)

    stale = _make_callback(user_id, f"hist:undo:{first}")
    await history.hist_undo_delete(stale, state)

    assert stale.answer.await_args.kwargs.get("show_alert") is True
    assert await db.get_workout(first) is None and await db.get_workout(second) is None


async def test_restore_rolls_back_when_an_exercise_is_gone(fresh_db, user_id):
    """Упражнение из снимка успели удалить — внешний ключ валит вставку, и
    тренировка не возвращается наполовину: ни шапки без подходов, ни части
    подходов."""
    db = fresh_db
    workout_id, _ = await _workout(db, user_id)
    snapshot = await db.snapshot_workout(workout_id)
    await db.discard_workout(workout_id)
    snapshot["sets"][0]["exercise_id"] = 10**9  # упражнения с таким id нет

    assert await db.restore_workout(snapshot) is False
    assert await db.get_workout(workout_id) is None
