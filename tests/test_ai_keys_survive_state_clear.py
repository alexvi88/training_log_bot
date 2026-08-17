"""Переписка с AI-тренером и черновик его программы переживают конец любого потока.

Черновик предложенной программы живёт только в FSM, а кнопка «Забрать программу»
остаётся в чате навсегда. Голый `state.clear()` на путях «тренировка закончена /
отменена / импорт завершён» стирал `ai_history` и `ai_program_draft` — и человек,
закрывший висящую тренировку после ответа тренера, получал по кнопке мёртвое
«предложение уже неактуально». Проверяем каждый такой путь: каркас потока
вычищен, AI-ключи целы.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

from fsm import BackfillFlow, ImportFlow, ResolveFlow, WorkoutFlow
from handlers import backfill, csv_import, exercise_resolve, sharing, workout

# То, что лежит в FSM после ответа тренера с предложенной программой: переписка
# и сам черновик, на который указывает кнопка «Забрать программу».
AI_DATA = {
    "ai_history": [{"role": "user", "content": "составь мне программу"}],
    "ai_program_draft": {"name": "Верх/низ", "days": []},
}


async def _state(user_id: int, **data) -> FSMContext:
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )
    await state.update_data(**AI_DATA, **data)
    return state


async def _assert_ai_survived(state: FSMContext) -> None:
    data = await state.get_data()
    assert {key: data.get(key) for key in AI_DATA} == AI_DATA


def _message(user_id: int, text: str | None = None):
    # spec=Message — обработчики отличают «пришли командой» от «нажали кнопку»
    # через isinstance.
    msg = MagicMock(spec=Message)
    msg.chat = SimpleNamespace(id=user_id)
    msg.message_id = 5
    msg.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    msg.text = text
    msg.reply = AsyncMock()
    msg.answer = AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=6))
    msg.bot = AsyncMock()
    return msg


def _callback(user_id: int, data: str = "x"):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 500
    message.text = "экран"
    message.photo = None
    message.edit_text = AsyncMock(return_value=SimpleNamespace(message_id=500))
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1))
    message.answer_photo = AsyncMock(
        return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=2)
    )
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.bot = AsyncMock()
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


# ---------- финализация тренировки ----------


async def test_finalize_workout_keeps_ai_draft(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)
    await db.add_set(block_id, bench, 1, 0, 100.0, 5)

    state = await _state(user_id, workout_id=workout_id, live_chat_id=user_id, live_message_id=1)
    await state.set_state(WorkoutFlow.idle)
    bot = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.bot = bot
    callback.answer = AsyncMock()

    await workout._finalize_workout(callback, state, note=None)

    assert (await db.get_workout(workout_id))["status"] == "finished"
    data = await state.get_data()
    assert data.get("workout_id") is None  # каркас тренировки вычищен
    await _assert_ai_survived(state)


# ---------- отмена пустой тренировки ----------


async def test_back_out_of_empty_workout_keeps_ai_draft(fresh_db, user_id):
    """«Назад» с первого экрана пустой тренировки удаляет её — но не черновик."""
    db = fresh_db
    workout_id = await db.create_workout(user_id)
    state = await _state(user_id, workout_id=workout_id)
    user = await db.get_user(user_id)

    await workout._back_after_cancel(_callback(user_id), state, user)

    assert await db.get_workout(workout_id) is None
    await _assert_ai_survived(state)


async def test_finish_empty_workout_keeps_ai_draft(fresh_db, user_id):
    """«Завершить» пустую (в т.ч. висящую) тренировку — ровно сценарий из аудита:
    тренер предложил программу → человек закрыл пустую тренировку → «Забрать»."""
    db = fresh_db
    workout_id = await db.create_workout(user_id)
    state = await _state(user_id, workout_id=workout_id)
    await state.set_state(WorkoutFlow.idle)

    await workout.live_finish_workout(_callback(user_id, "live:finish_workout"), state)

    assert await db.get_workout(workout_id) is None
    await _assert_ai_survived(state)


# ---------- тренировка задним числом ----------


async def test_backfill_start_keeps_ai_draft(fresh_db, user_id):
    state = await _state(user_id)

    await backfill.backfill_start(_callback(user_id, "menu:backfill_workout"), state)

    assert await state.get_state() == BackfillFlow.awaiting_date.state
    await _assert_ai_survived(state)


async def test_backfill_cancel_keeps_ai_draft(fresh_db, user_id):
    state = await _state(user_id)
    await state.set_state(BackfillFlow.awaiting_date)

    await backfill.bf_cancel(_callback(user_id, "bf:cancel"), state)

    await _assert_ai_survived(state)


# ---------- CSV-импорт ----------


async def test_csv_import_save_keeps_ai_draft(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", group_id)
    state = await _state(
        user_id,
        imp_workouts=[
            {"date": "2026-01-01", "entries": [{"name": "Жим лёжа", "sets": [(80.0, 8, None)]}]}
        ],
        imp_resolved={"Жим лёжа": bench},
    )
    await state.set_state(ImportFlow.confirming)

    await csv_import.import_save(_callback(user_id, "imp:save"), state)

    data = await state.get_data()
    assert data.get("imp_workouts") is None  # каркас импорта вычищен
    await _assert_ai_survived(state)


async def test_csv_import_cancel_keeps_ai_draft(fresh_db, user_id):
    state = await _state(user_id, imp_workouts=[])
    await state.set_state(ImportFlow.confirming)

    await csv_import.import_cancel(_callback(user_id, "imp:cancel"), state)

    await _assert_ai_survived(state)


# ---------- переход по shared-ссылке ----------


async def test_open_shared_link_keeps_ai_draft(fresh_db, user_id):
    state = await _state(user_id)

    await sharing.open_shared(
        _message(user_id, "/start sh_nope"),
        CommandObject(command="start", args="sh_nope"),
        state,
    )

    await _assert_ai_survived(state)


# ---------- отмена резолва имён из импорта ----------


async def test_resolve_cancel_all_keeps_ai_draft(fresh_db, user_id):
    state = await _state(user_id, resolve_pending=["Жим лёжа"], resolve_resolved={})
    await state.set_state(ResolveFlow.picking)

    await exercise_resolve.resolve_cancel_all(_callback(user_id, "resolve:cancelall"), state)

    data = await state.get_data()
    assert data.get("resolve_pending") is None  # каркас резолва вычищен
    await _assert_ai_survived(state)
