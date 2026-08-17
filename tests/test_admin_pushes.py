"""«/pushes» отвечал TelegramBadRequest: message is too long.

Прод: 10 пушей с AI-комментарием на несколько абзацев каждый (лимит Telegram —
4096 символов на сообщение) — вместо списка приходило «Что-то пошло не так».
Каждая запись теперь режется, так что 10 в одном сообщении укладываются всегда,
сколько бы ни было текста у пуша.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

import config
from handlers import admin

pytestmark = pytest.mark.asyncio


async def _state(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


def _make_message(user_id: int):
    message = MagicMock(spec=Message)
    message.from_user = SimpleNamespace(id=user_id, username="admin", language_code=None)
    message.answer = AsyncMock()
    return message


async def test_pushes_list_stays_within_the_telegram_message_limit(
    fresh_db, user_id, monkeypatch
):
    monkeypatch.setattr(config, "ADMIN_ID", user_id)
    # Реалистичный длинный AI-комментарий — несколько абзацев, как в проде.
    long_comment = "Хороший темп. " * 400
    for _ in range(10):
        await fresh_db.record_push(user_id, "workout_comment", long_comment, "2026-08-08")

    message = _make_message(user_id)
    await admin.cmd_pushes(message, await _state(user_id))

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert len(text) <= 4096


async def test_pushes_list_still_shows_the_text_body(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", user_id)
    await fresh_db.record_push(user_id, "workout_comment", "Отличная тренировка!", "2026-08-08")

    message = _make_message(user_id)
    await admin.cmd_pushes(message, await _state(user_id))

    text = message.answer.await_args.args[0]
    assert "Отличная тренировка!" in text
