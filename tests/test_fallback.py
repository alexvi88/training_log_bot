"""Что происходит с вводом, который не подхватил ни один обработчик.

Про текст это было и раньше. Про кнопки — нет, и это была самая заметная поломка
в боте: больше сотни обработчиков стоят под `StateFilter`, после `state.clear()`
их callback не брал никто, а Telegram без `answer()` крутит спиннер секунд
десять и гасит его молча. Ни ответа, ни ошибки, ни строчки в логах.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ContentType
from aiogram.types import CallbackQuery

import activity_log
from handlers import fallback


def _callback(user_id: int = 1, data: str = "pick:grp:7", *, inaccessible: bool = False):
    """Кнопка, нажатая на экране, который уже никем не обслуживается.

    `inaccessible=True` — то, что Telegram присылает для слишком старых или
    удалённых сообщений: `InaccessibleMessage`, у которого нет ни `text`, ни
    `answer` — только чат и id.
    """
    message = MagicMock(spec=["chat", "message_id"] if inaccessible else None)
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 10
    if not inaccessible:
        message.answer = AsyncMock(return_value=SimpleNamespace(message_id=11))
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.data = data
    callback.bot = MagicMock()
    callback.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=12))
    callback.bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=12))
    return callback


async def test_unhandled_text_points_to_ai_trainer_and_start(fresh_db, user_id):
    """Самый частый непонятый текст — вопрос тренеру, напечатанный из главного
    меню («составь мне программу»): ответ обязан вести и к AI-тренеру, и в меню."""
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, language_code=None)
    message.text = "составь мне программу"
    message.content_type = ContentType.TEXT
    message.reply = AsyncMock()

    await fallback.unhandled_text(message, MagicMock())

    message.reply.assert_awaited_once()
    reply = message.reply.await_args.args[0]
    assert "AI-тренер" in reply
    assert "/start" in reply


# ---------- находка: главное меню без активной тренировки маршрутизирует ----------
#
# «жим» с главного меню раньше падал в общий фолбэк («Не понял») — детерминиро-
# ванная (без единого платного вызова) развилка перехватывает два частых случая
# до этого: похоже на подход, или совпадает с уже заведённым упражнением.


async def test_a_set_typed_with_no_active_workout_points_at_starting_one(fresh_db, user_id):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, language_code=None)
    message.text = "100 8"
    message.content_type = ContentType.TEXT
    message.reply = AsyncMock()

    await fallback.unhandled_text(message, MagicMock())

    message.reply.assert_awaited_once()
    reply, kwargs = message.reply.await_args.args, message.reply.await_args.kwargs
    text = reply[0]
    assert "подход" in text.lower()
    kb = kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "menu:start_workout"


@pytest.mark.parametrize("text", ["15 kg / 90 reps", "10 Kg x 4", "100 кг 8 раз"])
async def test_a_set_with_unit_words_also_points_at_starting_one(fresh_db, user_id, text):
    """Живой лог 19.08: англоязычные новички писали подход со словами единиц, и
    это уходило в поиск упражнений («ничего не нашлось») вместо подсказки —
    самый массовый тупик первого действия."""
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, language_code=None)
    message.text = text
    message.content_type = ContentType.TEXT
    message.reply = AsyncMock()

    await fallback.unhandled_text(message, MagicMock())

    message.reply.assert_awaited_once()
    kb = message.reply.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "menu:start_workout"


async def test_pure_weight_x_reps_form_also_points_at_starting_one(fresh_db, user_id):
    """"100x8x3" — тоже подход, только через другой сепаратор в parse_sets_line."""
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, language_code=None)
    message.text = "100x8x3"
    message.content_type = ContentType.TEXT
    message.reply = AsyncMock()

    await fallback.unhandled_text(message, MagicMock())

    message.reply.assert_awaited_once()
    kb = message.reply.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "menu:start_workout"


async def test_typing_a_known_exercise_name_offers_its_card(fresh_db, user_id):
    group_id = await fresh_db.create_muscle_group(user_id, "Грудь")
    ex_id = await fresh_db.create_exercise(user_id, "Жим штанги лёжа", group_id)
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, language_code=None)
    message.text = "жим"
    message.content_type = ContentType.TEXT
    message.reply = AsyncMock()

    await fallback.unhandled_text(message, MagicMock())

    message.reply.assert_awaited_once()
    text = message.reply.await_args.args[0]
    kb = message.reply.await_args.kwargs["reply_markup"]
    assert "Жим штанги лёжа" in text
    assert kb.inline_keyboard[0][0].callback_data == f"prog:card:{ex_id}"


async def test_unknown_exercise_name_still_falls_back_to_the_generic_reply(fresh_db, user_id):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, language_code=None)
    message.text = "совсем незнакомое упражнение зюзюка"
    message.content_type = ContentType.TEXT
    message.reply = AsyncMock()

    await fallback.unhandled_text(message, MagicMock())

    message.reply.assert_awaited_once()
    text = message.reply.await_args.args[0]
    assert "Не понял" in text


async def test_a_command_never_goes_through_the_new_routes(fresh_db, user_id):
    """Незнакомая команда должна получать тот же общий ответ, что и раньше — не
    пытаемся распарсить "/xyz" как подход или найти его в упражнениях."""
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, language_code=None)
    message.text = "/xyz"
    message.content_type = ContentType.TEXT
    message.reply = AsyncMock()

    await fallback.unhandled_text(message, MagicMock())

    message.reply.assert_awaited_once()
    text = message.reply.await_args.args[0]
    assert "Не понял" in text


async def test_an_unhandled_button_answers_and_opens_the_menu(fresh_db, user_id):
    """Спиннер обязан погаснуть, и человек обязан оказаться там, откуда можно
    продолжить. «Экран устарел, нажми /start» — это работа, переложенная на него."""
    callback = _callback(user_id)
    state = MagicMock()
    state.get_data = AsyncMock(return_value={})
    state.clear = AsyncMock()
    state.update_data = AsyncMock()

    await fallback.unhandled_callback(callback, state)

    callback.answer.assert_awaited_once()
    callback.bot.send_message.assert_awaited()
    assert callback.bot.send_message.await_args.args[0] == user_id


async def test_an_unhandled_button_is_logged_separately_by_prefix(fresh_db, user_id):
    """Регрессия: обычная запись KIND_CALLBACK одинакова для живой и протухшей
    кнопки — без отдельного вида события вспышку одного префикса (регресс
    роутинга) было не отличить от фонового шума устаревших экранов."""
    callback = _callback(user_id, data="wo:set:12:3")
    state = MagicMock()
    state.get_data = AsyncMock(return_value={})
    state.clear = AsyncMock()
    state.update_data = AsyncMock()

    await fallback.unhandled_callback(callback, state)

    rows = await fresh_db.count_unhandled_callbacks_by_prefix("2000-01-01T00:00:00")
    assert [(r["prefix"], r["n"]) for r in rows] == [("wo:set", 1)]


async def test_a_button_from_an_inaccessible_message_does_not_crash(fresh_db, user_id):
    """Сообщение старше суток Telegram отдаёт как `InaccessibleMessage`: у него
    нет ни `.text`, ни `.answer`. Обращаться к ним — `AttributeError` вместо
    экрана, а это ровно тот случай, когда кнопку и нажимают."""
    callback = _callback(user_id, inaccessible=True)
    state = MagicMock()
    state.get_data = AsyncMock(return_value={})
    state.clear = AsyncMock()
    state.update_data = AsyncMock()

    await fallback.unhandled_callback(callback, state)

    callback.answer.assert_awaited_once()
    callback.bot.send_message.assert_awaited()


async def test_service_messages_get_no_answer_at_all():
    """Закрепил человек сообщение бота — Telegram шлёт такой же апдейт, и бот
    отвечал на него «Не понял 🤔». То есть выговор за действие, которого никто
    не совершал: человек ничего боту не писал."""
    message = MagicMock()
    message.from_user = SimpleNamespace(id=1, language_code=None)
    message.text = None
    message.content_type = ContentType.PINNED_MESSAGE
    message.reply = AsyncMock()

    await fallback.unhandled_text(message, MagicMock())

    message.reply.assert_not_awaited()


# ---------- живая тренировка не теряется от мёртвой кнопки трекера ----------
#
# Все кнопки трекера стоят под StateFilter, а состояние теряется от любого
# потока со своим: импорт CSV, мини-игра, опросник тренера. Тренировка при этом
# жива в базе, и человек, нажавший на своём же экране «✅ Закончить упражнение»,
# получал тост «кнопка устарела» и главное меню — то есть ровно то, что читается
# как «тренировку потеряли». В логе 19.08 это 💀 live:finish_exercise.


async def _live_state(user_id: int):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


async def test_tracker_button_without_state_brings_the_workout_back(fresh_db, user_id):
    group_id = await fresh_db.create_muscle_group(user_id, "Грудь")
    ex_id = await fresh_db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await fresh_db.create_workout(user_id)
    block_id = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_block_exercise(block_id, ex_id, 0)
    await fresh_db.add_set(block_id, ex_id, 1, 0, 100.0, 8)

    callback = _callback(user_id, "live:finish_exercise")
    # `_enter_live` отправляет новый экран через `message.answer` — ответ должен
    # выглядеть как настоящее сообщение (у него читают chat.id и message_id).
    callback.message.answer = AsyncMock(
        return_value=SimpleNamespace(message_id=11, chat=SimpleNamespace(id=user_id))
    )
    callback.bot = AsyncMock()
    callback.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=12, chat=SimpleNamespace(id=user_id))
    )
    state = await _live_state(user_id)

    await fallback.unhandled_callback(callback, state)

    # Экран трекера пересобран, и состояние снова знает про тренировку.
    data = await state.get_data()
    assert data["workout_id"] == workout_id
    assert data["open_exercises"] == [ex_id]
    # Тост — про возвращённую тренировку, а не про устаревшую кнопку.
    toast = callback.answer.await_args.args[0]
    assert "устарел" not in toast.lower()
    # И в логе это своё событие, а не «протухшая кнопка»: под общим видом
    # спасённый тап навсегда читался бы как регресс роутинга — и в /activity, и
    # в утреннем разборе (он два дня подряд подавал это как баг).
    (event,) = await fresh_db.list_user_events(user_id, limit=10)
    assert event["kind"] == activity_log.KIND_CALLBACK_RECOVERED
    assert event["content"] == "live:finish_exercise"
    assert await fresh_db.count_unhandled_callbacks_by_prefix("2000-01-01") == []


async def test_tracker_button_without_a_workout_still_opens_the_menu(fresh_db, user_id):
    """Тренировки нет — восстанавливать нечего, остаётся прежний ответ. И в логе
    это по-прежнему протухшая кнопка: счётчик префиксов ловит регресс роутинга
    ровно по таким событиям, и прятать их нельзя."""
    callback = _callback(user_id, "live:finish_exercise")
    state = await _live_state(user_id)

    await fallback.unhandled_callback(callback, state)

    assert (await state.get_data()).get("workout_id") is None
    callback.answer.assert_awaited_once()
    (event,) = await fresh_db.list_user_events(user_id, limit=10)
    assert event["kind"] == activity_log.KIND_CALLBACK_UNHANDLED
    counted = await fresh_db.count_unhandled_callbacks_by_prefix("2000-01-01")
    assert [(r["prefix"], r["n"]) for r in counted] == [("live:finish_exercise", 1)]


async def test_recovery_is_only_for_tracker_buttons(fresh_db, user_id):
    """Кнопка не из трекера при живой тренировке ведёт себя как раньше: у
    протухших экранов сотни префиксов, и подменять им всем ответ на «вернул
    тренировку» значило бы врать про то, куда человек нажал."""
    await fresh_db.create_workout(user_id)
    callback = _callback(user_id, "pick:grp:7")
    state = await _live_state(user_id)

    await fallback.unhandled_callback(callback, state)

    assert (await state.get_data()).get("workout_id") is None


# ---------- голос/фото вне ожидающего состояния — «🤖 Спросить тренера про это» ----------


async def _fb_state(user_id: int):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


async def test_voice_with_no_state_offers_ask_coach_button(user_id):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, language_code=None)
    message.content_type = ContentType.VOICE
    message.voice = SimpleNamespace(file_id="voice123", file_size=5000, duration=7)
    message.message_id = 77
    message.reply = AsyncMock()
    state = await _fb_state(user_id)

    await fallback.unhandled_text(message, state)

    message.reply.assert_awaited_once()
    text, kwargs = message.reply.await_args.args[0], message.reply.await_args.kwargs
    assert "Не понял" in text
    kb = kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "fb:ask_coach"
    data = await state.get_data()
    assert data["fb_media_kind"] == "voice"
    assert data["fb_file_id"] == "voice123"
    assert data["fb_file_size"] == 5000
    assert data["fb_duration"] == 7


async def test_photo_with_no_state_offers_ask_coach_button(user_id):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, language_code=None)
    message.content_type = ContentType.PHOTO
    message.photo = [SimpleNamespace(file_id="photo123", file_size=9000)]
    message.caption = "жим лёжа"
    message.message_id = 78
    message.reply = AsyncMock()
    state = await _fb_state(user_id)

    await fallback.unhandled_text(message, state)

    message.reply.assert_awaited_once()
    data = await state.get_data()
    assert data["fb_media_kind"] == "photo"
    assert data["fb_file_id"] == "photo123"
    assert data["fb_caption"] == "жим лёжа"


async def test_fb_ask_coach_dispatches_stored_voice_to_the_ai_handler(user_id, monkeypatch):
    from fsm import AITrainerFlow

    state = await _fb_state(user_id)
    await state.update_data(
        fb_media_kind="voice", fb_file_id="voice123", fb_file_size=5000,
        fb_duration=7, fb_caption=None, fb_message_id=77,
    )
    callback = _callback(user_id, "fb:ask_coach")
    callback.message.edit_reply_markup = AsyncMock()

    called = {}

    async def fake_ai_voice_question(msg, st):
        called["file_id"] = msg.voice.file_id
        called["file_size"] = msg.voice.file_size
        called["duration"] = msg.voice.duration

    monkeypatch.setattr("handlers.ai_trainer.ai_voice_question", fake_ai_voice_question)

    await fallback.fb_ask_coach(callback, state)

    assert called == {"file_id": "voice123", "file_size": 5000, "duration": 7}
    assert await state.get_state() == AITrainerFlow.chatting.state
    # Данные разово потрачены, повторный тап той же кнопки не сработает молча.
    assert (await state.get_data()).get("fb_file_id") is None


async def test_fb_ask_coach_dispatches_stored_photo_to_the_ai_handler(user_id, monkeypatch):
    state = await _fb_state(user_id)
    await state.update_data(
        fb_media_kind="photo", fb_file_id="photo123", fb_file_size=9000,
        fb_duration=None, fb_caption="жим лёжа", fb_message_id=78,
    )
    callback = _callback(user_id, "fb:ask_coach")
    callback.message.edit_reply_markup = AsyncMock()

    called = {}

    async def fake_ai_photo_question(msg, st):
        called["file_id"] = msg.photo[-1].file_id
        called["caption"] = msg.caption

    monkeypatch.setattr("handlers.ai_trainer.ai_photo_question", fake_ai_photo_question)

    await fallback.fb_ask_coach(callback, state)

    assert called == {"file_id": "photo123", "caption": "жим лёжа"}


async def test_fb_ask_coach_without_stored_media_answers_expired():
    callback = _callback(1, "fb:ask_coach")
    state = MagicMock()
    state.get_data = AsyncMock(return_value={})

    await fallback.fb_ask_coach(callback, state)

    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get("show_alert") is True
