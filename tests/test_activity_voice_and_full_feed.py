"""Лента /activity: что человек СКАЗАЛ голосом и тексты целиком.

Три вещи, которых владелец не видел в ленте:
- голосовое было одной пометкой «🎤 голосовое» — без расшифровки;
- сообщения и ответы тренера резались до 120 символов с «…»;
- ответ тренера показывался сырым markdown'ом («**не переходил сразу**»).

Расшифровка проверяется сквозь настоящую ai_trainer.transcribe_voice с
подменённым только провайдером — она единственная точка расшифровки и у бота, и
у /v1, и именно в ней расшифровка уходит в ленту.
"""

import base64
import contextlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

import activity_log
import ai_trainer
import api_v1
import config
import db
import formatting
import ui
from handlers import admin

ADMIN_ID = 999
USER_ID = 111


def _provider(text=None, error: Exception | None = None):
    create = AsyncMock(side_effect=error) if error else AsyncMock(return_value=SimpleNamespace(text=text))
    return SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))


def _voice_message(user_id: int = USER_ID) -> Message:
    message = MagicMock(spec=Message)
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 7
    message.text = None
    message.caption = None
    message.content_type = "voice"
    for attr, _mark in activity_log._MEDIA_MARKS:
        setattr(message, attr, None)
    message.voice = SimpleNamespace(duration=3, file_size=1000)
    return message


async def _bot_voice(monkeypatch, provider) -> str:
    """Голосовое через middleware бота; хендлер расшифровывает, как настоящий."""
    monkeypatch.setattr(ai_trainer, "_get_audio_client", lambda: provider)
    await db.get_or_create_user(USER_ID, "tester")

    async def handler(event, data):
        buf = io.BytesIO(b"ogg")
        buf.name = "voice.ogg"
        with contextlib.suppress(RuntimeError):
            await ai_trainer.transcribe_voice(buf, event.from_user.id, duration_seconds=3)
        return "handled"

    assert await activity_log.LogIncomingMessages()(handler, _voice_message(), {}) == "handled"
    rows = await db.list_user_events(USER_ID, limit=10)
    messages = [r for r in rows if r["kind"] == activity_log.KIND_MESSAGE]
    assert len(messages) == 1, "расшифровка — в ту же строку, а не второй строкой"
    return messages[0]["content"]


# ---------- голос в боте ----------


async def test_bot_voice_transcript_lands_in_the_same_feed_row(fresh_db, monkeypatch):
    content = await _bot_voice(monkeypatch, _provider("  жим сто на восемь  "))
    assert content == "🎤 голосовое: жим сто на восемь"


async def test_bot_voice_failure_is_marked_not_recognized(fresh_db, monkeypatch):
    content = await _bot_voice(monkeypatch, _provider(error=RuntimeError("provider down")))
    assert content == "🎤 голосовое (не распознал)"


async def test_bot_voice_empty_transcript_is_marked_not_recognized(fresh_db, monkeypatch):
    content = await _bot_voice(monkeypatch, _provider(""))
    assert content == "🎤 голосовое (не распознал)"


async def test_transcript_without_update_context_finds_the_fresh_voice_row(fresh_db, monkeypatch):
    """Вызов не из апдейта (контекста входа нет) — дописываем свежую строку
    голосового этого человека, нового события не выдумываем."""
    await fresh_db.get_or_create_user(USER_ID, "tester")
    await fresh_db.log_user_event(USER_ID, activity_log.KIND_MESSAGE, activity_log.VOICE_MARK)
    monkeypatch.setattr(ai_trainer, "_get_audio_client", lambda: _provider("тяга 120"))

    await ai_trainer.transcribe_voice(SimpleNamespace(name="voice.ogg"), USER_ID, duration_seconds=2)
    await ai_trainer.transcribe_voice(SimpleNamespace(name="voice.ogg"), 222, duration_seconds=2)

    (row,) = await fresh_db.list_user_events(USER_ID, limit=10)
    assert row["content"] == "🎤 голосовое: тяга 120"
    assert await fresh_db.list_user_events(222, limit=10) == []


async def test_transcript_never_goes_into_product_analytics(fresh_db, monkeypatch):
    await _bot_voice(monkeypatch, _provider("секретная фраза про колено"))
    cur = await db.conn().execute("SELECT event, props FROM analytics_events")
    for event, props in await cur.fetchall():
        assert "колено" not in f"{event} {props or ''}"


# ---------- голос в /v1 (iOS) ----------


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory):
    await fresh_db.get_or_create_user(telegram_id=USER_ID, username="tester")
    code = await fresh_db.issue_oauth_link_code(USER_ID, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


def _audio():
    return f"data:audio/m4a;base64,{base64.b64encode(b'fake-m4a').decode()}"


async def _ios_rows(fresh_db):
    rows = await fresh_db.list_user_events(USER_ID, limit=20)
    return [r for r in rows if r["source"] == activity_log.SOURCE_IOS]


async def test_ios_voice_question_shows_the_transcript(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "_get_audio_client", lambda: _provider("сколько подходов на массу?"))
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/voice", json={"audio_data_url": _audio(), "duration_seconds": 2})
    assert resp.status_code == 200, resp.text

    (row,) = [r for r in await _ios_rows(fresh_db) if r["payload"] == "POST /ai/voice"]
    assert row["content"] == "спросил тренера голосом: 🎤 голосовое: сколько подходов на массу?"


async def test_ios_voice_set_shows_the_transcript(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "_get_audio_client", lambda: _provider("сто на восемь"))
    client = await _linked_client(fresh_db, client_factory)
    workout_id = (await client.post("/workouts/active")).json()["id"]
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]

    resp = await client.post(
        f"/workouts/{workout_id}/sets/voice",
        json={"exercise_id": exercise_id, "audio_data_url": _audio()},
    )
    assert resp.status_code == 201, resp.text

    path = f"POST /workouts/{workout_id}/sets/voice"
    (row,) = [r for r in await _ios_rows(fresh_db) if r["payload"] == path]
    assert row["content"] == "записал подход голосом: Жим лёжа · 🎤 голосовое: сто на восемь"


async def test_ios_voice_failure_still_lands_with_the_mark(fresh_db, client_factory, monkeypatch):
    """Провайдер упал — запрос 502, но голосовое в ленте видно, с «не распознал»."""
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "_get_audio_client", lambda: _provider(error=RuntimeError("down")))
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/voice", json={"audio_data_url": _audio()})
    assert resp.status_code == 502

    (row,) = [r for r in await _ios_rows(fresh_db) if r["payload"] == "POST /ai/voice"]
    assert row["content"] == "прислал голосовое: 🎤 голосовое (не распознал)"


async def test_ios_failed_request_without_voice_is_still_not_logged(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/workouts/999999/finish")
    assert resp.status_code >= 400
    assert [r for r in await _ios_rows(fresh_db) if "999999" in (r["payload"] or "")] == []


# ---------- без обрезки: запись, экран, stdout ----------


async def test_long_user_message_is_stored_and_shown_in_full(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TZ_OFFSET", 0)
    text = "колено ноет после приседа " * 100  # ~2600 символов — больше старых 1000 и 120
    msg = _voice_message()
    msg.voice = None
    msg.text = text
    msg.content_type = "text"
    await fresh_db.get_or_create_user(USER_ID, "tester")
    await activity_log.record_message(msg)

    (row,) = await fresh_db.list_user_events(USER_ID, limit=10)
    assert row["content"] == text.strip()
    line = admin._activity_line(row)
    assert text.strip() in line
    assert "…" not in line


async def test_coach_reply_is_stored_in_full_and_markdown_is_rendered(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TZ_OFFSET", 0)
    await fresh_db.get_or_create_user(USER_ID, "tester")
    reply = "Я бы **не переходил сразу** на такую схему. " + "Длинное объяснение. " * 300
    await activity_log.record_ai_reply(USER_ID, reply)

    (row,) = await fresh_db.list_user_events(USER_ID, limit=10)
    assert row["content"] == reply.strip()
    line = admin._activity_line(row)
    assert "<b>не переходил сразу</b>" in line
    assert "**" not in line
    assert line.count("Длинное объяснение.") == 300


def test_hard_cap_still_protects_the_db():
    assert len(activity_log.voice_content("а" * 10_000)) == activity_log.MAX_CONTENT_LEN


def test_stdout_line_is_full_and_one_line():
    text = "строка один\nстрока два " + "x" * 3000
    line = activity_log.process_log_line("tg", 1, text, "message", "ok")
    assert "\n" not in line
    assert "строка один\\nстрока два " + "x" * 3000 in line
    assert "…" not in line


# ---------- длинная страница уходит несколькими сообщениями ----------


def _callback(data: str) -> CallbackQuery:
    inner = MagicMock()
    inner.chat = SimpleNamespace(id=ADMIN_ID)
    inner.message_id = 1
    inner.text = "старый экран"
    inner.edit_text = AsyncMock(return_value=inner)
    inner.delete = AsyncMock()
    inner.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=ADMIN_ID, username="admin", language_code=None)
    callback.message = inner
    callback.data = data
    callback.answer = AsyncMock()
    ids = iter(range(100, 200))
    callback.bot = MagicMock()
    callback.bot.send_message = AsyncMock(side_effect=lambda *a, **k: SimpleNamespace(message_id=next(ids)))
    callback.bot.delete_message = AsyncMock()
    return callback


async def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID))


async def test_long_feed_page_is_split_without_losing_anything(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    await fresh_db.get_or_create_user(USER_ID, "tester")
    for i in range(10):
        await fresh_db.log_user_event(
            USER_ID, activity_log.KIND_AI_REPLY, f"ответ {i}: **жирно** <тег> & " + "слово " * 400
        )

    callback = _callback(f"admin:acu:{USER_ID}")
    state = await _state()
    await admin.admin_activity_pick_user(callback, state)

    calls = callback.bot.send_message.await_args_list
    assert len(calls) > 1
    chunks = [c.args[1] for c in calls]
    for chunk in chunks:
        assert formatting.telegram_length(chunk) <= formatting.MESSAGE_LIMIT
        assert ui._open_tag_stack(chunk, []) == [], "каждый кусок закрывает свои теги"
    whole = "".join(formatting.strip_tags(c) for c in chunks)
    for i in range(10):
        assert f"ответ {i}: жирно &lt;тег&gt; &amp; " in whole
    assert whole.count("слово") == 4000
    assert all(c.kwargs.get("parse_mode") == "HTML" for c in calls)
    # Кнопки — только под последним куском.
    assert calls[-1].kwargs.get("reply_markup") is not None
    assert all(c.kwargs.get("reply_markup") is None for c in calls[:-1])

    # Листаем дальше — куски прошлой страницы убираются.
    next_callback = _callback(f"admin:acu:{USER_ID}")
    await admin.admin_activity_pick_user(next_callback, state)
    deleted = {c.args[1] for c in next_callback.bot.delete_message.await_args_list}
    assert deleted == {100 + i for i in range(len(calls) - 1)}


async def test_all_users_feed_shows_full_texts_with_author(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    await fresh_db.get_or_create_user(USER_ID, "tester")
    long_text = "вопрос про тестостерон " * 50
    await fresh_db.log_user_event(USER_ID, activity_log.KIND_MESSAGE, long_text.strip())

    callback = _callback("admin:aca:0")
    await admin.admin_activity_all(callback, await _state())

    shown = callback.message.edit_text.await_args or callback.message.answer.await_args
    text = shown.args[0] if shown else callback.bot.send_message.await_args.args[1]
    assert long_text.strip() in text
    assert "— <b>@tester</b>" in text


def test_split_html_reopens_tags_across_chunks():
    text = "<b>" + "а" * 50 + "\n" + "б" * 50 + "</b>"
    chunks = ui.split_html(text, 60)
    assert chunks == ["<b>" + "а" * 50 + "</b>", "<b>" + "б" * 50 + "</b>"]


def test_split_html_never_cuts_inside_an_entity():
    text = "&amp;" * 30
    for chunk in ui.split_html(text, 7):
        assert formatting.strip_tags(chunk).replace("&amp;", "") == ""
