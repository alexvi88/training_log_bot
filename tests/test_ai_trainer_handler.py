"""handlers.ai_trainer: keyboard helper that reacts to an active workout, and the
'К тренировке' button that resumes it without wiping the AI chat history.
"""

import asyncio
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
    message.edit_text = AsyncMock()
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
    # Сообщение о лимите — нижний экран переписки, и без клавиатуры из него
    # оставался только выход через нижнее меню.
    kb = message.reply.await_args.kwargs.get("reply_markup")
    assert kb is not None
    assert "ai:menu" in _callbacks(kb)


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


# ---------- rich answers (Bot API 10.1) ----------


_TABLE_ANSWER = (
    "Смотри цифры:\n\n"
    "| Движение | Факт |\n"
    "|---|---|\n"
    "| **squat** | 140×6 |\n\n"
    "Присед не раскрыт."
)


async def test_a_plain_prose_answer_stays_an_ordinary_message(fresh_db, user_id, monkeypatch):
    """Rich messages are laid out like an article — big headings, wide spacing —
    so an answer with nothing but prose in it must not go that way: there's
    nothing a plain message can't carry, and the article look just inflates it."""
    monkeypatch.setattr(
        ai_trainer.ai_trainer, "ask", AsyncMock(return_value="## Итог\n\nТяга растёт, жми дальше.")
    )

    state = await _make_state(user_id)
    await state.set_state("AITrainerFlow:chatting")
    message = _make_chat_message(user_id, "как дела с тягой?")
    placeholder = message.answer.return_value
    placeholder.bot.edit_message_text = AsyncMock()
    message.answer_rich = AsyncMock()

    await ai_trainer.ai_question(message, state)

    placeholder.bot.edit_message_text.assert_not_awaited()
    message.answer_rich.assert_not_awaited()
    # The heading still arrives as a bold line rather than raw hashes.
    assert "<b>Итог</b>" in placeholder.edit_text.await_args.args[0]


async def test_answer_goes_out_as_a_rich_message_when_the_server_supports_it(
    fresh_db, user_id, monkeypatch
):
    """The model's markdown is handed to Telegram as-is so it parses the table
    itself — a plain message has no table markup at all, and the pipes used to
    reach the user verbatim."""
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", AsyncMock(return_value=_TABLE_ANSWER))

    state = await _make_state(user_id)
    await state.set_state("AITrainerFlow:chatting")
    message = _make_chat_message(user_id, "разбери мои цифры")
    placeholder = message.answer.return_value
    placeholder.bot.edit_message_text = AsyncMock()

    await ai_trainer.ai_question(message, state)

    rich = placeholder.bot.edit_message_text.await_args.kwargs["rich_message"]
    assert rich.markdown == _TABLE_ANSWER
    # The "думаю…" bubble is rewritten in place, not left behind next to the answer.
    placeholder.edit_text.assert_not_awaited()


async def test_headings_are_flattened_to_bold_before_going_out_as_rich(
    fresh_db, user_id, monkeypatch
):
    """A real heading is laid out like an article — big type, wide margins above
    and below — which is what blows the answer apart with whitespace. The rich
    message is sent for its tables, not for that."""
    answer = f"## Твои цифры\n\n{_TABLE_ANSWER}"
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", AsyncMock(return_value=answer))

    state = await _make_state(user_id)
    await state.set_state("AITrainerFlow:chatting")
    message = _make_chat_message(user_id, "разбери мои цифры")
    placeholder = message.answer.return_value
    placeholder.bot.edit_message_text = AsyncMock()

    await ai_trainer.ai_question(message, state)

    sent = placeholder.bot.edit_message_text.await_args.kwargs["rich_message"].markdown
    assert "## Твои цифры" not in sent
    assert "**Твои цифры**" in sent
    # The table itself is untouched — that's the whole reason for going rich.
    assert "| **squat** | 140×6 |" in sent


async def test_answer_falls_back_to_plain_html_without_rich_support(fresh_db, user_id, monkeypatch):
    """Servers and clients below 10.1 must still get the whole answer — with the
    table flattened into readable lines rather than raw pipes."""
    from aiogram.exceptions import TelegramBadRequest

    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", AsyncMock(return_value=_TABLE_ANSWER))

    state = await _make_state(user_id)
    await state.set_state("AITrainerFlow:chatting")
    message = _make_chat_message(user_id, "разбери мои цифры")
    placeholder = message.answer.return_value
    placeholder.bot.edit_message_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="unknown field rich_message")
    )
    message.answer_rich = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="unknown method")
    )

    await ai_trainer.ai_question(message, state)

    sent = placeholder.edit_text.await_args.args[0]
    assert "|" not in sent
    assert "<b>squat</b>" in sent
    assert "Присед не раскрыт." in sent


async def test_a_deleted_placeholder_still_gets_a_rich_answer(fresh_db, user_id, monkeypatch):
    """The draft streamer deletes the placeholder when it starts typing, so
    editing it fails — that's not the server refusing rich, and the answer
    should still go out as one."""
    from aiogram.exceptions import TelegramBadRequest

    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", AsyncMock(return_value=_TABLE_ANSWER))

    state = await _make_state(user_id)
    await state.set_state("AITrainerFlow:chatting")
    message = _make_chat_message(user_id, "разбери мои цифры")
    placeholder = message.answer.return_value
    placeholder.bot.edit_message_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="message to edit not found")
    )
    message.answer_rich = AsyncMock()

    await ai_trainer.ai_question(message, state)

    rich = message.answer_rich.await_args.kwargs["rich_message"]
    assert rich.markdown == _TABLE_ANSWER
    placeholder.edit_text.assert_not_awaited()


async def test_followup_question_does_not_clear_the_pending_program_draft(fresh_db, user_id, monkeypatch):
    """A9: a plain follow-up ("сколько отдыхать между подходами?") right after
    a program proposal used to null ai_program_draft on every turn, even one
    that produced no new draft of its own — killing the still-visible
    "✅ Добавить себе" button under the previous answer (it would then answer
    "предложение уже неактуально")."""
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", AsyncMock(return_value="Отдыхай 2-3 минуты."))

    state = await _make_state(user_id)
    await state.set_state("AITrainerFlow:chatting")
    draft = {
        "name": "Верх/низ",
        "days": [{"name": "День 1", "items": [{"name": "Жим", "target": "3×8", "source": "own"}]}],
    }
    await state.update_data(ai_program_draft=draft)
    message = _make_chat_message(user_id, "сколько отдыхать между подходами?")

    await ai_trainer.ai_question(message, state)

    assert (await state.get_data())["ai_program_draft"] == draft


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
    # "⬅️ Назад" must close the card (see ai_close_exercise_card), not drop
    # into the exercises menu list.
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "ai:closecard" in callback_datas
    assert await state.get_state() == "ExerciseManage:picking_exercise"


async def test_exercise_card_button_rejects_someone_elses_exercise(fresh_db, user_id):
    other = await fresh_db.get_or_create_user(telegram_id=222, username="other")
    ex_id = await fresh_db.create_exercise(other["telegram_id"], "Жим лёжа", None)
    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"ai:excard:{ex_id}")

    await ai_trainer.ai_exercise_card(callback, state)

    callback.answer.assert_awaited_once_with("Упражнение не найдено", show_alert=True)


# ---------- paging through mentions (ai:mpage:) ----------


async def test_mentions_page_rerenders_only_the_keyboard(fresh_db, user_id):
    ids = [await fresh_db.create_exercise(user_id, f"Упражнение {i}", None) for i in range(5)]
    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"ai:mpage:1:{','.join(map(str, ids))}")
    callback.message.edit_reply_markup = AsyncMock()

    await ai_trainer.ai_mentions_page(callback, state)

    callback.message.edit_reply_markup.assert_awaited_once()
    kb = callback.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert _callbacks(kb) == [f"ai:excard:{ids[3]}", f"ai:excard:{ids[4]}", f"ai:mpage:0:{','.join(map(str, ids))}", "ai:menu"]
    callback.answer.assert_awaited_once()


async def test_mentions_page_drops_ids_that_are_not_the_users_own(fresh_db, user_id):
    mine = await fresh_db.create_exercise(user_id, "Жим лёжа", None)
    other = await fresh_db.get_or_create_user(telegram_id=222, username="other")
    theirs = await fresh_db.create_exercise(other["telegram_id"], "Присед", None)
    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"ai:mpage:0:{mine},{theirs}")
    callback.message.edit_reply_markup = AsyncMock()

    await ai_trainer.ai_mentions_page(callback, state)

    kb = callback.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert _callbacks(kb) == [f"ai:excard:{mine}", "ai:menu"]


async def test_mentions_page_keeps_catalog_templates_across_pages(fresh_db, user_id):
    """A page can mix the user's own exercises with not-yet-added catalog
    templates — templates have no user_id of their own and must not be
    filtered out by the ownership check meant for the user's own rows."""
    mine = await fresh_db.create_exercise(user_id, "Жим лёжа", None)
    templates = await fresh_db.list_all_exercise_templates()
    template = next(t for t in templates if t["name"] == "Жим гантелей сидя")
    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"ai:mpage:0:{mine},{template['id']}")
    callback.message.edit_reply_markup = AsyncMock()

    await ai_trainer.ai_mentions_page(callback, state)

    kb = callback.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert _callbacks(kb) == [f"ai:excard:{mine}", f"ai:tpladd:{template['id']}", "ai:menu"]


# ---------- adding a mentioned catalog template the user doesn't have yet ----------


async def test_add_template_forks_it_and_opens_its_card(fresh_db, user_id):
    templates = await fresh_db.list_all_exercise_templates()
    template = next(t for t in templates if t["name"] == "Жим гантелей сидя")
    state = await _make_state(user_id)
    callback = _make_callback(user_id, f"ai:tpladd:{template['id']}")
    callback.message.answer_media_group = AsyncMock(
        return_value=[SimpleNamespace(message_id=11), SimpleNamespace(message_id=12)]
    )

    await ai_trainer.ai_add_template(callback, state)

    ex = await fresh_db.find_exercise_by_display_name(user_id, "Жим гантелей сидя")
    assert ex is not None
    callback.message.answer.assert_awaited()
    callback.answer.assert_awaited()
    assert (await state.get_data()).get("exm_from_ai") is True


# ---------- closing a card opened from the AI-тренер chat ----------


async def test_close_card_just_deletes_it(fresh_db, user_id):
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "ai:closecard")

    await ai_trainer.ai_close_exercise_card(callback, state)

    callback.message.delete.assert_awaited_once()
    callback.answer.assert_awaited_once()


async def test_long_card_gets_the_comment_as_its_own_message(fresh_db, user_id, monkeypatch):
    """A long finish card plus a comment can pass Telegram's 4096 cap. The edit
    is wrapped in suppress(), so the user was told the comment was coming and
    then nothing changed — deliver it separately instead of losing it."""
    import ai_trainer as ai_trainer_module
    import db as dbmod

    workout_id = await dbmod.create_workout(user_id)
    await dbmod.finish_workout(workout_id)

    async def fake_comment(uid, wid):
        return "Хорошая работа, держи темп."

    monkeypatch.setattr(ai_trainer_module, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer_module, "comment_on_workout", fake_comment)

    callback = _make_callback(user_id, f"ai:comment:{workout_id}")
    callback.message.html_text = "к" * 4090
    callback.message.reply_markup = None
    callback.message.edit_text = AsyncMock()
    callback.message.edit_reply_markup = AsyncMock()

    await ai_trainer.ai_comment_workout(callback, await _make_state(user_id))

    callback.message.edit_text.assert_not_awaited()
    callback.message.answer.assert_awaited_once()
    assert "Хорошая работа" in callback.message.answer.await_args.args[0]


async def test_short_card_still_gets_the_comment_appended_in_place(fresh_db, user_id, monkeypatch):
    import ai_trainer as ai_trainer_module
    import db as dbmod

    workout_id = await dbmod.create_workout(user_id)
    await dbmod.finish_workout(workout_id)

    async def fake_comment(uid, wid):
        return "Коротко и по делу."

    monkeypatch.setattr(ai_trainer_module, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer_module, "comment_on_workout", fake_comment)

    callback = _make_callback(user_id, f"ai:comment:{workout_id}")
    callback.message.html_text = "Карточка тренировки"
    callback.message.reply_markup = None
    callback.message.edit_text = AsyncMock()
    callback.message.edit_reply_markup = AsyncMock()

    await ai_trainer.ai_comment_workout(callback, await _make_state(user_id))

    callback.message.edit_text.assert_awaited_once()
    assert "Коротко и по делу" in callback.message.edit_text.await_args.args[0]

# ---------- program the trainer proposed (ai:prog:*) ----------


def _draft(days: int = 2, draft_id: int = 1) -> dict:
    return {
        "id": draft_id,
        "name": "Верх/низ",
        "days": [
            {
                "name": f"День {i}",
                "items": [
                    {"name": "Жим штанги лёжа", "target": "3×5–10", "source": "template"}
                ],
            }
            for i in range(1, days + 1)
        ],
    }


async def test_program_preview_keeps_the_answer_and_offers_saving(fresh_db, user_id):
    state = await _make_state(user_id)
    await state.update_data(ai_program_draft=_draft())
    callback = _make_callback(user_id, "ai:prog:view:1")

    await ai_trainer.ai_program_view(callback, state)

    callback.message.answer.assert_awaited_once()
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    assert _callbacks(kb) == ["ai:prog:save:1", "ai:prog:drop:1"]
    callback.message.delete.assert_not_awaited()


async def test_stale_draft_button_is_refused_by_id(fresh_db, user_id):
    """5.2: a button под старым ответом хранит id того черновика, который был
    актуален в момент показа. Более новое propose_program заменяет слот другим
    id — тап по старой кнопке не должен молча сохранить/показать эту новую,
    более позднюю программу."""
    state = await _make_state(user_id)
    await state.update_data(ai_program_draft=_draft(draft_id=2))
    callback = _make_callback(user_id, "ai:prog:view:1")

    await ai_trainer.ai_program_view(callback, state)

    callback.message.answer.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get("show_alert") is True


async def test_saving_a_program_creates_one_routine_per_day(fresh_db, user_id):
    state = await _make_state(user_id)
    await state.update_data(ai_program_draft=_draft(days=2))
    callback = _make_callback(user_id, "ai:prog:save:1")

    await ai_trainer.ai_program_save(callback, state)

    routines = await fresh_db.list_routines(user_id)
    assert sorted(r["name"] for r in routines) == ["День 1", "День 2"]
    # Дни сгруппированы под именем программы — в списке она одна строка, а не две.
    programs = await fresh_db.list_programs(user_id)
    assert [(p["program_name"], p["day_count"]) for p in programs] == [("Верх/низ", 2)]
    # Черновик израсходован — второй тап по той же кнопке ничего не задублирует.
    assert (await state.get_data()).get("ai_program_draft") is None


async def test_saving_twice_does_not_duplicate_the_program(fresh_db, user_id):
    state = await _make_state(user_id)
    await state.update_data(ai_program_draft=_draft(days=1))
    callback = _make_callback(user_id, "ai:prog:save:1")

    await ai_trainer.ai_program_save(callback, state)
    await ai_trainer.ai_program_save(callback, state)

    assert len(await fresh_db.list_routines(user_id)) == 1


async def test_concurrent_saves_do_not_duplicate_the_program(fresh_db, user_id):
    """A3: the draft used to be read at the top and only cleared ~5 awaits
    later, after real DB work — two taps racing through that window both saw
    the same live draft and both saved. The claim now happens with no
    yielding await in between (see ai_program_save), so the second tap's read
    finds the draft already gone."""
    state = await _make_state(user_id)
    await state.update_data(ai_program_draft=_draft(days=2))

    await asyncio.gather(
        ai_trainer.ai_program_save(_make_callback(user_id, "ai:prog:save:1"), state),
        ai_trainer.ai_program_save(_make_callback(user_id, "ai:prog:save:1"), state),
    )

    assert len(await fresh_db.list_routines(user_id)) == 2


async def test_concurrent_saves_do_not_bypass_the_routine_cap(fresh_db, user_id):
    """Repro from the bug report: 28 existing routines + a 2-day draft, two
    concurrent taps used to produce 32 routines, blowing past
    MAX_ROUTINES_PER_USER (30). With the draft claimed atomically, only one of
    the two taps gets to save."""
    import ai_trainer as ai_trainer_module

    for i in range(ai_trainer_module.MAX_ROUTINES_PER_USER - 2):
        await fresh_db.create_routine(user_id, f"Программа {i}")
    state = await _make_state(user_id)
    await state.update_data(ai_program_draft=_draft(days=2))

    await asyncio.gather(
        ai_trainer.ai_program_save(_make_callback(user_id, "ai:prog:save:1"), state),
        ai_trainer.ai_program_save(_make_callback(user_id, "ai:prog:save:1"), state),
    )

    assert len(await fresh_db.list_routines(user_id)) == ai_trainer_module.MAX_ROUTINES_PER_USER


async def test_partial_save_failure_keeps_the_old_program_intact(fresh_db, user_id, monkeypatch):
    """A6: new days used to be created only after the replaced program's old
    days were already deleted. If a create raised partway through, the user
    was left with a half-built new program and no old one to fall back to.
    New days now go in first — a failure here should still find the old
    program's day untouched."""
    import db as db_module

    gid = await fresh_db.create_muscle_group(user_id, "Ноги")
    squat = await fresh_db.create_exercise(user_id, "Присед", gid)
    program_id = await fresh_db.create_program(user_id, "Верх/низ")
    old_day = await fresh_db.create_routine(user_id, "Старый день", program_id=program_id)
    await fresh_db.add_routine_exercise(old_day, squat, 0, "3×5")

    state = await _make_state(user_id)
    draft = _draft(days=2)
    draft["replaces"] = {"kind": "program", "id": program_id, "name": "Верх/низ", "routine_ids": [old_day]}
    await state.update_data(ai_program_draft=draft)

    real_create = db_module.create_routine_from_program
    calls = {"n": 0}

    async def flaky_create(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(db_module, "create_routine_from_program", flaky_create)

    callback = _make_callback(user_id, "ai:prog:save:1")
    await ai_trainer.ai_program_save(callback, state)

    names = [r["name"] for r in await fresh_db.list_routines(user_id)]
    assert "Старый день" in names
    # Replace-путь: удалять «обрубок» нельзя — программа существовала до
    # предложения. Черновик возвращён, человеку сказано честно, а не молча.
    assert (await state.get_data())["ai_program_draft"] == draft
    assert len(await fresh_db.list_programs(user_id)) == 1
    text = callback.message.answer.await_args.args[0]
    assert "Не получилось" in text and "ещё раз" in text


async def test_saving_a_stale_draft_alerts_and_writes_nothing(fresh_db, user_id):
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "ai:prog:save:1")

    await ai_trainer.ai_program_save(callback, state)

    assert await fresh_db.list_routines(user_id) == []
    assert callback.answer.await_args.kwargs.get("show_alert") is True


async def test_editing_a_program_replaces_its_days_instead_of_duplicating(fresh_db, user_id):
    """Правка сохранённой программы (propose_program.replaces_program) по тапу
    заменяет её дни, а не кладёт рядом вторую копию с тем же именем."""
    db_ = fresh_db
    gid = await db_.create_muscle_group(user_id, "Ноги")
    squat = await db_.create_exercise(user_id, "Присед", gid)
    program_id = await db_.create_program(user_id, "Верх/низ")
    old_day = await db_.create_routine(user_id, "Старый день", program_id=program_id)
    await db_.add_routine_exercise(old_day, squat, 0, "3×5")

    state = await _make_state(user_id)
    draft = _draft(days=2)
    draft["replaces"] = {"kind": "program", "id": program_id, "name": "Верх/низ", "routine_ids": [old_day]}
    await state.update_data(ai_program_draft=draft)

    await ai_trainer.ai_program_save(_make_callback(user_id, "ai:prog:save:1"), state)

    programs = await db_.list_programs(user_id)
    assert [(p["program_name"], p["day_count"]) for p in programs] == [("Верх/низ", 2)]
    names = [r["name"] for r in await db_.list_routines(user_id)]
    assert "Старый день" not in names


async def test_editing_replaces_all_current_days_even_ones_added_since_the_proposal(fresh_db, user_id):
    """A7: правка резолвится заново по id в момент тапа, а не по снимку
    routine_ids, сделанному при предложении — день, добавленный руками между
    предложением и тапом, тоже должен уйти вместе со старой версией, а не
    пережить замену и не всплыть первым в списке."""
    db_ = fresh_db
    gid = await db_.create_muscle_group(user_id, "Ноги")
    squat = await db_.create_exercise(user_id, "Присед", gid)
    program_id = await db_.create_program(user_id, "Верх/низ")
    old_day = await db_.create_routine(user_id, "Старый день", program_id=program_id)
    await db_.add_routine_exercise(old_day, squat, 0, "3×5")

    state = await _make_state(user_id)
    draft = _draft(days=1)
    # routine_ids в черновике — снимок на момент предложения, ДО того как
    # пользователь дописал ещё один день руками.
    draft["replaces"] = {"kind": "program", "id": program_id, "name": "Верх/низ", "routine_ids": [old_day]}
    await state.update_data(ai_program_draft=draft)

    await db_.create_routine(user_id, "Добавил руками", program_id=program_id)

    await ai_trainer.ai_program_save(_make_callback(user_id, "ai:prog:save:1"), state)

    names = [r["name"] for r in await db_.list_program_days_by_id(program_id)]
    assert names == ["День 1"]
    assert "Добавил руками" not in names
    assert "Старый день" not in names


async def test_editing_keeps_the_program_name_unless_the_trainer_renamed_it(fresh_db, user_id):
    """A7: имя программы не должно молча откатываться, если пользователь
    переименовал её между предложением и тапом руками — но если тренер сам
    прислал другое имя в propose_program, это переименование должно примениться."""
    db_ = fresh_db
    program_id = await db_.create_program(user_id, "Верх/низ")
    old_day = await db_.create_routine(user_id, "Старый день", program_id=program_id)

    state = await _make_state(user_id)
    draft = _draft(days=1)
    draft["name"] = "Верх/низ"  # то же имя, что и у replaces — рассматривается как правка состава
    draft["replaces"] = {"kind": "program", "id": program_id, "name": "Верх/низ", "routine_ids": [old_day]}
    await state.update_data(ai_program_draft=draft)
    await db_.rename_program_by_id(program_id, "Переименовал руками")

    await ai_trainer.ai_program_save(_make_callback(user_id, "ai:prog:save:1"), state)

    programs = await db_.list_programs(user_id)
    assert [p["program_name"] for p in programs] == ["Переименовал руками"]


async def test_editing_preview_says_it_will_replace_and_labels_the_button(fresh_db, user_id):
    state = await _make_state(user_id)
    draft = _draft(days=1)
    draft["replaces"] = {"kind": "program", "id": 999, "name": "Верх/низ", "routine_ids": [123]}
    await state.update_data(ai_program_draft=draft)
    callback = _make_callback(user_id, "ai:prog:view:1")

    await ai_trainer.ai_program_view(callback, state)

    text = callback.message.answer.await_args.args[0]
    assert "Верх/низ" in text and "замен" in text.lower()
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].text == "✅ Обновить программу"


async def test_a_program_deleted_before_the_tap_is_just_added(fresh_db, user_id):
    """Черновик переживает и удаление оригинала руками: заменять нечего —
    значит просто добавляем, а не падаем."""
    state = await _make_state(user_id)
    draft = _draft(days=1)
    draft["replaces"] = {"kind": "program", "id": 999_999, "name": "Верх/низ", "routine_ids": [999_999]}
    await state.update_data(ai_program_draft=draft)

    await ai_trainer.ai_program_save(_make_callback(user_id, "ai:prog:save:1"), state)

    assert len(await fresh_db.list_routines(user_id)) == 1


async def test_program_draft_survives_a_trip_to_the_menu(fresh_db, user_id):
    """Кнопка программы висит под ответом тренера и остаётся нажимаемой, так что
    черновик обязан пережить выход в меню: раньше поход в меню (в том числе
    после просмотра превью) чистил FSM целиком, и кнопка отвечала алертом
    «предложение уже неактуально»."""
    from handlers.workout import _clear_state_keep_workout

    state = await _make_state(user_id)
    await state.update_data(ai_program_draft=_draft())

    await _clear_state_keep_workout(state)

    callback = _make_callback(user_id, "ai:prog:view:1")
    await ai_trainer.ai_program_view(callback, state)

    callback.message.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get("show_alert") is not True


async def test_saving_over_the_routine_cap_is_refused(fresh_db, user_id):
    import ai_trainer as ai_trainer_module

    for i in range(ai_trainer_module.MAX_ROUTINES_PER_USER):
        await fresh_db.create_routine(user_id, f"Программа {i}")
    state = await _make_state(user_id)
    await state.update_data(ai_program_draft=_draft(days=1))
    callback = _make_callback(user_id, "ai:prog:save:1")

    await ai_trainer.ai_program_save(callback, state)

    assert len(await fresh_db.list_routines(user_id)) == ai_trainer_module.MAX_ROUTINES_PER_USER
    assert callback.answer.await_args.kwargs.get("show_alert") is True


async def test_saving_a_program_with_a_taken_name_offers_a_choice(fresh_db, user_id):
    """A2: раньше дни молча дописывались в существующую программу с тем же
    именем — три сохранения подряд давали 18 дней в одной программе. Теперь
    сохранение (без replaces) на занятое имя не пишет ничего, а предлагает
    пользователю решить."""
    await fresh_db.create_program(user_id, "Верх/низ")
    state = await _make_state(user_id)
    await state.update_data(ai_program_draft=_draft(days=2))
    callback = _make_callback(user_id, "ai:prog:save:1")

    await ai_trainer.ai_program_save(callback, state)

    # Ничего не сохранено, пока пользователь не выбрал.
    assert await fresh_db.list_routines(user_id) == []
    kb = callback.message.edit_text.await_args.kwargs["reply_markup"]
    callbacks = _callbacks(kb)
    assert "ai:prog:replace:1" in callbacks
    assert "ai:prog:copy:1" in callbacks
    # Черновик жив — решение ещё предстоит принять.
    assert (await state.get_data())["ai_program_draft"]["id"] == 1


async def test_conflict_replace_choice_replaces_the_existing_program(fresh_db, user_id):
    existing_id = await fresh_db.create_program(user_id, "Верх/низ")
    await fresh_db.create_routine(user_id, "Старый день", program_id=existing_id)
    state = await _make_state(user_id)
    await state.update_data(ai_program_draft=_draft(days=2))

    await ai_trainer.ai_program_save(_make_callback(user_id, "ai:prog:save:1"), state)
    await ai_trainer.ai_program_replace_conflict(_make_callback(user_id, "ai:prog:replace:1"), state)

    programs = await fresh_db.list_programs(user_id)
    assert [(p["program_name"], p["day_count"]) for p in programs] == [("Верх/низ", 2)]
    names = [r["name"] for r in await fresh_db.list_routines(user_id)]
    assert "Старый день" not in names


async def test_conflict_copy_choice_saves_under_a_free_name(fresh_db, user_id):
    await fresh_db.create_program(user_id, "Верх/низ")
    state = await _make_state(user_id)
    await state.update_data(ai_program_draft=_draft(days=2))

    await ai_trainer.ai_program_save(_make_callback(user_id, "ai:prog:save:1"), state)
    await ai_trainer.ai_program_copy_conflict(_make_callback(user_id, "ai:prog:copy:1"), state)

    programs = await fresh_db.list_programs(user_id)
    names = sorted(p["program_name"] for p in programs)
    assert names == ["Верх/низ", "Верх/низ (2)"]


async def test_dropping_a_program_removes_the_preview_and_the_draft(fresh_db, user_id):
    state = await _make_state(user_id)
    await state.update_data(ai_program_draft=_draft())
    callback = _make_callback(user_id, "ai:prog:drop:1")

    await ai_trainer.ai_program_drop(callback, state)

    callback.message.delete.assert_awaited_once()
    assert (await state.get_data()).get("ai_program_draft") is None
    assert await fresh_db.list_routines(user_id) == []


# ---------- "Составить с AI-тренером" entry point (ai:buildprog) ----------


def _make_buildprog_callback(user_id: int):
    """screen — the '🗂 Программы' message the button lives on; its .answer()
    yields the intro screen (safe_edit's fallback path, forced by pinning
    chat_bottom below), whose own .answer() yields the "думаю…" placeholder
    that _handle_question edits with the final answer.
    """
    from aiogram.types import CallbackQuery

    thinking_placeholder = MagicMock()
    thinking_placeholder.edit_text = AsyncMock()

    intro_screen = MagicMock()
    intro_screen.chat = SimpleNamespace(id=user_id)
    intro_screen.message_id = 9
    intro_screen.answer = AsyncMock(return_value=thinking_placeholder)

    screen = MagicMock()
    screen.chat = SimpleNamespace(id=user_id)
    screen.message_id = 3
    screen.text = "🗂 Программы"
    screen.delete = AsyncMock()
    screen.answer = AsyncMock(return_value=intro_screen)

    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = screen
    callback.data = "ai:buildprog"
    callback.answer = AsyncMock()
    return callback


async def test_ai_build_program_hints_when_not_configured(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer.ai_trainer, "is_configured", lambda: False)
    state = await _make_state(user_id)
    callback = _make_buildprog_callback(user_id)

    await ai_trainer.ai_build_program(callback, state)

    assert callback.answer.await_args.kwargs.get("show_alert") is True
    assert await state.get_state() is None


async def test_ai_build_program_seeds_the_conversation_and_enters_chatting(
    fresh_db, user_id, monkeypatch
):
    """The button skips typing anything: it drops straight into the trainer's
    existing propose_program flow with a canned "build me a program" question,
    which is what actually drives the guiding questions and the eventual draft."""
    import ui

    monkeypatch.setattr(ai_trainer.ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ui.chat_bottom, "is_at_bottom", lambda *a, **k: False)

    captured = {}

    async def fake_ask(uid, question, history, **kwargs):
        captured["user_id"] = uid
        captured["question"] = question
        return "Для программы расскажи: сколько дней в неделю?"

    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", fake_ask)

    state = await _make_state(user_id)
    callback = _make_buildprog_callback(user_id)

    await ai_trainer.ai_build_program(callback, state)

    assert captured["user_id"] == user_id
    assert captured["question"] == ai_trainer.BUILD_PROGRAM_SEED
    assert await state.get_state() == "AITrainerFlow:chatting"
    assert user_id not in ai_trainer._busy
    assert await fresh_db.get_ai_question_count_today(user_id) == 1

    # The intro screen replaces the 🗂 Программы menu, so it needs its own way
    # out — an answer may never arrive (daily limit reached, provider down).
    assert callback.message.answer.await_args.kwargs["reply_markup"] is not None


async def test_ai_build_program_releases_busy_on_a_stale_callback(fresh_db, user_id, monkeypatch):
    """A5: callback.answer() used to sit outside the try/finally that releases
    `_busy` — a stale-button tap ("query is too old", a real TelegramBadRequest)
    made that answer raise, and the finally releasing `_busy` never ran. That
    locked the user out of the AI trainer (every message/photo/voice answered
    "ещё думаю над прошлым вопросом") for the rest of the process's life."""
    from aiogram.exceptions import TelegramBadRequest

    monkeypatch.setattr(ai_trainer.ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", AsyncMock(return_value="ок"))

    state = await _make_state(user_id)
    callback = _make_buildprog_callback(user_id)
    callback.answer = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="query is too old")
    )

    await ai_trainer.ai_build_program(callback, state)

    assert user_id not in ai_trainer._busy


async def test_announcement_build_program_keeps_the_announcement_in_the_chat(
    fresh_db, user_id, monkeypatch
):
    """Кнопка под релизной рассылкой не съедает саму рассылку.

    Живой случай: тап по «🤖 Собрать программу» под анонсом — и анонс исчезал
    из чата. Экранная кнопка так и должна работать (сценарий встаёт на место
    меню), но рассылка не экран: под ней вторая кнопка, про разбор видео, и
    вернуться к ней после первого тапа было уже некуда.
    """
    monkeypatch.setattr(ai_trainer.ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", AsyncMock(return_value="ок"))

    state = await _make_state(user_id)
    callback = _make_buildprog_callback(user_id)
    callback.data = "ann:buildprog"
    callback.message.edit_text = AsyncMock()

    await ai_trainer.announcement_build_program(callback, state)

    callback.message.delete.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()
    # А сценарий при этом стартовал — интро приехало отдельным сообщением.
    callback.message.answer.assert_awaited()
    assert await state.get_state() == "AITrainerFlow:chatting"


# ---------- готовые вопросы на стартовом экране (ai:preset:*) ----------


def _make_menu_ai_callback(user_id: int):
    from aiogram.types import CallbackQuery

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
    return callback


async def test_fresh_intro_offers_preset_question_buttons(fresh_db, user_id, monkeypatch):
    """Примеры вопросов из интро стали кнопками: тап задаёт вопрос сразу,
    вместо перепечатывания примера руками."""
    monkeypatch.setattr(ai_trainer.ai_trainer, "is_configured", lambda: True)
    state = await _make_state(user_id)
    callback = _make_menu_ai_callback(user_id)

    await ai_trainer.menu_ai(callback, state)

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    callbacks = _callbacks(kb)
    for key in ai_trainer.PRESET_QUESTIONS:
        assert f"ai:preset:{key}" in callbacks
    assert "ai:buildprog" in callbacks
    # Пресеты стоят выше навигации: на интро они — основной призыв к действию.
    assert callbacks.index("ai:preset:progress") < callbacks.index("ai:menu")


async def test_intro_shows_what_the_trainer_remembers_and_how_to_fix_it(
    fresh_db, user_id, monkeypatch
):
    """Профиль пишется без спроса и без кнопок — значит, человек должен хотя бы
    видеть, что там записано, и знать, что это правится словами.

    Проверяем на ВОЗВРАТЕ в разговор, а не на свежем интро: ai_history переживает
    и выход в меню, и перезапуск бота, поэтому свежее интро человек видит один
    раз в жизни — привяжи напоминание к нему, и его не увидит вообще никто."""
    monkeypatch.setattr(ai_trainer.ai_trainer, "is_configured", lambda: True)
    await fresh_db.update_user(user_id, experience="новичок", goal="масса")
    state = await _make_state(user_id)
    await state.update_data(ai_history=[{"role": "user", "content": "как жим?"}])
    callback = _make_menu_ai_callback(user_id)

    await ai_trainer.menu_ai(callback, state)

    call = callback.message.answer.await_args
    text = call.args[0] if call.args else call.kwargs["text"]
    assert "Что я про тебя помню" in text
    assert "опыт — новичок" in text
    assert "цель — масса" in text
    assert "поправлю" in text


async def test_memory_reminder_stays_quiet_for_a_week(fresh_db, user_id, monkeypatch):
    """На каждом заходе один и тот же список читался бы как шум."""
    monkeypatch.setattr(ai_trainer.ai_trainer, "is_configured", lambda: True)
    await fresh_db.update_user(user_id, goal="масса")

    first = await ai_trainer._memory_reminder(user_id)
    second = await ai_trainer._memory_reminder(user_id)

    assert "Что я про тебя помню" in first
    assert second == ""


async def test_memory_reminder_pause_survives_a_state_reset(fresh_db, user_id):
    """Отметка о показе живёт в базе, а не в FSM.

    /start чистит состояние целиком, кроме трёх AI-ключей — с отметкой в FSM
    напоминание вылезало бы на каждый тап «🏠 Меню» и обратно в тренера, а
    именно этим оно и превращается в шум."""
    await fresh_db.update_user(user_id, goal="масса")
    state = await _make_state(user_id)

    assert await ai_trainer._memory_reminder(user_id) != ""
    await state.clear()

    assert await ai_trainer._memory_reminder(user_id) == ""


async def test_memory_reminder_is_silent_when_nothing_is_known(fresh_db, user_id):
    """Хвастаться нечем — и «я про тебя ничего не помню» тут не нужно."""
    assert await ai_trainer._memory_reminder(user_id) == ""


async def test_resumed_conversation_has_no_preset_buttons(fresh_db, user_id, monkeypatch):
    """Посреди разговора стартовые вопросы читались бы как потеря контекста."""
    monkeypatch.setattr(ai_trainer.ai_trainer, "is_configured", lambda: True)
    state = await _make_state(user_id)
    await state.update_data(ai_history=[{"role": "user", "content": "как жим?"}])
    callback = _make_menu_ai_callback(user_id)

    await ai_trainer.menu_ai(callback, state)

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    assert not any(cb.startswith("ai:preset:") for cb in _callbacks(kb))


async def test_preset_tap_seeds_the_conversation_with_the_full_question(
    fresh_db, user_id, monkeypatch
):
    """Кнопка отправляет тренеру полный текст вопроса и проводит его через тот же
    путь, что и напечатанный руками (лимит, история, чат-состояние)."""
    import ui

    monkeypatch.setattr(ai_trainer.ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ui.chat_bottom, "is_at_bottom", lambda *a, **k: False)

    captured = {}

    async def fake_ask(uid, question, history, **kwargs):
        captured["question"] = question
        return "Жим растёт, присед стоит."

    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", fake_ask)

    state = await _make_state(user_id)
    callback = _make_buildprog_callback(user_id)
    callback.data = "ai:preset:progress"

    await ai_trainer.ai_preset_question(callback, state)

    _, expected_question = ai_trainer.PRESET_QUESTIONS["progress"]
    assert captured["question"] == expected_question
    assert await state.get_state() == "AITrainerFlow:chatting"
    assert user_id not in ai_trainer._busy
    assert await fresh_db.get_ai_question_count_today(user_id) == 1
    # Экран-интро цитирует вопрос: сам вопрос в чат от лица пользователя не
    # попадает, и без цитаты ответ висел бы без контекста.
    sent = callback.message.answer.await_args
    intro_text = sent.args[0] if sent.args else sent.kwargs["text"]
    assert expected_question in intro_text


async def test_stale_preset_button_alerts_instead_of_crashing(fresh_db, user_id, monkeypatch):
    """Кнопка из интро прошлой версии, где такой вопрос ещё существовал."""
    monkeypatch.setattr(ai_trainer.ai_trainer, "is_configured", lambda: True)
    state = await _make_state(user_id)
    callback = _make_buildprog_callback(user_id)
    callback.data = "ai:preset:gone"

    await ai_trainer.ai_preset_question(callback, state)

    assert callback.answer.await_args.kwargs.get("show_alert") is True
    assert await state.get_state() is None


async def test_ai_build_program_asks_the_trainer_to_lead_with_questions(fresh_db, user_id):
    """The button promises "сейчас задам пару вопросов", and the system prompt
    lets the trainer skip straight to a draft on sensible defaults — so the
    seed has to ask for the questions explicitly, or the intro would lie."""
    assert "вопрос" in ai_trainer.BUILD_PROGRAM_SEED.lower()
    assert "вопрос" in ai_trainer.BUILD_PROGRAM_INTRO.lower()


# ---------- неповторяющийся id черновика ----------


def _fake_ask_with_program():
    """ai_trainer.ask, который на каждый вопрос отдаёт свежий черновик программы."""

    async def fake_ask(uid, question, history, **kwargs):
        await kwargs["on_program"](
            {
                "name": "Верх/низ",
                "days": [
                    {"name": "День 1", "items": [{"name": "Жим", "target": "3×8", "source": "own"}]}
                ],
            }
        )
        return "Собрал программу."

    return fake_ask


async def test_draft_ids_survive_a_full_state_clear_without_colliding(fresh_db, user_id, monkeypatch):
    """Счётчик ai_draft_seq жил в FSM и гиб при state.clear() (конец тренировки)
    и походах в меню: следующая программа снова получала id=1, и вечная кнопка
    «ai:prog:save:1» под превью программы А молча сохраняла программу Б."""
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", _fake_ask_with_program())

    state = await _make_state(user_id)
    await state.set_state("AITrainerFlow:chatting")
    await ai_trainer.ai_question(_make_chat_message(user_id, "собери программу"), state)
    first_id = (await state.get_data())["ai_program_draft"]["id"]

    await state.clear()  # полный сброс — как после завершения тренировки
    await state.set_state("AITrainerFlow:chatting")
    await ai_trainer.ai_question(_make_chat_message(user_id, "собери другую"), state)
    second_id = (await state.get_data())["ai_program_draft"]["id"]

    assert first_id != second_id


async def test_a_hex_draft_id_button_saves_its_draft(fresh_db, user_id):
    """Id черновика — непрозрачный токен: парсинг сегмента callback_data в int
    превращал бы любой нечисловой id в «несуществующий» и убивал кнопку."""
    state = await _make_state(user_id)
    await state.update_data(ai_program_draft=_draft(days=1, draft_id="a1b2c3d4"))

    await ai_trainer.ai_program_save(_make_callback(user_id, "ai:prog:save:a1b2c3d4"), state)

    assert len(await fresh_db.list_routines(user_id)) == 1


# ---------- упавшее сохранение не теряет ни черновик, ни список программ ----------


async def test_failed_save_restores_the_draft_and_removes_the_stub_program(fresh_db, user_id, monkeypatch):
    """Сохранение стирает черновик из FSM до записи (защита от двойного тапа) —
    необработанное исключение раньше уносило черновик насовсем, а в списке
    программ оставался обрубок: create_program и дни пишутся не транзакцией."""
    import db as db_module

    real_create = db_module.create_routine_from_program

    async def broken_create(*args, **kwargs):
        raise RuntimeError("db went away")

    monkeypatch.setattr(db_module, "create_routine_from_program", broken_create)
    state = await _make_state(user_id)
    draft = _draft(days=2)
    await state.update_data(ai_program_draft=draft)
    callback = _make_callback(user_id, "ai:prog:save:1")

    await ai_trainer.ai_program_save(callback, state)

    # Обрубок (create_program успел пройти) удалён — «🗂 Программы» пуст.
    assert await fresh_db.list_programs(user_id) == []
    # Черновик вернулся в FSM, человеку сказано честно и предложено повторить.
    assert (await state.get_data())["ai_program_draft"] == draft
    text = callback.message.answer.await_args.args[0]
    assert "Не получилось" in text and "ещё раз" in text

    # Кнопка действительно живая: повторный тап после починки сохраняет всё —
    # без удаления обрубка он упёрся бы в конфликт имён вместо сохранения.
    monkeypatch.setattr(db_module, "create_routine_from_program", real_create)
    await ai_trainer.ai_program_save(_make_callback(user_id, "ai:prog:save:1"), state)
    programs = await fresh_db.list_programs(user_id)
    assert [(p["program_name"], p["day_count"]) for p in programs] == [("Верх/низ", 2)]


# ---------- обрыв стрима не остаётся тишиной ----------


async def test_stream_break_after_placeholder_deletion_is_reported_in_a_new_message(
    fresh_db, user_id, monkeypatch
):
    """Черновик-стример удаляет placeholder, когда начинает «печатать»
    (on_draft_start); если после этого стрим обрывается, правка placeholder
    падала под suppress — и человек оставался в полной тишине."""
    from aiogram.exceptions import TelegramBadRequest

    monkeypatch.setattr(
        ai_trainer.ai_trainer, "ask", AsyncMock(side_effect=RuntimeError("stream died"))
    )

    state = await _make_state(user_id)
    await state.set_state("AITrainerFlow:chatting")
    message = _make_chat_message(user_id, "как жим?")
    placeholder = message.answer.return_value
    placeholder.edit_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="message to edit not found")
    )

    await ai_trainer.ai_question(message, state)

    # Первый message.answer — placeholder «думаю…», второй — сама ошибка.
    assert message.answer.await_count == 2
    err = message.answer.await_args
    assert "Не получилось" in err.args[0]
    assert err.kwargs.get("reply_markup") is not None


# ---------- ответ не теряется после списания квоты ----------


async def test_html_chunk_grown_past_the_limit_by_conversion_is_cut(monkeypatch):
    """Чанки режутся по сырому markdown, а HTML-конверсия удлиняет текст
    (развёрнутая таблица — в полтора-два раза): чанк, «влезавший» до конверсии,
    превышал 4096, и Telegram отвергал сообщение целиком."""
    import formatting
    import ui

    monkeypatch.setattr(ai_trainer.formatting, "ai_markdown_to_html", lambda s: s * 2)
    placeholder = MagicMock()
    placeholder.edit_text = AsyncMock()
    message = MagicMock()
    message.chat = SimpleNamespace(id=1)
    message.answer = AsyncMock()

    await ai_trainer._send_html_answer(message, placeholder, ["к" * 4000], "", None)

    sent = placeholder.edit_text.await_args.args[0]
    assert formatting.telegram_length(sent) <= ui.TEXT_LIMIT


async def test_one_failed_chunk_does_not_eat_the_rest(monkeypatch):
    """Второй и дальние чанки уходили без перехвата: падение одного обрывало
    отправку всех последующих — вместе с клавиатурой на последнем."""
    from aiogram.exceptions import TelegramBadRequest

    placeholder = MagicMock()
    placeholder.edit_text = AsyncMock()
    message = MagicMock()
    message.chat = SimpleNamespace(id=1)
    message.answer = AsyncMock(
        side_effect=[TelegramBadRequest(method=MagicMock(), message="too long"), None]
    )

    await ai_trainer._send_html_answer(message, placeholder, ["один", "два", "три"], "", None)

    # Чанк «три» отправлен, несмотря на упавший «два».
    assert message.answer.await_count == 2


async def test_mention_paging_arrows_fit_telegrams_callback_data_limit():
    """Ссылки на упомянутое едут прямо в callback_data стрелок, а Telegram
    ограничивает её 64 байтами: с 8-значными id упражнений полный список не
    влезал, и Telegram отвергал всё сообщение с ответом."""
    import keyboards

    exercises = [
        {"id": 10_000_000 + i, "is_template": False, "display_name": f"Упражнение {i}"}
        for i in range(8)
    ]
    kb = keyboards.ai_trainer_keyboard(exercises=exercises)

    arrows = [
        b for row in kb.inline_keyboard for b in row if b.callback_data.startswith("ai:mpage:")
    ]
    assert arrows  # листание не пропало совсем — потерян только хвост ссылок
    for b in arrows:
        assert len(b.callback_data.encode()) <= 64


# ---------- UX-мелочи релиза ----------


async def test_intro_advertises_the_program_builder(fresh_db, user_id):
    """Сборка программы должна рекламироваться прямо на старте диалога — раньше
    примером в тексте интро, теперь кнопкой готового вопроса под ним."""
    labels = [label for label, _cb in await ai_trainer.intro_presets(user_id)]
    assert any("Составь мне программу" in label for label in labels)


async def test_program_gone_alert_does_not_ask_to_rebuild_a_saved_program():
    """Алерт показывается и после УСПЕШНОГО сохранения (черновик израсходован) —
    совет «собрать заново» вёл к дубликатам."""
    assert "🗂 Программы" in ai_trainer._PROGRAM_GONE


async def test_saved_announcement_offers_opening_the_program(fresh_db, user_id):
    """Текст говорил «ищи в «🗂 Программы»», хотя бот и так знает, что только
    что сохранил — первая кнопка открывает программу напрямую."""
    state = await _make_state(user_id)
    await state.update_data(ai_program_draft=_draft(days=2))
    callback = _make_callback(user_id, "ai:prog:save:1")

    await ai_trainer.ai_program_save(callback, state)

    (program,) = await fresh_db.list_programs(user_id)
    kb = callback.message.edit_text.await_args.kwargs["reply_markup"]
    callbacks = _callbacks(kb)
    assert callbacks[0] == f"rt:prg:{program['id']}"
    assert "rt:manage" in callbacks


async def test_saving_a_single_day_does_not_promise_any_of_the_days(fresh_db, user_id):
    """Для программы из одного дня «тренировка по любому из дней» — нелепость."""
    state = await _make_state(user_id)
    await state.update_data(ai_program_draft=_draft(days=1))
    callback = _make_callback(user_id, "ai:prog:save:1")

    await ai_trainer.ai_program_save(callback, state)

    text = callback.message.edit_text.await_args.args[0]
    assert "любому из дней" not in text


async def test_ai_build_program_refuses_before_burning_the_screen(fresh_db, user_id, monkeypatch):
    """Лимит вопросов кончился — кнопка не должна менять экран «🗂 Программы»
    на интро «сейчас задам пару вопросов», под которым тут же приедет отказ.

    _handle_question проверяет лимит и сам, но к тому моменту человек уже
    потерял экран, с которого пришёл, и получил на его месте обещание, которое
    сразу же не сбылось.
    """
    import config

    monkeypatch.setattr(ai_trainer.ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(
        ai_trainer.ai_trainer, "ask", AsyncMock(side_effect=AssertionError("модель не должна дёргаться"))
    )
    for _ in range(config.AI_QUESTION_DAILY_LIMIT):
        await fresh_db.increment_ai_question_count(user_id)

    state = await _make_state(user_id)
    callback = _make_buildprog_callback(user_id)

    await ai_trainer.ai_build_program(callback, state)

    callback.message.answer.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get("show_alert") is True
    assert "лимит" in callback.answer.await_args.args[0].lower()
    # И экран не должен утащить человека в чат с тренером, куда он не попал.
    assert await state.get_state() is None
    assert user_id not in ai_trainer._busy


# ---------- кнопка отката под ответом тренера ----------


def _make_undo_callback(user_id: int, data: str, buttons: list[str]):
    from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

    message = MagicMock()
    message.reply_markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=cb, callback_data=cb)] for cb in buttons]
    )
    message.edit_reply_markup = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def test_undo_button_reverts_and_disappears(fresh_db, user_id):
    log_id = await fresh_db.add_bodyweight_log(user_id, 78.4)
    state = await _make_state(user_id)
    buttons = await ai_trainer._register_undos(
        state, [{"label": "↩️ Отменить: 78.4 kg", "undo": {"kind": "bodyweight", "id": log_id}}]
    )
    data = buttons[0]["callback"]
    await state.update_data(ai_actions=buttons)

    callback = _make_undo_callback(user_id, data, [data, "ai:menu"])
    await ai_trainer.ai_undo(callback, state)

    assert await fresh_db.list_bodyweight_logs(user_id) == []
    # Отработавшая кнопка убирается, соседние остаются — по ответу ещё ходят.
    kept = callback.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    left = [b.callback_data for row in kept.inline_keyboard for b in row]
    assert left == ["ai:menu"]
    assert (await state.get_data())["ai_actions"] == []


async def test_a_second_tap_on_the_same_undo_does_nothing(fresh_db, user_id):
    """Ключ забирается из хранилища до самого отката: иначе второй тап снёс бы
    запись, которую человек успел сделать после первого."""
    log_id = await fresh_db.add_bodyweight_log(user_id, 78.4)
    state = await _make_state(user_id)
    buttons = await ai_trainer._register_undos(
        state, [{"label": "↩️", "undo": {"kind": "bodyweight", "id": log_id}}]
    )
    data = buttons[0]["callback"]

    await ai_trainer.ai_undo(_make_undo_callback(user_id, data, [data]), state)
    await fresh_db.add_bodyweight_log(user_id, 81.0)

    second = _make_undo_callback(user_id, data, [data])
    await ai_trainer.ai_undo(second, state)

    assert second.answer.await_args.kwargs.get("show_alert") is True
    assert [r["weight"] for r in await fresh_db.list_bodyweight_logs(user_id)] == [81.0]


async def test_undo_of_someone_elses_row_is_refused(fresh_db, user_id):
    """id приезжает из FSM, но строка в базе к моменту тапа могла смениться —
    владельца проверяем заново на каждом шаге."""
    other_id = user_id + 1
    await fresh_db.get_or_create_user(other_id, "чужой")
    foreign_log = await fresh_db.add_bodyweight_log(other_id, 90.0)

    state = await _make_state(user_id)
    buttons = await ai_trainer._register_undos(
        state, [{"label": "↩️", "undo": {"kind": "bodyweight", "id": foreign_log}}]
    )
    callback = _make_undo_callback(user_id, buttons[0]["callback"], [buttons[0]["callback"]])

    await ai_trainer.ai_undo(callback, state)

    assert callback.answer.await_args.kwargs.get("show_alert") is True
    assert len(await fresh_db.list_bodyweight_logs(other_id)) == 1


# ---------- «▶️ Начать по ней»: план в работу, программа не заводится ----------


def _make_train_callback(user_id: int, draft_id: str):
    from aiogram.types import CallbackQuery

    live = MagicMock()
    live.chat = SimpleNamespace(id=user_id)
    live.message_id = 77

    preview = MagicMock()
    preview.chat = SimpleNamespace(id=user_id)
    preview.message_id = 42
    preview.delete = AsyncMock()
    preview.answer = AsyncMock(return_value=live)

    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = preview
    callback.data = f"ai:prog:train:{draft_id}"
    callback.answer = AsyncMock()
    return callback


async def _draft_in_state(fresh_db, user_id, state, items):
    group_id = await fresh_db.create_muscle_group(user_id, "Ноги")
    ids = [await fresh_db.create_exercise(user_id, name, group_id) for name, _ in items]
    draft = {
        "id": "d1", "name": "Сегодня", "replaces": None, "notes": [],
        "days": [{"name": "Ноги", "items": [
            {"name": name, "target": target, "source": "own"} for name, target in items
        ]}],
    }
    await state.update_data(ai_program_draft=draft)
    return ids


async def test_training_by_a_draft_creates_no_program(fresh_db, user_id, monkeypatch):
    """Ради этого всё и делалось: разовая тренировка не должна оседать в
    «🗂 Программы» рядом с настоящими программами."""
    loaded = {}

    async def fake_load(callback, state, index=0):
        loaded["called"] = True
        return True

    monkeypatch.setattr("handlers.workout._load_next_planned_block", fake_load)

    state = await _make_state(user_id)
    ex_ids = await _draft_in_state(fresh_db, user_id, state, [("Присед", "4×8"), ("Выпады", "3×12")])

    await ai_trainer.ai_program_train(_make_train_callback(user_id, "d1"), state)

    assert await fresh_db.list_programs(user_id) == []
    assert await fresh_db.list_routines(user_id) == []
    # А тренировка — есть, и план в ней.
    assert await fresh_db.get_active_workout(user_id) is not None
    planned = (await state.get_data())["planned_blocks"]
    assert [b["exercise_ids"][0] for b in planned] == ex_ids
    assert planned[0]["targets"][ex_ids[0]] == "4×8"
    assert loaded["called"]


async def test_training_by_a_draft_uses_the_workout_already_open(fresh_db, user_id, monkeypatch):
    """На превью приходят с экрана выбора, который активную тренировку уже
    создал — заводить вторую значило бы бросить первую."""
    monkeypatch.setattr("handlers.workout._load_next_planned_block", AsyncMock(return_value=True))

    workout_id, _ = await fresh_db.get_or_create_active_workout(user_id)
    state = await _make_state(user_id)
    await _draft_in_state(fresh_db, user_id, state, [("Присед", "4×8")])

    await ai_trainer.ai_program_train(_make_train_callback(user_id, "d1"), state)

    assert (await state.get_data())["workout_id"] == workout_id


async def test_the_draft_is_spent_once_you_train_by_it(fresh_db, user_id, monkeypatch):
    """Кнопка «Добавить себе» под тем же превью после старта должна отвечать,
    что предложение неактуально: по нему уже занимаются."""
    monkeypatch.setattr("handlers.workout._load_next_planned_block", AsyncMock(return_value=True))

    state = await _make_state(user_id)
    await _draft_in_state(fresh_db, user_id, state, [("Присед", "4×8")])

    await ai_trainer.ai_program_train(_make_train_callback(user_id, "d1"), state)

    assert (await state.get_data())["ai_program_draft"] is None


async def test_a_draft_already_done_this_session_says_so(fresh_db, user_id, monkeypatch):
    """Упражнения, уже отработанные в этой тренировке, из плана вычитаются —
    а если не осталось ничего, надо сказать, а не открыть пустой план."""
    monkeypatch.setattr("handlers.workout._load_next_planned_block", AsyncMock(return_value=True))

    state = await _make_state(user_id)
    ex_ids = await _draft_in_state(fresh_db, user_id, state, [("Присед", "4×8")])
    workout_id, _ = await fresh_db.get_or_create_active_workout(user_id)
    block_id = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_block_exercise(block_id, ex_ids[0], 0)
    await fresh_db.append_set(block_id, ex_ids[0], 0, 100, 5)

    callback = _make_train_callback(user_id, "d1")
    await ai_trainer.ai_program_train(callback, state)

    assert callback.answer.await_args.kwargs.get("show_alert") is True
    # Черновик при этом цел: по нему ещё можно пойти в следующий раз.
    assert (await state.get_data())["ai_program_draft"] is not None


async def test_the_workout_picker_offers_todays_workout_to_everyone(fresh_db, user_id, monkeypatch):
    """Кнопка сбора ПРОГРАММЫ показывается только тем, у кого программ нет, —
    а «что сегодня качать» одинаково нужно и тем, и другим."""
    from handlers import workout as workout_handlers

    monkeypatch.setattr(workout_handlers.ai_trainer, "is_configured", lambda: True)
    await fresh_db.create_routine(user_id, "Верх/низ")

    from tests.test_workout_picker import _picker_extra_callbacks

    callbacks = await _picker_extra_callbacks(fresh_db, user_id, monkeypatch)

    assert "ai:buildworkout" in callbacks
    assert "ai:buildprog" not in callbacks
