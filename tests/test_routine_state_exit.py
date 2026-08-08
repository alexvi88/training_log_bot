"""Ушёл с экрана ввода — бот больше не считает следующую реплику названием.

Экраны программ, которые ждут текста («как назвать программу», «как назвать
день», «новое название», «схема подходов»), уводят «❌ Отмена» на обычный экран:
то на rt:view, то на rt:prg, то на rt:manage. Состояние FSM при этом оставалось
висеть, и следующее написанное человеком сообщение молча становилось названием:
«а сколько мне есть белка?» превращалось в название дня, «спасибо, пока» — в
название программы. Хранилище файловое (fsm_storage.py), так что это переживало
и перезапуск, и сутки.

Тесты гоняют события через сам роутер, а не вызывают хендлеры напрямую:
проверяется именно то, кто заберёт следующее сообщение — фильтры и middleware, а
не тело функции.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

from fsm import RoutineFlow, WorkoutFlow
from handlers import routines

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.answer = AsyncMock(
        return_value=SimpleNamespace(message_id=1, chat=SimpleNamespace(id=user_id))
    )
    message.delete = AsyncMock()
    message.edit_text = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.bot = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _make_message(user_id: int, text: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.from_user = SimpleNamespace(id=user_id, username="tester")
    message.text = text
    message.answer = AsyncMock(
        return_value=SimpleNamespace(message_id=1, chat=SimpleNamespace(id=user_id))
    )
    message.reply = AsyncMock()
    return message


async def _state(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


async def _feed(update_type: str, event, state: FSMContext):
    """Отдать событие роутеру целиком — с фильтрами и middleware.

    `raw_state` кладём руками: в бою его в data подсовывает FSMContextMiddleware
    диспетчера, а без него StateFilter считает, что состояния нет вовсе, и
    хендлеры потока не совпали бы независимо от того, снято состояние или нет.
    """
    return await routines.router.propagate_event(
        update_type, event, state=state, raw_state=await state.get_state()
    )


def _last_text(callback) -> str:
    """Чем экран реально отрисовался: ui.safe_edit либо правит сообщение, либо
    отправляет новое."""
    calls = [
        c for mock in (callback.message.edit_text, callback.message.answer)
        for c in mock.await_args_list
    ]
    return calls[-1].args[0] if calls and calls[-1].args else ""


async def _program(db, user_id, name="PPL", days=("Толкай", "Тяни")):
    program_id = await db.create_program(user_id, name)
    for day in days:
        await db.create_routine(user_id, day, program_id=program_id)
    return program_id


# ---------- отмена на переименовании ----------


async def test_cancelling_a_day_rename_leaves_the_next_message_alone(fresh_db, user_id):
    """Ровно воспроизведённый баг: «❌ Отмена» → «а сколько мне есть белка?» →
    день назывался вопросом про белок."""
    db = fresh_db
    program_id = await _program(db, user_id)
    day = (await db.list_program_days_by_id(program_id))[0]
    state = await _state(user_id)

    await _feed("callback_query", _make_callback(user_id, f"rt:rename:{day['id']}"), state)
    assert await state.get_state() == RoutineFlow.renaming
    # «❌ Отмена» на этом экране — это кнопка возврата на сам день.
    await _feed("callback_query", _make_callback(user_id, f"rt:view:{day['id']}"), state)

    await _feed("message", _make_message(user_id, "а сколько мне есть белка?"), state)

    assert (await db.get_routine(day["id"]))["name"] == "Толкай"
    assert await state.get_state() is None


async def test_cancelling_a_program_rename_leaves_the_next_message_alone(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id)
    state = await _state(user_id)

    await _feed("callback_query", _make_callback(user_id, f"rt:pgmrename:{program_id}"), state)
    assert await state.get_state() == RoutineFlow.renaming_program
    await _feed("callback_query", _make_callback(user_id, f"rt:prg:{program_id}"), state)

    await _feed("message", _make_message(user_id, "спасибо, пока"), state)

    assert (await db.get_program(program_id))["name"] == "PPL"
    assert await state.get_state() is None


async def test_cancelling_a_new_day_does_not_create_one_from_the_next_message(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id)
    state = await _state(user_id)

    await _feed("callback_query", _make_callback(user_id, f"rt:dayblank:{program_id}"), state)
    assert await state.get_state() == RoutineFlow.naming_day
    await _feed("callback_query", _make_callback(user_id, f"rt:prg:{program_id}"), state)

    await _feed("message", _make_message(user_id, "ок, потом соберу"), state)

    assert [d["name"] for d in await db.list_program_days_by_id(program_id)] == ["Толкай", "Тяни"]


async def test_cancelling_a_scheme_edit_does_not_write_the_next_message_as_a_scheme(
    fresh_db, user_id
):
    """У «схемы подходов» отмены на экране нет вовсе — уйти можно только чем-то
    ещё, и это тоже должно снимать состояние."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    routine_id = await db.create_routine(user_id, "Толкай")
    await db.add_routine_exercise(routine_id, ex_id, 0, "3×10")
    entry = (await db.list_routine_exercises(routine_id))[0]
    state = await _state(user_id)

    await _feed(
        "callback_query",
        _make_callback(user_id, f"rt:extarget:{routine_id}:{entry['id']}"),
        state,
    )
    assert await state.get_state() == RoutineFlow.editing_exercise_target
    await _feed("callback_query", _make_callback(user_id, f"rt:view:{routine_id}"), state)

    await _feed("message", _make_message(user_id, "а когда лучше кардио?"), state)

    assert (await db.list_routine_exercises(routine_id))[0]["target"] == "3×10"


@pytest.mark.parametrize("state_name", RoutineFlow.__all_states_names__)
async def test_any_screen_button_drops_any_input_state(fresh_db, user_id, state_name):
    """Один и тот же выход обязан работать для всех состояний потока, включая те,
    что появятся позже: снимается оно в middleware, а не в каждой кнопке."""
    db = fresh_db
    routine_id = await db.create_routine(user_id, "Толкай")
    state = await _state(user_id)
    await state.set_state(state_name)

    await _feed("callback_query", _make_callback(user_id, f"rt:view:{routine_id}"), state)

    assert await state.get_state() is None


async def test_a_button_inside_the_flow_keeps_the_state(fresh_db, user_id):
    """Обратная сторона: «📋 Шаблоны» посреди добавления упражнения — не уход, и
    если снять состояние здесь, следующий экран станет мёртвым (его кнопки стоят
    под StateFilter)."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    routine_id = await db.create_routine(user_id, "Толкай")
    state = await _state(user_id)
    await state.set_state(RoutineFlow.adding_exercise_pick)
    await state.update_data(rtadd_routine_id=routine_id, rtadd_group_id=group_id)

    await _feed("callback_query", _make_callback(user_id, "rtadd:catalog"), state)

    assert await state.get_state() == RoutineFlow.adding_exercise_pick


async def test_an_open_workout_survives_leaving_a_routine_screen(fresh_db, user_id):
    """Состояние снимается только своё: тапнуть кнопку программы можно и посреди
    незакрытой тренировки, а `state.clear()` снёс бы каркас открытых упражнений —
    «Продолжить» после этого восстанавливает только последнее."""
    db = fresh_db
    routine_id = await db.create_routine(user_id, "Толкай")
    state = await _state(user_id)
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(workout_id=7, open_exercises=[1, 2], active_exercise_id=2)

    await _feed("callback_query", _make_callback(user_id, f"rt:view:{routine_id}"), state)

    assert await state.get_state() == WorkoutFlow.logging_set
    assert (await state.get_data())["open_exercises"] == [1, 2]


# ---------- «из тренировки»: тот же уход, только черновик лежит в data ----------


async def test_backing_out_of_add_a_day_stops_the_next_snapshot_joining_that_program(
    fresh_db, user_id
):
    """«➕ Добавить день → 🏋️ Из тренировки», потом «⬅️ Назад» к списку и
    «➕ Из тренировки» — новая программа молча становилась ещё одним днём той
    программы, из которой человек уже ушёл: пометка о дне оставалась в data."""
    db = fresh_db
    program_id = await _program(db, user_id, days=("Толкай",))
    workout_id = await db.create_finished_workout(
        user_id, "2026-07-15T10:00:00", "2026-07-15T11:00:00"
    )
    state = await _state(user_id)

    await _feed("callback_query", _make_callback(user_id, f"rt:daypickw:{program_id}:0"), state)
    # «⬅️ Назад» из выбора тренировки ведёт к списку программ — это уход.
    await _feed("callback_query", _make_callback(user_id, "rt:manage"), state)

    ask = _make_callback(user_id, f"rt:pickw:use:{workout_id}")
    await _feed("callback_query", ask, state)
    await _feed("message", _make_message(user_id, "Своя тренировка"), state)

    assert "программу" in _last_text(ask)
    assert [d["name"] for d in await db.list_program_days_by_id(program_id)] == ["Толкай"]
    assert [r["name"] for r in await db.list_standalone_routines(user_id)] == ["Своя тренировка"]
