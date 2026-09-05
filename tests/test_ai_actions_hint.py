"""Одноразовая подсказка под первым ответом AI-тренера: кроме вопросов он умеет
и действия с данными — записать вес, завести упражнение, посчитать еду. Один
раз за всю жизнь аккаунта, хвостом к ответу (не отдельным сообщением) и без
единого вызова модели — см. handlers.ai_trainer.ACTIONS_HINT_TEXT."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from handlers import ai_trainer

pytestmark = pytest.mark.asyncio

_HINT = "могу сам занести"


def _make_chat_message(user_id: int, text: str):
    """Сообщение пользователя, чей .answer() отдаёт placeholder «думаю…» —
    финальный ответ тренера приезжает правкой этого placeholder'а."""
    message = MagicMock()
    message.text = text
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.reply = AsyncMock()
    placeholder = MagicMock()
    placeholder.edit_text = AsyncMock()
    placeholder.chat = SimpleNamespace(id=user_id)
    placeholder.message_id = 9
    message.answer = AsyncMock(return_value=placeholder)
    # AsyncMock, not MagicMock: _handle_question awaits bot.set_message_reaction
    # (the 👀 acknowledgement) on every question.
    message.bot = AsyncMock()
    return message


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=MemoryStorage(), key=key)
    await state.set_state("AITrainerFlow:chatting")
    return state


def _final_text(message) -> str:
    return message.answer.return_value.edit_text.await_args.args[0]


async def test_hint_arrives_glued_to_the_first_answer(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", AsyncMock(return_value="Растёт."))
    state = await _make_state(user_id)
    message = _make_chat_message(user_id, "как жим?")

    await ai_trainer.ai_question(message, state)

    assert _HINT in _final_text(message)
    # Хвост, а не пуш: единственный message.answer — это placeholder «думаю…»,
    # подсказка приехала правкой его же, отдельного сообщения нет.
    message.answer.assert_awaited_once()


async def test_hint_does_not_repeat_on_the_second_answer(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", AsyncMock(return_value="Растёт."))
    state = await _make_state(user_id)

    first = _make_chat_message(user_id, "как жим?")
    await ai_trainer.ai_question(first, state)
    assert _HINT in _final_text(first)

    second = _make_chat_message(user_id, "а тяга?")
    await ai_trainer.ai_question(second, state)
    assert _HINT not in _final_text(second)


async def test_failed_answer_saves_the_hint_for_the_first_real_one(fresh_db, user_id, monkeypatch):
    """Сбой провайдера — не ответ: подсказка не сгорает вместе с ним и приходит
    под первым состоявшимся ответом."""
    monkeypatch.setattr(
        ai_trainer.ai_trainer, "ask", AsyncMock(side_effect=RuntimeError("provider is down"))
    )
    state = await _make_state(user_id)
    failed = _make_chat_message(user_id, "как жим?")
    await ai_trainer.ai_question(failed, state)
    assert _HINT not in _final_text(failed)

    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", AsyncMock(return_value="Растёт."))
    ok = _make_chat_message(user_id, "как жим?")
    await ai_trainer.ai_question(ok, state)
    assert _HINT in _final_text(ok)


# ---------- db.claim_ai_actions_hint напрямую ----------


async def test_claim_fires_exactly_once_per_account(fresh_db, user_id):
    assert await fresh_db.claim_ai_actions_hint(user_id) is True
    assert await fresh_db.claim_ai_actions_hint(user_id) is False
    assert await fresh_db.claim_ai_actions_hint(user_id) is False


async def test_claim_for_an_unknown_user_is_a_quiet_no(fresh_db):
    assert await fresh_db.claim_ai_actions_hint(999_999) is False
