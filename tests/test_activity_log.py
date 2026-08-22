"""Лог действий: что попадает в user_events и что из него видит админ.

Смысл фичи — видеть путь, а не только результат, поэтому тесты в первую очередь
про то, что в лог попадает и то, чего не поймал ни один хендлер, и то, что
хендлер уронил.
"""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

import activity_log
import config
import db
from fsm import AdminFlow
from handlers import admin

pytestmark = pytest.mark.asyncio

ADMIN_ID = 999


def _message(user_id: int = 111, text: str | None = "жим 100х5", **kwargs) -> Message:
    message = MagicMock(spec=Message)
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 7
    message.text = text
    message.caption = kwargs.pop("caption", None)
    for attr, _mark in activity_log._MEDIA_MARKS:
        setattr(message, attr, kwargs.pop(attr, None))
    message.answer = AsyncMock()
    return message


def _callback(user_id: int = 111, data: str = "wo:finish", buttons=(("Завершить", "wo:finish"),)) -> CallbackQuery:
    keyboard = [[SimpleNamespace(text=text, callback_data=cb) for text, cb in buttons]]
    inner = MagicMock()
    inner.reply_markup = SimpleNamespace(inline_keyboard=keyboard)
    inner.chat = SimpleNamespace(id=user_id)
    inner.message_id = 1
    inner.edit_text = AsyncMock(return_value=inner)
    inner.delete = AsyncMock()
    inner.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = inner
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _screen_text(callback) -> str:
    """Текст экрана, который увидел админ.

    ui.safe_edit либо правит сообщение на месте, либо удаляет его и шлёт новое
    (см. chat_bottom) — тесту важно содержимое, а не то, каким из двух путей оно
    доехало.
    """
    for mock in (callback.message.edit_text, callback.message.answer):
        if mock.await_args is not None:
            return mock.await_args.args[0]
    raise AssertionError("экран так и не показали")


async def _state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


async def _pass_through(event, data):
    return "handled"


# ---------- что записывается ----------


async def test_typed_text_is_stored_verbatim(fresh_db, user_id):
    await activity_log.LogIncomingMessages()(_pass_through, _message(user_id), {})

    (row,) = await db.list_user_events(user_id)
    assert row["kind"] == activity_log.KIND_MESSAGE
    assert row["content"] == "жим 100х5"


async def test_a_voice_message_leaves_a_trace_even_without_text(fresh_db, user_id):
    await activity_log.LogIncomingMessages()(_pass_through, _message(user_id, text=None, voice=object()), {})

    (row,) = await db.list_user_events(user_id)
    assert "голосовое" in row["content"]


async def test_a_photo_caption_is_kept_next_to_the_photo_mark(fresh_db, user_id):
    message = _message(user_id, text=None, caption="обед", photo=[object()])
    await activity_log.LogIncomingMessages()(_pass_through, message, {})

    (row,) = await db.list_user_events(user_id)
    assert "фото" in row["content"] and "обед" in row["content"]


async def test_a_bottom_keyboard_press_is_logged_as_a_button_not_as_typed_text(
    fresh_db, user_id
):
    """«Workout»/«Тренировка» приходят обычным сообщением — это нажатие нижней
    клавиатуры, а не набранное слово. Под видом обычного текста утренний разбор
    два дня подряд выводил из них несуществующий баг «свободный текст не
    мапится на мастер тренировки»."""
    for text in ("Workout", "Тренировка", "Меню", "AI Coach"):
        await activity_log.LogIncomingMessages()(_pass_through, _message(user_id, text=text), {})

    rows = await db.list_user_events(user_id)
    assert {row["kind"] for row in rows} == {activity_log.KIND_REPLY_BUTTON}
    assert {row["content"] for row in rows} == {"Workout", "Тренировка", "Меню", "AI Coach"}


async def test_text_that_merely_mentions_a_button_stays_a_message(fresh_db, user_id):
    """Совпадать должна ВСЯ подпись: «workout tomorrow» — это фраза человеку,
    а не тап."""
    await activity_log.LogIncomingMessages()(
        _pass_through, _message(user_id, text="workout tomorrow"), {}
    )

    (row,) = await db.list_user_events(user_id)
    assert row["kind"] == activity_log.KIND_MESSAGE


async def test_a_tap_is_stored_as_the_label_the_user_saw(fresh_db, user_id):
    """callback_data вроде `wo:finish` — это про бота. Что нажал человек,
    отвечает надпись на кнопке, поэтому в ленте она, а данные — рядом."""
    await activity_log.LogCallbackQueries()(_pass_through, _callback(user_id), {})

    (row,) = await db.list_user_events(user_id)
    assert row["kind"] == activity_log.KIND_CALLBACK
    assert row["content"] == "Завершить"
    assert row["payload"] == "wo:finish"


async def test_a_tap_on_a_vanished_keyboard_still_lands_in_the_log(fresh_db, user_id):
    callback = _callback(user_id, data="wo:set:12", buttons=())
    await activity_log.LogCallbackQueries()(_pass_through, callback, {})

    (row,) = await db.list_user_events(user_id)
    assert row["content"] == "wo:set:12"


async def test_long_input_is_truncated_before_it_reaches_the_db(fresh_db, user_id):
    await activity_log.LogIncomingMessages()(_pass_through, _message(user_id, text="а" * 5000), {})

    (row,) = await db.list_user_events(user_id)
    assert len(row["content"]) == activity_log.MAX_CONTENT_LEN


async def test_the_event_is_logged_even_when_the_handler_blows_up(fresh_db, user_id):
    """Упавший хендлер — ровно тот случай, ради которого лента и заводится."""

    async def failing(event, data):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await activity_log.LogIncomingMessages()(failing, _message(user_id), {})

    assert len(await db.list_user_events(user_id)) == 1


async def test_a_broken_log_write_never_breaks_the_users_workout(fresh_db, user_id, monkeypatch):
    async def explode(*args, **kwargs):
        raise RuntimeError("db is gone")

    monkeypatch.setattr(db, "log_user_event", explode)

    assert await activity_log.LogIncomingMessages()(_pass_through, _message(user_id), {}) == "handled"


async def test_the_handler_still_runs_and_gets_its_result(fresh_db, user_id):
    assert await activity_log.LogIncomingMessages()(_pass_through, _message(user_id), {}) == "handled"
    assert await activity_log.LogCallbackQueries()(_pass_through, _callback(user_id), {}) == "handled"


# ---------- чистка ----------


async def test_old_events_are_pruned_and_recent_ones_stay(fresh_db, user_id):
    await db.log_user_event(user_id, activity_log.KIND_MESSAGE, "свежее")
    old = (dt.datetime.now() - dt.timedelta(days=40)).isoformat(timespec="seconds")
    await db.conn().execute(
        "INSERT INTO user_events (telegram_id, kind, content, created_at) VALUES (?, ?, ?, ?)",
        (user_id, activity_log.KIND_MESSAGE, "старое", old),
    )
    await db.conn().commit()

    assert await db.prune_old_user_events(30) == 1
    (row,) = await db.list_user_events(user_id)
    assert row["content"] == "свежее"


# ---------- админский экран ----------


async def test_non_admin_gets_nothing_from_the_activity_command(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    message = _message(user_id)
    state = await _state(user_id)

    await admin.cmd_activity(message, state)

    message.answer.assert_not_awaited()
    assert await state.get_state() is None


async def test_the_user_list_counts_events_and_keeps_silent_users(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    await db.get_or_create_user(telegram_id=222, username="silent")
    await db.log_user_event(user_id, activity_log.KIND_MESSAGE, "жим 100х5")

    message = _message(ADMIN_ID)
    state = await _state(ADMIN_ID)
    await admin.cmd_activity(message, state)

    assert await state.get_state() == AdminFlow.browsing_activity_users.state
    kb = message.answer.await_args.kwargs["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "@tester (1)" in labels
    assert "@silent (0)" in labels


async def test_the_feed_shows_what_was_typed_and_tapped_newest_first(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    await activity_log.LogIncomingMessages()(_pass_through, _message(user_id), {})
    await activity_log.LogCallbackQueries()(_pass_through, _callback(user_id), {})

    callback = _callback(ADMIN_ID, data=f"admin:acu:{user_id}")
    state = await _state(ADMIN_ID)
    await admin.admin_activity_pick_user(callback, state)

    assert await state.get_state() == AdminFlow.browsing_activity.state
    text = _screen_text(callback)
    assert "@tester" in text
    assert text.index("Завершить") < text.index("жим 100х5")


async def test_the_feed_pages_back_into_older_events(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    for i in range(admin.ACTIVITY_PAGE_SIZE + 1):
        await db.log_user_event(user_id, activity_log.KIND_MESSAGE, f"событие {i}")

    state = await _state(ADMIN_ID)
    callback = _callback(ADMIN_ID, data=f"admin:acf:{user_id}:1")
    await admin.admin_activity_feed_page(callback, state)

    text = _screen_text(callback)
    assert "событие 0" in text
    assert "событие 25" not in text


async def test_a_user_with_no_events_gets_an_honest_screen(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    callback = _callback(ADMIN_ID, data=f"admin:acu:{user_id}")

    await admin.admin_activity_pick_user(callback, await _state(ADMIN_ID))

    assert "действий пока нет" in _screen_text(callback)


async def test_a_non_admin_cannot_open_someone_elses_feed(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    await db.log_user_event(user_id, activity_log.KIND_MESSAGE, "секрет")
    callback = _callback(222, data=f"admin:acu:{user_id}")

    await admin.admin_activity_pick_user(callback, await _state(222))

    callback.message.edit_text.assert_not_awaited()
    callback.message.answer.assert_not_awaited()


async def test_newlines_do_not_break_the_one_event_per_line_feed(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    await db.log_user_event(user_id, activity_log.KIND_MESSAGE, "жим 100х5\nтяга 120х5")

    callback = _callback(ADMIN_ID, data=f"admin:acu:{user_id}")
    await admin.admin_activity_pick_user(callback, await _state(ADMIN_ID))

    text = _screen_text(callback)
    assert "жим 100х5 ⏎ тяга 120х5" in text


# ---------- общая лента по всем пользователям ----------


async def test_the_users_list_offers_a_button_for_the_shared_feed(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    message = _message(ADMIN_ID)
    state = await _state(ADMIN_ID)

    await admin.cmd_activity(message, state)

    kb = message.answer.await_args.kwargs["reply_markup"]
    buttons = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
    assert buttons.get("admin:aca:0") == "🌐 Все пользователи"


async def test_the_shared_feed_mixes_events_from_every_user_newest_first(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    await db.get_or_create_user(telegram_id=222, username="other")
    await db.log_user_event(user_id, activity_log.KIND_MESSAGE, "жим 100х5")
    await db.log_user_event(222, activity_log.KIND_MESSAGE, "присед 80х5")

    callback = _callback(ADMIN_ID, data="admin:aca:0")
    state = await _state(ADMIN_ID)
    await admin.admin_activity_all(callback, state)

    assert await state.get_state() == AdminFlow.browsing_activity_all.state
    text = _screen_text(callback)
    assert "<b>@other</b>" in text and "<b>@tester</b>" in text
    assert text.index("присед 80х5") < text.index("жим 100х5")
    used_mock = callback.message.edit_text if callback.message.edit_text.await_args else callback.message.answer
    assert used_mock.await_args.kwargs["parse_mode"] == "HTML"


async def test_the_shared_feed_pages_back_into_older_events(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    for i in range(admin.ACTIVITY_ALL_PAGE_SIZE + 1):
        await db.log_user_event(user_id, activity_log.KIND_MESSAGE, f"событие {i}")

    callback = _callback(ADMIN_ID, data="admin:aca:1")
    await admin.admin_activity_all(callback, await _state(ADMIN_ID))

    text = _screen_text(callback)
    assert "событие 0" in text
    assert f"событие {admin.ACTIVITY_ALL_PAGE_SIZE}" not in text


async def test_a_non_admin_cannot_open_the_shared_feed(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    await db.log_user_event(user_id, activity_log.KIND_MESSAGE, "секрет")
    callback = _callback(222, data="admin:aca:0")

    await admin.admin_activity_all(callback, await _state(222))

    callback.message.edit_text.assert_not_awaited()
    callback.message.answer.assert_not_awaited()
