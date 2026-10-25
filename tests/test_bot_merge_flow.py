"""«🔗 Объединить» → «Разъединить» в боте (handlers.exercises).

Обработчики подтверждения объединения и кнопки «Разъединить» под итогом не
исполнялись ни разу: именно они сносят упражнение со всей историей в другое
и возвращают обратно. Здесь — что кнопка с чужим id ничего не трогает и
называет причину, что под итогом действительно есть «Разъединить» с журналом
этого объединения, и что эта кнопка возвращает подходы.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from fsm import ExerciseManage
from handlers import exercises

pytestmark = pytest.mark.asyncio


def _callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=5, chat=SimpleNamespace(id=user_id)))
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    message.answer_photo = AsyncMock()
    message.answer_media_group = AsyncMock(return_value=[])
    bot = MagicMock()
    bot.send_media_group = AsyncMock(return_value=[])
    bot.send_photo = AsyncMock()
    bot.delete_message = AsyncMock()
    message.bot = bot
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _state(user_id, source_id):
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(ExerciseManage.picking_merge_target)
    await state.update_data(exm_merge_source_id=source_id)
    return state


async def _with_set(db, user_id, exercise_id):
    workout = await db.create_finished_workout(
        user_id, started_at="2026-10-01T10:00:00", finished_at="2026-10-01T11:00:00"
    )
    block = await db.create_block(workout, "single")
    await db.add_block_exercise(block, exercise_id, 0)
    await db.add_set(block, exercise_id, 0, 0, 60.0, 10, None)


async def test_confirm_with_a_foreign_target_changes_nothing(fresh_db, user_id):
    db = fresh_db
    await db.get_or_create_user(telegram_id=222, username="other")
    mine = await db.create_exercise(user_id, "Мостик", await db.create_muscle_group(user_id, "Ноги"))
    foreign = await db.create_exercise(222, "Чужой мостик", await db.create_muscle_group(222, "Ноги"))
    await _with_set(db, user_id, mine)
    callback = _callback(user_id, f"exm:mergeyes:{foreign}")

    await exercises.exm_merge_confirm(callback, await _state(user_id, mine))

    assert callback.answer.await_args.kwargs.get("show_alert") is True
    assert await db.get_exercise(mine) is not None
    assert len(await db.list_sets_for_exercise(mine)) == 1


async def test_confirm_into_archived_target_names_the_reason(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Ноги")
    source = await db.create_exercise(user_id, "Мостик", gid)
    target = await db.create_exercise(user_id, "Ягодичный мост", gid)
    await db.archive_exercise(target)
    callback = _callback(user_id, f"exm:mergeyes:{target}")

    await exercises.exm_merge_confirm(callback, await _state(user_id, source))

    text = callback.answer.await_args.args[0]
    assert callback.answer.await_args.kwargs.get("show_alert") is True
    assert await db.get_exercise(source) is not None
    assert text  # причина названа, а не пустой тост


async def test_merge_then_unmerge_from_the_buttons(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Ноги")
    source = await db.create_exercise(user_id, "Мостик", gid)
    target = await db.create_exercise(user_id, "Ягодичный мост", gid)
    await _with_set(db, user_id, source)
    callback = _callback(user_id, f"exm:mergeyes:{target}")
    state = await _state(user_id, source)

    await exercises.exm_merge_confirm(callback, state)

    assert await db.get_exercise(source) is None
    assert len(await db.list_sets_for_exercise(target)) == 1
    # Под итогом — «Разъединить» с журналом именно этого объединения.
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    unmerge = next(
        b.callback_data for row in kb.inline_keyboard for b in row
        if b.callback_data.startswith("exm:unmerge:")
    )
    assert (await state.get_data())["exm_merge_source_id"] is None

    back = _callback(user_id, unmerge)
    await exercises.exm_unmerge(back, state)

    assert (await db.get_exercise(source))["display_name"] == "Мостик"
    assert len(await db.list_sets_for_exercise(source)) == 1
    assert await db.list_sets_for_exercise(target) == []

    # Второй тап по той же кнопке — отказ, а не вторая копия.
    again = _callback(user_id, unmerge)
    await exercises.exm_unmerge(again, state)
    assert again.answer.await_args.kwargs.get("show_alert") is True


async def test_unmerge_button_of_someone_else_is_refused(fresh_db, user_id):
    db = fresh_db
    await db.get_or_create_user(telegram_id=222, username="other")
    gid = await db.create_muscle_group(222, "Ноги")
    keep = await db.create_exercise(222, "Мост", gid)
    drop = await db.create_exercise(222, "Мостик", gid)
    journal: dict = {}
    await db.merge_exercises(222, keep, drop, journal_out=journal)
    callback = _callback(user_id, f"exm:unmerge:{journal['merge_id']}")

    await exercises.exm_unmerge(callback, await _state(user_id, None))

    assert callback.answer.await_args.kwargs.get("show_alert") is True
    assert await db.get_exercise(drop) is None
