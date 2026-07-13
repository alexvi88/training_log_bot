"""handlers.ai_trainer: keyboard helper that reacts to an active workout, and the
'К тренировке' button that resumes it without wiping the AI chat history.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from handlers import ai_trainer

pytestmark = pytest.mark.asyncio


def _callbacks(kb) -> list[str]:
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1))
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=2))
    bot.send_chat_action = AsyncMock()
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


async def test_ai_keyboard_shows_menu_without_active_workout(fresh_db, user_id):
    kb = await ai_trainer.ai_keyboard(user_id)
    assert "ai:menu" in _callbacks(kb)


async def test_ai_keyboard_shows_resume_workout_with_active_workout(fresh_db, user_id):
    await fresh_db.create_workout(user_id, started_at="2026-07-13T10:00:00", status="active")
    kb = await ai_trainer.ai_keyboard(user_id)
    callbacks = _callbacks(kb)
    assert "ai:resume_workout" in callbacks
    assert "ai:menu" in callbacks


async def test_ai_resume_workout_does_not_delete_ai_chat_message(fresh_db, user_id):
    """The AI chat message the button is on must stay in the chat, unlike menu:resume_workout."""
    await fresh_db.create_workout(user_id, started_at="2026-07-13T10:00:00", status="active")
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "ai:resume_workout")

    await ai_trainer.ai_resume_workout(callback, state)

    callback.message.delete.assert_not_awaited()
    callback.answer.assert_awaited()


async def test_ai_resume_workout_alerts_when_no_active_workout(fresh_db, user_id):
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "ai:resume_workout")

    await ai_trainer.ai_resume_workout(callback, state)

    callback.message.delete.assert_not_awaited()
    callback.answer.assert_awaited_once_with("Нет активной тренировки", show_alert=True)


# ---------- voice input (ai_voice_question) ----------


def _make_voice_message(user_id: int, duration: int = 5, file_size: int = 1000, download_result=None):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.voice = SimpleNamespace(file_id="voice_1", duration=duration, file_size=file_size)
    message.reply = AsyncMock()
    bot = MagicMock()
    bot.download = AsyncMock(return_value=download_result if download_result is not None else SimpleNamespace())
    message.bot = bot
    return message


async def test_ai_voice_question_defers_when_busy(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer, "_busy", {user_id})
    message = _make_voice_message(user_id)
    state = await _make_state(user_id)

    await ai_trainer.ai_voice_question(message, state)

    message.reply.assert_awaited_once()
    assert "Секунду" in message.reply.await_args.args[0]


async def test_ai_voice_question_hints_when_not_configured(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer.ai_trainer, "is_voice_configured", lambda: False)
    message = _make_voice_message(user_id)
    state = await _make_state(user_id)

    await ai_trainer.ai_voice_question(message, state)

    message.reply.assert_awaited_once()
    assert "текстом" in message.reply.await_args.args[0]


async def test_ai_voice_question_rejects_too_long_voice(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer.ai_trainer, "is_voice_configured", lambda: True)
    message = _make_voice_message(user_id, duration=ai_trainer.MAX_VOICE_SECONDS + 1)
    state = await _make_state(user_id)

    await ai_trainer.ai_voice_question(message, state)

    message.reply.assert_awaited_once()
    assert "длинное" in message.reply.await_args.args[0]
    message.bot.download.assert_not_awaited()


async def test_ai_voice_question_rejects_too_large_file(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer.ai_trainer, "is_voice_configured", lambda: True)
    message = _make_voice_message(user_id, file_size=ai_trainer.MAX_VOICE_BYTES + 1)
    state = await _make_state(user_id)

    await ai_trainer.ai_voice_question(message, state)

    message.reply.assert_awaited_once()
    assert "большое" in message.reply.await_args.args[0]
    message.bot.download.assert_not_awaited()


async def test_ai_voice_question_reports_transcription_failure(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer.ai_trainer, "is_voice_configured", lambda: True)

    async def boom(_file):
        raise RuntimeError("openai exploded")

    monkeypatch.setattr(ai_trainer.ai_trainer, "transcribe_voice", boom)
    message = _make_voice_message(user_id)
    state = await _make_state(user_id)

    await ai_trainer.ai_voice_question(message, state)

    message.reply.assert_awaited_once()
    assert "распознать" in message.reply.await_args.args[0]


async def test_ai_voice_question_rejects_empty_transcription(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer.ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(ai_trainer.ai_trainer, "transcribe_voice", AsyncMock(return_value=""))
    message = _make_voice_message(user_id)
    state = await _make_state(user_id)

    await ai_trainer.ai_voice_question(message, state)

    message.reply.assert_awaited_once()
    assert "разобрать" in message.reply.await_args.args[0]


async def test_ai_voice_question_forwards_transcribed_text_as_question(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer.ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(ai_trainer.ai_trainer, "transcribe_voice", AsyncMock(return_value="как мой прогресс"))
    handle_question = AsyncMock()
    monkeypatch.setattr(ai_trainer, "_handle_question", handle_question)
    message = _make_voice_message(user_id)
    state = await _make_state(user_id)

    await ai_trainer.ai_voice_question(message, state)

    message.reply.assert_not_awaited()
    handle_question.assert_awaited_once_with(
        message, state, "как мой прогресс", history_question="как мой прогресс"
    )
