"""Снесённый аккаунт не воскресает по кнопке из старого сообщения.

Ровно так и выглядела жалоба «очистил всю историю, а программа осталась».
Черновик программы от тренера живёт не в базе, а в FSM, и /admin_wipe его не
трогал; сообщение с карточкой висит в чате вечно и остаётся тапабельным. Тап по
«✅ Добавить себе» под старой карточкой заводил программу заново — уже после
сноса, на пустой истории. Снаружи это и выглядит как «историю снесло, а
программа осталась».

Пара тестов ниже держит обе половины: до сноса кнопка работает (иначе второй
тест зеленел бы по любой причине), после — не создаёт ничего.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

import ai_trainer
from fsm_storage import JSONFileStorage
from handlers import admin
from handlers import ai_trainer as ai_handler

pytestmark = pytest.mark.asyncio

TEMPLATE_A = "Жим штанги лёжа"
TEMPLATE_B = "Присед со штангой"
DRAFT_ID = "abc"


async def _draft_in_state(user_id: int, tmp_path) -> tuple[FSMContext, dict]:
    """Предложение тренера — как в проде: черновик в файловом FSM, карточка в чате.

    Хранилище именно файловое (JSONFileStorage), а не MemoryStorage: пережить
    снос аккаунта черновик мог как раз потому, что лежит на диске.
    """
    captured: list[dict] = []

    async def on_program(draft: dict) -> None:
        captured.append(draft)

    await ai_trainer.execute_tool(
        user_id,
        "propose_program",
        {
            "name": "Масса 4× верх/низ",
            "days": [
                {"name": "Верх А", "exercises": [{"name": TEMPLATE_A, "sets": 4, "reps_min": 5, "reps_max": 12}]},
                {"name": "Низ А", "exercises": [{"name": TEMPLATE_B, "sets": 3, "reps_min": 5, "reps_max": 12}]},
            ],
        },
        on_program=on_program,
    )
    draft = captured[-1]
    draft["id"] = DRAFT_ID
    state = FSMContext(
        storage=JSONFileStorage(str(tmp_path / "fsm.json")),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )
    await state.update_data(ai_program_draft=draft)
    return state, draft


def _card_tap(user_id: int):
    """Тап по «✅ Добавить себе» под карточкой, которая висит в чате с прошлого раза."""
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.edit_text = AsyncMock(return_value=message)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.data = f"ai:prog:save:{DRAFT_ID}"
    callback.answer = AsyncMock()
    return callback


async def test_card_saves_the_program_while_the_account_is_alive(fresh_db, user_id, tmp_path):
    state, _ = await _draft_in_state(user_id, tmp_path)

    await ai_handler.ai_program_save(_card_tap(user_id), state)

    (program,) = await fresh_db.list_programs(user_id)
    assert program["name"] == "Масса 4× верх/низ"
    assert len(await fresh_db.list_program_days_by_id(program["id"])) == 2


async def test_stale_card_resurrects_nothing_after_the_wipe(fresh_db, user_id, tmp_path):
    state, _ = await _draft_in_state(user_id, tmp_path)

    await fresh_db.wipe_user_account(user_id)
    await admin._forget_user_outside_db(state, user_id)

    callback = _card_tap(user_id)
    await ai_handler.ai_program_save(callback, state)

    assert await fresh_db.list_programs(user_id) == []
    assert await fresh_db.list_routines(user_id) == []
    assert await fresh_db.get_user(user_id) is None
    # Человеку сказали, что черновика больше нет, а не сохранили молча.
    assert callback.answer.await_args.kwargs.get("show_alert") is True


async def test_wipe_empties_the_draft_on_disk_too(fresh_db, user_id, tmp_path):
    """Не только в памяти: файл переживает перезапуск, значит и снос должен."""
    path = tmp_path / "fsm.json"
    state, _ = await _draft_in_state(user_id, tmp_path)
    assert json.loads(path.read_text())  # черновик там был

    await fresh_db.wipe_user_account(user_id)
    await admin._forget_user_outside_db(state, user_id)

    assert json.loads(path.read_text()) == {}
    assert await state.get_data() == {}
