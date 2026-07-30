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

    # What was heard is echoed back, so a misheard question doesn't make the
    # answer look like the trainer inventing things.
    message.reply.assert_awaited_once()
    assert "как мой прогресс" in message.reply.await_args.args[0]
    handle_question.assert_awaited_once_with(
        message, state, "как мой прогресс", history_question="как мой прогресс"
    )


def _make_message(user_id: int, text: str):
    message = MagicMock()
    message.text = text
    message.from_user = SimpleNamespace(id=user_id, username="tester")
    message.reply = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=9))
    message.bot = MagicMock()
    return message


async def test_ai_question_daily_limit_blocks_before_calling_model(fresh_db, user_id, monkeypatch):
    import config

    for _ in range(config.AI_QUESTION_DAILY_LIMIT):
        await fresh_db.increment_ai_question_count(user_id)

    called = False

    async def fake_ask(*args, **kwargs):
        nonlocal called
        called = True
        return "should not run"

    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", fake_ask)

    state = await _make_state(user_id)
    await state.set_state("AITrainerFlow:chatting")
    message = _make_message(user_id, "как жим?")
    await ai_trainer.ai_question(message, state)

    assert called is False
    message.reply.assert_awaited_once()
    assert "лимит" in message.reply.await_args.args[0].lower()


def _make_chat_message(user_id: int, text: str):
    """_make_message with a placeholder that can actually be edited — the answer
    path edits the "думаю…" message in place."""
    message = _make_message(user_id, text)
    placeholder = MagicMock()
    placeholder.edit_text = AsyncMock()
    placeholder.chat = SimpleNamespace(id=user_id)
    placeholder.message_id = 9
    message.answer = AsyncMock(return_value=placeholder)
    return message


async def test_failed_question_does_not_spend_the_daily_quota(fresh_db, user_id, monkeypatch):
    """A provider outage shouldn't cost the user one of their daily questions —
    the counter used to be charged before the request and never refunded."""
    async def failing_ask(*args, **kwargs):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", failing_ask)

    state = await _make_state(user_id)
    await state.set_state("AITrainerFlow:chatting")
    message = _make_chat_message(user_id, "как жим?")

    await ai_trainer.ai_question(message, state)

    assert await fresh_db.get_ai_question_count_today(user_id) == 0


async def test_successful_question_spends_exactly_one(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", AsyncMock(return_value="растёт"))

    state = await _make_state(user_id)
    await state.set_state("AITrainerFlow:chatting")
    message = _make_chat_message(user_id, "как жим?")

    await ai_trainer.ai_question(message, state)

    assert await fresh_db.get_ai_question_count_today(user_id) == 1


async def test_reentering_the_trainer_keeps_the_conversation(fresh_db, user_id, monkeypatch):
    """Stepping out to the menu and back used to reset the trainer to its intro
    with no memory of what was just discussed."""
    from aiogram.types import CallbackQuery

    monkeypatch.setattr(ai_trainer.ai_trainer, "is_configured", lambda: True)
    state = await _make_state(user_id)
    history = [
        {"role": "user", "content": "как жим?"},
        {"role": "assistant", "content": "растёт"},
    ]
    await state.update_data(ai_history=history)

    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 3
    message.text = "меню"
    message.photo = None
    message.delete = AsyncMock()
    message.edit_text = AsyncMock(return_value=True)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=4))
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = "menu:ai"
    callback.answer = AsyncMock()

    await ai_trainer.menu_ai(callback, state)

    assert (await state.get_data())["ai_history"] == history
    sent = message.answer.await_args
    text = sent.args[0] if sent.args else sent.kwargs["text"]
    assert "Продолжаем" in text  # the resume line, not the full intro


async def test_first_entry_still_shows_the_intro(fresh_db, user_id, monkeypatch):
    from aiogram.types import CallbackQuery

    monkeypatch.setattr(ai_trainer.ai_trainer, "is_configured", lambda: True)
    state = await _make_state(user_id)

    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 3
    message.text = "меню"
    message.photo = None
    message.delete = AsyncMock()
    message.edit_text = AsyncMock(return_value=True)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=4))
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = "menu:ai"
    callback.answer = AsyncMock()

    await ai_trainer.menu_ai(callback, state)

    sent = message.answer.await_args
    text = sent.args[0] if sent.args else sent.kwargs["text"]
    assert "ТРЕНЕР НА СВЯЗИ" in text
    assert "Спрашивай что угодно" in text


# ---------- exercise cards for exercises the answer mentions ----------


async def test_answer_gets_a_card_button_per_mentioned_exercise(fresh_db, user_id, monkeypatch):
    bench_id = await fresh_db.create_exercise(user_id, "Жим лёжа", None)
    row_id = await fresh_db.create_exercise(user_id, "Тяга горизонтального блока", None)
    await fresh_db.create_exercise(user_id, "Приседания со штангой", None)
    answer = "Убери жим лёжа на неделю и замени на тягу горизонтального блока."
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", AsyncMock(return_value=answer))

    state = await _make_state(user_id)
    await state.set_state("AITrainerFlow:chatting")
    message = _make_chat_message(user_id, "болит плечо")

    await ai_trainer.ai_question(message, state)

    placeholder = message.answer.return_value
    kb = placeholder.edit_text.await_args.kwargs["reply_markup"]
    assert _callbacks(kb) == [f"ai:excard:{bench_id}", f"ai:excard:{row_id}", "ai:menu"]


async def test_answer_without_mentions_keeps_the_plain_keyboard(fresh_db, user_id, monkeypatch):
    await fresh_db.create_exercise(user_id, "Жим лёжа", None)
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", AsyncMock(return_value="Спи восемь часов."))

    state = await _make_state(user_id)
    await state.set_state("AITrainerFlow:chatting")
    message = _make_chat_message(user_id, "как восстанавливаться?")

    await ai_trainer.ai_question(message, state)

    kb = message.answer.return_value.edit_text.await_args.kwargs["reply_markup"]
    assert _callbacks(kb) == ["ai:menu"]


async def test_exercise_card_button_keeps_the_answer_in_the_chat(fresh_db, user_id):
    """Карточка приходит новым сообщением: ответ тренера, из которого в неё
    перешли, должен остаться на месте."""
    # Название с демо-фото: карточка шлёт ещё и медиа, это тоже не должно
    # трогать сообщение с ответом.
    ex_id = await fresh_db.create_exercise(user_id, "Жим штанги лёжа", None)
    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"ai:excard:{ex_id}")
    callback.message.answer_media_group = AsyncMock(
        return_value=[SimpleNamespace(message_id=11), SimpleNamespace(message_id=12)]
    )

    await ai_trainer.ai_exercise_card(callback, state)

    callback.message.answer_media_group.assert_awaited_once()
    callback.message.edit_text.assert_not_called()
    callback.message.answer.assert_awaited()
    callback.answer.assert_awaited()
    assert await state.get_state() == "ExerciseManage:picking_exercise"


async def test_exercise_card_button_rejects_someone_elses_exercise(fresh_db, user_id):
    other = await fresh_db.get_or_create_user(telegram_id=222, username="other")
    ex_id = await fresh_db.create_exercise(other["telegram_id"], "Жим лёжа", None)
    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"ai:excard:{ex_id}")

    await ai_trainer.ai_exercise_card(callback, state)

    callback.answer.assert_awaited_once_with("Упражнение не найдено", show_alert=True)
