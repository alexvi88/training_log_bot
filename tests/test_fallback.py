"""Что происходит с вводом, который не подхватил ни один обработчик.

Про текст это было и раньше. Про кнопки — нет, и это была самая заметная поломка
в боте: больше сотни обработчиков стоят под `StateFilter`, после `state.clear()`
их callback не брал никто, а Telegram без `answer()` крутит спиннер секунд
десять и гасит его молча. Ни ответа, ни ошибки, ни строчки в логах.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.enums import ContentType
from aiogram.types import CallbackQuery

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
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
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
    message.from_user = SimpleNamespace(id=user_id)
    message.text = "составь мне программу"
    message.content_type = ContentType.TEXT
    message.reply = AsyncMock()

    await fallback.unhandled_text(message)

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
    message.from_user = SimpleNamespace(id=user_id)
    message.text = "100 8"
    message.content_type = ContentType.TEXT
    message.reply = AsyncMock()

    await fallback.unhandled_text(message)

    message.reply.assert_awaited_once()
    reply, kwargs = message.reply.await_args.args, message.reply.await_args.kwargs
    text = reply[0]
    assert "подход" in text.lower()
    kb = kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "menu:start_workout"


async def test_pure_weight_x_reps_form_also_points_at_starting_one(fresh_db, user_id):
    """"100x8x3" — тоже подход, только через другой сепаратор в parse_sets_line."""
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.text = "100x8x3"
    message.content_type = ContentType.TEXT
    message.reply = AsyncMock()

    await fallback.unhandled_text(message)

    message.reply.assert_awaited_once()
    kb = message.reply.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "menu:start_workout"


async def test_typing_a_known_exercise_name_offers_its_card(fresh_db, user_id):
    group_id = await fresh_db.create_muscle_group(user_id, "Грудь")
    ex_id = await fresh_db.create_exercise(user_id, "Жим штанги лёжа", group_id)
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.text = "жим"
    message.content_type = ContentType.TEXT
    message.reply = AsyncMock()

    await fallback.unhandled_text(message)

    message.reply.assert_awaited_once()
    text = message.reply.await_args.args[0]
    kb = message.reply.await_args.kwargs["reply_markup"]
    assert "Жим штанги лёжа" in text
    assert kb.inline_keyboard[0][0].callback_data == f"prog:card:{ex_id}"


async def test_unknown_exercise_name_still_falls_back_to_the_generic_reply(fresh_db, user_id):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.text = "совсем незнакомое упражнение зюзюка"
    message.content_type = ContentType.TEXT
    message.reply = AsyncMock()

    await fallback.unhandled_text(message)

    message.reply.assert_awaited_once()
    text = message.reply.await_args.args[0]
    assert "Не понял" in text


async def test_a_command_never_goes_through_the_new_routes(fresh_db, user_id):
    """Незнакомая команда должна получать тот же общий ответ, что и раньше — не
    пытаемся распарсить "/xyz" как подход или найти его в упражнениях."""
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.text = "/xyz"
    message.content_type = ContentType.TEXT
    message.reply = AsyncMock()

    await fallback.unhandled_text(message)

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
    message.from_user = SimpleNamespace(id=1)
    message.text = None
    message.content_type = ContentType.PINNED_MESSAGE
    message.reply = AsyncMock()

    await fallback.unhandled_text(message)

    message.reply.assert_not_awaited()
