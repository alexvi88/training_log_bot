"""/ai_retry — админ отвечает атлету на вопрос, на котором тренер сорвался
(handlers/admin.py → handlers/ai_trainer.retry_failed_question)."""

import html
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import activity_log
import config
import i18n
from fsm import AITrainerFlow
from handlers import admin
from handlers import ai_trainer as ai_handler

pytestmark = pytest.mark.asyncio

ADMIN_ID = 999
BOT_ID = 1


def _admin_message(user_id: int = ADMIN_ID):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="admin", language_code=None)
    message.chat = SimpleNamespace(id=user_id)
    message.answer = AsyncMock()
    return message


def _bot():
    """Бот, который «отправляет» в чат атлета: placeholder правится на месте."""
    bot = MagicMock()
    bot.id = BOT_ID
    sent: list = []

    async def send_message(chat_id, text, **kwargs):
        placeholder = MagicMock()
        placeholder.chat = SimpleNamespace(id=chat_id)
        placeholder.message_id = 100 + len(sent)
        placeholder.bot = bot
        placeholder.edit_text = AsyncMock()
        placeholder.delete = AsyncMock()
        sent.append((chat_id, text, placeholder))
        return placeholder

    bot.send_message = AsyncMock(side_effect=send_message)
    bot.send_message_draft = AsyncMock()
    bot.edit_message_text = AsyncMock(side_effect=AttributeError("no rich"))
    bot.send_rich_message = AsyncMock(side_effect=AttributeError("no rich"))
    bot.sent = sent
    return bot


def _callback(data: str, bot, user_id: int = ADMIN_ID):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.edit_text = AsyncMock(return_value=message)
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="admin", language_code=None)
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    callback.bot = bot
    return callback


def _state(storage, user_id: int) -> FSMContext:
    return FSMContext(storage=storage, key=StorageKey(bot_id=BOT_ID, chat_id=user_id, user_id=user_id))


async def _failed_turn(db, user_id: int, question: str = "как поднять жим?") -> None:
    """Как в проде: middleware пишет вопрос, сорвавшийся ход — ai_failed за ним."""
    await db.log_user_event(user_id, activity_log.KIND_MESSAGE, question)
    await activity_log.record_ai_failure(user_id, TimeoutError())


def _answer_text(bot) -> str:
    """Финальный ответ — правка placeholder (единственного отправленного)."""
    (_, _, placeholder), = bot.sent
    return html.unescape(placeholder.edit_text.await_args.args[0])


async def _ai_chat_rows(db, user_id: int) -> list[tuple[str, str]]:
    cur = await db.conn().execute(
        "SELECT role, content FROM ai_chat_messages WHERE telegram_id = ? ORDER BY id", (user_id,)
    )
    return [(row["role"], row["content"]) for row in await cur.fetchall()]


async def test_finds_the_failed_question(fresh_db, user_id):
    await fresh_db.log_user_event(user_id, activity_log.KIND_MESSAGE, "старый вопрос")
    await activity_log.record_ai_reply(user_id, "старый ответ")
    await _failed_turn(fresh_db, user_id, "как поднять жим?")

    found = await admin.find_failed_ai_question(user_id)

    assert found is not None
    assert found["question"] == "как поднять жим?"
    assert found["asked_at"] and found["failed_at"]


async def test_command_shows_the_question_with_confirm_buttons(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    await _failed_turn(fresh_db, user_id, "как поднять жим?")
    message = _admin_message()

    await admin.cmd_ai_retry(message, MagicMock(), SimpleNamespace(args="@tester"))

    text = message.answer.await_args.args[0]
    assert "как поднять жим?" in text
    kb = message.answer.await_args.kwargs["reply_markup"]
    buttons = [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]
    assert buttons[0][0] == "Ответить" and buttons[0][1].startswith(f"admin:air:go:{user_id}:")
    assert buttons[1] == ("Отмена", "admin:air:no")


async def test_answered_failure_is_not_offered(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    await _failed_turn(fresh_db, user_id)
    await fresh_db.log_user_event(user_id, activity_log.KIND_MESSAGE, "ещё раз")
    await activity_log.record_ai_reply(user_id, "ответ")
    assert await admin.find_failed_ai_question(user_id) is None

    message = _admin_message()
    await admin.cmd_ai_retry(message, MagicMock(), SimpleNamespace(args=str(user_id)))
    assert "нет вопроса без ответа" in message.answer.await_args.args[0]
    assert "reply_markup" not in message.answer.await_args.kwargs


async def test_no_failure_in_the_last_week(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    await _failed_turn(fresh_db, user_id)
    await fresh_db.conn().execute(
        "UPDATE user_events SET created_at = '2020-01-01T00:00:00' WHERE telegram_id = ?", (user_id,)
    )
    await fresh_db.conn().commit()
    message = _admin_message()

    await admin.cmd_ai_retry(message, MagicMock(), SimpleNamespace(args=str(user_id)))

    assert "нет вопроса без ответа" in message.answer.await_args.args[0]


async def test_voice_question_cannot_be_replayed(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    await fresh_db.log_user_event(user_id, activity_log.KIND_MESSAGE, "🎤 голосовое")
    await activity_log.record_ai_failure(user_id, TimeoutError())
    message = _admin_message()

    await admin.cmd_ai_retry(message, MagicMock(), SimpleNamespace(args=str(user_id)))

    assert "не текстом" in message.answer.await_args.args[0]
    assert "reply_markup" not in message.answer.await_args.kwargs


async def test_non_admin_cannot_run_it(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    await _failed_turn(fresh_db, user_id)
    ask = AsyncMock(return_value="ответ")
    monkeypatch.setattr(ai_handler.ai_trainer, "ask", ask)
    message = _admin_message(user_id=555)

    await admin.cmd_ai_retry(message, MagicMock(), SimpleNamespace(args=str(user_id)))
    message.answer.assert_not_awaited()

    found = await admin.find_failed_ai_question(user_id)
    bot = _bot()
    callback = _callback(f"admin:air:go:{user_id}:{found['failed_id']}", bot, user_id=555)
    await admin.ai_retry_confirm(callback, _state(MemoryStorage(), 555))
    ask.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_confirm_answers_the_athlete_in_their_language(fresh_db, user_id, monkeypatch, lang):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    await fresh_db.set_user_lang(user_id, lang)
    await _failed_turn(fresh_db, user_id, "как поднять жим?")
    storage = MemoryStorage()
    athlete_state = _state(storage, user_id)
    earlier = [{"role": "user", "content": "привет"}, {"role": "assistant", "content": "здорово"}]
    await athlete_state.update_data(ai_history=earlier)

    seen: dict = {}

    async def fake_ask(uid, question, history, **kwargs):
        seen.update(uid=uid, question=question, history=list(history), lang=i18n.get_lang())
        return "Жми 3×5 и добавляй по 2.5кг."

    monkeypatch.setattr(ai_handler.ai_trainer, "ask", fake_ask)
    found = await admin.find_failed_ai_question(user_id)
    bot = _bot()
    callback = _callback(f"admin:air:go:{user_id}:{found['failed_id']}", bot)

    # Апдейт пришёл от админа: его язык в контексте не должен протечь атлету.
    with i18n.use_lang("en" if lang == "ru" else "ru"):
        await admin.ai_retry_confirm(callback, _state(storage, ADMIN_ID))

    # Тот же путь, что у обычного вопроса: его user_id, его история, его язык.
    assert seen == {"uid": user_id, "question": "как поднять жим?", "history": earlier, "lang": lang}
    chat_id, _, _ = bot.sent[0]
    assert chat_id == user_id
    text = _answer_text(bot)
    prefix = i18n.t_in(lang, "ai.screen.retry_prefix")
    assert text.startswith(prefix)
    assert "Жми 3×5" in text
    # Квоту не списали: сбой был наш.
    assert await fresh_db.get_ai_question_count_today(user_id) == 0
    # История дописана обычным ходом, без извинения внутри.
    history = (await athlete_state.get_data())["ai_history"]
    assert history[:2] == earlier
    assert history[-2:] == [
        {"role": "user", "content": "как поднять жим?"},
        {"role": "assistant", "content": "Жми 3×5 и добавляй по 2.5кг."},
    ]
    assert await athlete_state.get_state() == AITrainerFlow.chatting.state
    assert await _ai_chat_rows(fresh_db, user_id) == [
        ("user", "как поднять жим?"), ("assistant", "Жми 3×5 и добавляй по 2.5кг."),
    ]
    # Ответ в ленте — и повторно тот же сбой уже не предлагается.
    assert await admin.find_failed_ai_question(user_id) is None
    report = callback.message.answer.await_args.args[0]
    assert "Ответ отправлен" in report
    assert user_id not in ai_handler._busy


async def test_retry_failure_reports_to_admin_and_keeps_quota(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    await _failed_turn(fresh_db, user_id)

    async def failing_ask(*args, **kwargs):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(ai_handler.ai_trainer, "ask", failing_ask)
    found = await admin.find_failed_ai_question(user_id)
    bot = _bot()
    callback = _callback(f"admin:air:go:{user_id}:{found['failed_id']}", bot)

    await admin.ai_retry_confirm(callback, _state(MemoryStorage(), ADMIN_ID))

    report = callback.message.answer.await_args.args[0]
    assert "Не отправил" in report and "RuntimeError" in report
    # Атлет второй раз «не смог» не видит: «думаю…» убран, а не переписан.
    (_, _, placeholder), = bot.sent
    placeholder.delete.assert_awaited()
    placeholder.edit_text.assert_not_awaited()
    assert await fresh_db.get_ai_question_count_today(user_id) == 0
    assert user_id not in ai_handler._busy


async def test_retry_respects_the_hard_money_stop(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    await _failed_turn(fresh_db, user_id)
    ask = AsyncMock(return_value="ответ")
    monkeypatch.setattr(ai_handler.ai_trainer, "ask", ask)

    async def hard_stop():
        return ai_handler.ai_limits.Block(kind="spend_hard", log="spend_hard: ~$99.00")

    monkeypatch.setattr(ai_handler.ai_limits, "hard_stop_block", hard_stop)
    found = await admin.find_failed_ai_question(user_id)
    bot = _bot()
    callback = _callback(f"admin:air:go:{user_id}:{found['failed_id']}", bot)

    await admin.ai_retry_confirm(callback, _state(MemoryStorage(), ADMIN_ID))

    ask.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    assert "стоп по деньгам" in callback.message.answer.await_args.args[0]


async def test_quota_is_not_a_blocker_for_the_retry(fresh_db, user_id, monkeypatch):
    """Атлет выбрал дневную квоту — повтор всё равно отвечает и не списывает."""
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    for _ in range(config.AI_QUESTION_DAILY_LIMIT):
        await fresh_db.increment_ai_question_count(user_id)
    await _failed_turn(fresh_db, user_id)
    monkeypatch.setattr(ai_handler.ai_trainer, "ask", AsyncMock(return_value="ответ"))
    found = await admin.find_failed_ai_question(user_id)
    bot = _bot()
    callback = _callback(f"admin:air:go:{user_id}:{found['failed_id']}", bot)

    await admin.ai_retry_confirm(callback, _state(MemoryStorage(), ADMIN_ID))

    assert "Ответ отправлен" in callback.message.answer.await_args.args[0]
    assert await fresh_db.get_ai_question_count_today(user_id) == config.AI_QUESTION_DAILY_LIMIT
