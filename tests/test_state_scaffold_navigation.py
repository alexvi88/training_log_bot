"""Уйти посреди тренировки в дневник еды, достижения, отзыв или /mcp — и вернуться
к своим подходам.

Голый `state.clear()` в этих разделах сносил каркас незакрытой тренировки:
открытые упражнения с их блоками, остаток плана программы, переписку с
AI-тренером. По базе восстанавливается лишь последнее упражнение (см.
handlers.workout._reopen_exercises), а плана и переписки в базе нет вовсе — так
что человек, записавший в перерыве завтрак, возвращался в трекер на пустой экран.
Проверяем поведением: после каждого такого перехода «Продолжить» открывает ровно
то, что было открыто.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

import config
from fsm import FoodDiaryFlow, WorkoutFlow
from handlers import food_diary, history, mcp_access, workout

# Каркас ровно того вида, что живёт в FSM посреди суперсета по программе: два
# открытых упражнения, активное из них, остаток плана и начатая переписка с
# тренером.
SCAFFOLD = {
    "open_exercises": [7, 9],
    "open_blocks": {7: 70, 9: 90},
    "active_exercise_id": 9,
    "last_by_exercise": {7: (45.0, 8)},
    "last_session_sets": {7: [], 9: []},
    "weight_steps": {7: 2.5},
    "planned_blocks": [{"exercise_id": 11}],
    "exercise_targets": {11: "3x8"},
    "confirmed_weights": {11: 60.0},
    "ai_history": [{"role": "user", "content": "что делать с жимом"}],
    "ai_program_draft": {"name": "Верх/низ"},
}


async def _state_mid_workout(user_id: int, workout_id: int = 42) -> FSMContext:
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(workout_id=workout_id, **SCAFFOLD)
    return state


async def _assert_scaffold_survived(state: FSMContext, workout_id: int = 42) -> None:
    data = await state.get_data()
    assert data.get("workout_id") == workout_id
    assert {key: data.get(key) for key in SCAFFOLD} == SCAFFOLD


def _message(user_id: int):
    # spec=Message — по нему middleware отличает сообщение от прочих апдейтов.
    msg = MagicMock(spec=Message)
    msg.chat = SimpleNamespace(id=user_id)
    msg.from_user = SimpleNamespace(id=user_id, username="tester")
    msg.bot = AsyncMock()
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=12))
    msg.reply = AsyncMock()
    return msg


def _callback(user_id: int, data: str):
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
    # spec=CallbackQuery — обработчики различают «пришли командой» и «нажали
    # кнопку» через isinstance, голый MagicMock прошёл бы как сообщение.
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.bot = AsyncMock()
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


# ---------- дневник еды ----------


async def test_food_diary_command_keeps_open_workout(fresh_db, user_id):
    state = await _state_mid_workout(user_id)

    await food_diary.cmd_food_diary(_message(user_id), state)

    assert await state.get_state() == FoodDiaryFlow.viewing.state
    await _assert_scaffold_survived(state)


async def test_food_diary_menu_button_keeps_open_workout(fresh_db, user_id):
    state = await _state_mid_workout(user_id)

    await food_diary.menu_food_diary(_callback(user_id, "menu:food"), state)

    await _assert_scaffold_survived(state)


async def test_leaving_food_diary_to_the_menu_keeps_open_workout(fresh_db, user_id):
    """«🏠 Меню» с экрана дня: меню собирает сам workout._show_main_menu, и каркас
    он бережёт — а стоявший до него state.clear() успевал его снести."""
    workout_id = await fresh_db.create_workout(user_id)
    state = await _state_mid_workout(user_id, workout_id)

    await food_diary.fd_menu(_callback(user_id, "fd:menu"), state)

    await _assert_scaffold_survived(state, workout_id)


# ---------- достижения ----------


async def test_achievements_screen_keeps_open_workout(fresh_db, user_id):
    state = await _state_mid_workout(user_id)

    await history.menu_achievements(_callback(user_id, "menu:achievements"), state)

    await _assert_scaffold_survived(state)


# ---------- /mcp ----------


@pytest.fixture
def mcp_enabled(monkeypatch):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "https://training-log.example.com")
    monkeypatch.setattr(config, "MCP_ENABLED", True)


async def test_mcp_screen_keeps_open_workout(fresh_db, user_id, mcp_enabled):
    state = await _state_mid_workout(user_id)

    await mcp_access.cmd_mcp(_message(user_id), state)

    await _assert_scaffold_survived(state)


async def test_mcp_link_code_screen_keeps_open_workout(fresh_db, user_id, mcp_enabled):
    state = await _state_mid_workout(user_id)

    await mcp_access.mcp_code(_callback(user_id, "mcp:code"), state)

    await _assert_scaffold_survived(state)


# ---------- и, собственно, возврат к подходам ----------


async def test_resume_after_the_detour_reopens_the_whole_superset(fresh_db, user_id):
    """Сквозная проверка: записал в перерыве завтрак — и «Продолжить» вернуло оба
    открытых упражнения, а не одно последнее из базы."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Руки")
    triceps = await db.create_exercise(user_id, "Разгибания", group_id)
    biceps = await db.create_exercise(user_id, "Сгибания", group_id)
    workout_id = await db.create_workout(user_id)
    triceps_block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(triceps_block, triceps, 0)
    await db.add_set(triceps_block, triceps, 1, 0, 45.0, 7)
    biceps_block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(biceps_block, biceps, 0)

    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        workout_id=workout_id,
        open_exercises=[triceps, biceps],
        open_blocks={triceps: triceps_block, biceps: biceps_block},
        active_exercise_id=biceps,
        last_by_exercise={triceps: (45.0, 7)},
        last_session_sets={triceps: [], biceps: []},
    )

    await food_diary.cmd_food_diary(_message(user_id), state)
    await workout.resume_workout(_callback(user_id, "menu:resume_workout"), state)

    data = await state.get_data()
    assert data["open_exercises"] == [triceps, biceps]
    assert data["open_blocks"] == {triceps: triceps_block, biceps: biceps_block}
    assert data["active_exercise_id"] == biceps
