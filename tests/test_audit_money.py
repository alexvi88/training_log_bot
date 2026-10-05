"""Находки аудита про деньги: платные вызовы без потолка, без учёта и без квоты.

Каждый тест падает без своего фикса: голос (HARD-стоп, суточная квота, длина,
цена по длительности), автокомментарий и дайджесты на HARD-стопе, квота видео за
состоявшийся вызов, цена оборванного вызова, расписка «Понятно» против HARD,
квота текстового импорта.
"""

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import ai_limits
import ai_trainer
import api_v1
import config
import db
import engagement
import text_import
import video_analysis
from handlers import ai_trainer as ai_handlers
from handlers import workout as workout_handlers


@pytest.fixture(autouse=True)
def _reset_limit_cache():
    ai_limits.reset_cache()
    yield
    ai_limits.reset_cache()


@pytest.fixture
def hard_stop(monkeypatch):
    """Расход за сутки уже за жёстким потолком."""
    async def level():
        return ai_limits.KIND_SPEND_HARD

    monkeypatch.setattr(ai_limits, "spend_level", level)
    monkeypatch.setattr(ai_limits, "daily_spend_usd", AsyncMock(return_value=99.0))


async def _events(event_type=None):
    cur = await db.conn().execute(
        "SELECT event_type, model, prompt_tokens, completion_tokens FROM cost_events ORDER BY id"
    )
    rows = await cur.fetchall()
    return [r for r in rows if event_type is None or r["event_type"] == event_type]


# ---------- 1. голос ----------


async def test_voice_is_priced_by_duration_not_flat(fresh_db, user_id):
    await db.log_cost_event(user_id, "transcription", model="m", audio_seconds=120)
    total = await db.get_cost_total_usd()
    assert total == pytest.approx(2 * config.TRANSCRIPTION_PRICE_USD_PER_MINUTE)
    assert total != pytest.approx(config.TRANSCRIPTION_PRICE_USD_PER_CALL)
    # Строка без длительности (старый лог) по-прежнему считается плоско.
    await db.log_cost_event(user_id, "transcription", model="m")
    assert await db.get_cost_total_usd() == pytest.approx(
        2 * config.TRANSCRIPTION_PRICE_USD_PER_MINUTE + config.TRANSCRIPTION_PRICE_USD_PER_CALL
    )
    assert await db.get_transcription_cost_usd(db._utc_day()) == pytest.approx(await db.get_cost_total_usd())


async def test_transcribe_voice_logs_duration_and_counts_attempt(fresh_db, user_id, monkeypatch):
    create = AsyncMock(return_value=SimpleNamespace(text=" привет "))
    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
    monkeypatch.setattr(ai_trainer, "_get_audio_client", lambda: client)

    out = await ai_trainer.transcribe_voice(SimpleNamespace(name="v.ogg"), user_id, duration_seconds=90)

    assert out == "привет"
    (event,) = await _events("transcription")
    assert event["prompt_tokens"] == 90  # секунды лежат в prompt_tokens
    assert await db.get_ai_usage_today(user_id, ai_limits.KIND_VOICE) == 1


async def test_voice_attempt_counts_even_when_provider_fails(fresh_db, user_id, monkeypatch):
    create = AsyncMock(side_effect=RuntimeError("down"))
    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
    monkeypatch.setattr(ai_trainer, "_get_audio_client", lambda: client)
    with pytest.raises(RuntimeError):
        await ai_trainer.transcribe_voice(SimpleNamespace(name="v.ogg"), user_id, duration_seconds=5)
    assert await db.get_ai_usage_today(user_id, ai_limits.KIND_VOICE) == 1


async def test_voice_daily_quota_blocks(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "AI_VOICE_DAILY_LIMIT", 2)
    assert await ai_limits.check(user_id, ai_limits.KIND_VOICE) is None
    await db.increment_ai_usage(user_id, ai_limits.KIND_VOICE)
    await db.increment_ai_usage(user_id, ai_limits.KIND_VOICE)
    block = await ai_limits.check(user_id, ai_limits.KIND_VOICE)
    assert block is not None and block.kind == ai_limits.KIND_VOICE
    assert "2" in block.user_text


async def test_voice_is_stopped_by_hard_stop(fresh_db, user_id, hard_stop):
    block = await ai_limits.check(user_id, ai_limits.KIND_VOICE)
    assert block is not None and block.kind == ai_limits.KIND_SPEND_HARD
    assert block.user_text == ai_limits._hard_stop_text()


async def test_soft_stop_does_not_switch_voice_off(fresh_db, user_id, monkeypatch):
    async def level():
        return ai_limits.KIND_SPEND_SOFT

    monkeypatch.setattr(ai_limits, "spend_level", level)
    assert await ai_limits.check(user_id, ai_limits.KIND_VOICE) is None


def _voice_message(user_id, duration=5):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, language_code=None)
    message.voice = SimpleNamespace(file_id="v", duration=duration, file_size=1000)
    message.reply = AsyncMock()
    message.bot = MagicMock()
    message.bot.download = AsyncMock(return_value=SimpleNamespace(name=""))
    return message


async def test_bot_set_voice_rejects_long_recording_before_download(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    transcribe = AsyncMock()
    monkeypatch.setattr(ai_trainer, "transcribe_voice", transcribe)
    message = _voice_message(user_id, duration=config.MAX_VOICE_SECONDS + 1)

    await workout_handlers.log_set_voice(message, MagicMock())

    message.bot.download.assert_not_awaited()
    transcribe.assert_not_awaited()
    message.reply.assert_awaited_once()


async def test_bot_set_voice_stops_on_hard_stop(fresh_db, user_id, hard_stop, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    transcribe = AsyncMock()
    monkeypatch.setattr(ai_trainer, "transcribe_voice", transcribe)
    message = _voice_message(user_id)

    await workout_handlers.log_set_voice(message, MagicMock())

    message.bot.download.assert_not_awaited()
    transcribe.assert_not_awaited()
    assert message.reply.await_args.args[0] == ai_limits._hard_stop_text()


async def test_bot_voice_question_stops_on_voice_quota(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "AI_VOICE_DAILY_LIMIT", 1)
    await db.increment_ai_usage(user_id, ai_limits.KIND_VOICE)
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    transcribe = AsyncMock()
    monkeypatch.setattr(ai_trainer, "transcribe_voice", transcribe)
    monkeypatch.setattr(ai_handlers, "ai_keyboard", AsyncMock(return_value=None))
    ai_handlers._busy.discard(user_id)
    message = _voice_message(user_id)

    await ai_handlers.ai_voice_question(message, MagicMock())

    transcribe.assert_not_awaited()
    message.bot.download.assert_not_awaited()
    assert "1" in message.reply.await_args.args[0]


def _client():
    transport = httpx.ASGITransport(app=api_v1.build_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _linked_client(telegram_id=111):
    await db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = _client()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


def _audio_url(payload=b"abc"):
    return f"data:audio/m4a;base64,{base64.b64encode(payload).decode()}"


async def test_api_voice_hard_stop_is_429_and_provider_untouched(fresh_db, hard_stop, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    transcribe = AsyncMock(return_value="x")
    monkeypatch.setattr(ai_trainer, "transcribe_voice", transcribe)
    client = await _linked_client()

    resp = await client.post("/ai/voice", json={"audio_data_url": _audio_url(), "duration_seconds": 3})

    assert resp.status_code == 429
    assert resp.json()["error"] == "spend_limit_exceeded"
    transcribe.assert_not_awaited()


async def test_api_voice_daily_quota_is_429(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "AI_VOICE_DAILY_LIMIT", 1)
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    transcribe = AsyncMock(return_value="x")
    monkeypatch.setattr(ai_trainer, "transcribe_voice", transcribe)
    client = await _linked_client()
    await db.increment_ai_usage(111, ai_limits.KIND_VOICE)

    resp = await client.post("/ai/voice", json={"audio_data_url": _audio_url(), "duration_seconds": 3})

    assert resp.status_code == 429
    assert resp.json()["error"] == "voice_limit_exceeded"
    assert resp.json()["message"]  # голосом тренера, на языке атлета
    transcribe.assert_not_awaited()


async def test_api_voice_without_duration_and_big_file_is_refused(fresh_db, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    transcribe = AsyncMock(return_value="x")
    monkeypatch.setattr(ai_trainer, "transcribe_voice", transcribe)
    client = await _linked_client()
    big = b"\0" * (config.VOICE_NO_DURATION_MAX_BYTES + 1)

    resp = await client.post("/ai/voice", json={"audio_data_url": _audio_url(big)})

    assert resp.status_code == 400
    assert resp.json()["error"] == "voice_too_long"
    transcribe.assert_not_awaited()


# ---------- 2. автокомментарий ----------


async def test_api_auto_comment_does_not_call_model_on_hard_stop(fresh_db, user_id, hard_stop, monkeypatch):
    comment = AsyncMock(return_value="разбор")
    monkeypatch.setattr(ai_trainer, "comment_on_workout", comment)
    workout_id = await db.create_finished_workout(user_id, "2026-01-01T10:00:00", "2026-01-01T11:00:00")

    assert await api_v1._write_ai_comment(user_id, workout_id) is None
    comment.assert_not_awaited()

    user = await db.get_user(user_id)
    workout = await db.get_workout(workout_id)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(api_v1.common, "ai_consent_given", lambda request, u: True)
    assert await api_v1._spawn_ai_comment(MagicMock(), user_id, workout_id, user, workout) is False


async def test_bot_auto_comment_does_not_call_model_on_hard_stop(fresh_db, user_id, hard_stop, monkeypatch):
    comment = AsyncMock(return_value="разбор")
    monkeypatch.setattr(ai_trainer, "comment_on_workout", comment)
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    workout_id = await db.create_finished_workout(user_id, "2026-01-01T10:00:00", "2026-01-01T11:00:00")

    await workout_handlers._attach_ai_comment(bot, 1, 2, user_id, workout_id, "карточка")

    comment.assert_not_awaited()
    bot.edit_message_text.assert_awaited_once()  # плейсхолдер «разбираю» снят


# ---------- 3. дайджесты ----------


async def test_behaviour_digest_is_silent_on_hard_stop(fresh_db, hard_stop, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    get_client = MagicMock()
    monkeypatch.setattr(ai_trainer, "_get_client", get_client)
    assert await ai_trainer.behaviour_digest("сводка") is None
    get_client.assert_not_called()


async def test_weekly_digest_loop_checks_hard_stop_every_iteration(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "AI_WEEKLY_DIGEST_ENABLED", True)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    digest = AsyncMock(return_value="текст")
    monkeypatch.setattr(ai_trainer, "weekly_digest", digest)
    states = iter([None, ai_limits.Block(kind="spend_hard", log="x", user_text="стоп")])
    monkeypatch.setattr(ai_limits, "hard_stop_block", AsyncMock(side_effect=lambda: next(states)))

    assert await engagement._ai_weekly_digest_text(1) == "текст"
    assert await engagement._ai_weekly_digest_text(2) is None  # потолок сработал посреди рассылки
    assert digest.await_count == 1


# ---------- 4. видео: квота за состоявшийся вызов ----------


def _novita_response(content):
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None,
                            completion_tokens_details=None)
    return SimpleNamespace(
        usage=usage, choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _novita_client(create):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


async def test_video_quota_counts_paid_call_even_when_answer_is_garbage(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "NOVITA_API_KEY", "k")
    create = AsyncMock(return_value=_novita_response("это не JSON"))
    monkeypatch.setattr(video_analysis, "_get_client", lambda: _novita_client(create))

    assert await video_analysis.analyze(b"bytes", user_id) is None

    assert await db.get_ai_video_count_today(user_id) == 1


async def test_video_quota_not_spent_when_provider_never_answered(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "NOVITA_API_KEY", "k")
    create = AsyncMock(side_effect=RuntimeError("network"))
    monkeypatch.setattr(video_analysis, "_get_client", lambda: _novita_client(create))

    assert await video_analysis.analyze(b"bytes", user_id) is None

    assert await db.get_ai_video_count_today(user_id) == 0


# ---------- 5. оборванные вызовы не бесплатны ----------


async def test_paid_call_timeout_logs_estimated_cost(fresh_db, user_id):
    async def slow():
        raise asyncio.TimeoutError

    messages = [{"role": "user", "content": "я" * 4000}]
    with pytest.raises(asyncio.TimeoutError):
        await ai_trainer.paid_call(user_id, None, slow, messages=messages)

    (event,) = await _events("llm_call")
    assert event["prompt_tokens"] == ai_trainer._estimate_tokens_from_messages(messages) > 900


async def test_paid_call_cancel_logs_fallback_estimate(fresh_db, user_id):
    started = asyncio.Event()

    async def hang():
        started.set()
        await asyncio.sleep(30)

    task = asyncio.ensure_future(ai_trainer.paid_call(user_id, None, hang))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    (event,) = await _events("llm_call")
    assert event["prompt_tokens"] == ai_trainer._ABORT_FALLBACK_PROMPT_TOKENS


async def test_fact_check_timeout_is_billed(fresh_db, user_id, monkeypatch):
    create = AsyncMock(side_effect=asyncio.TimeoutError)
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: _novita_client(create))
    with pytest.raises(asyncio.TimeoutError):
        await ai_trainer.fact_check_post(user_id, "пост " * 500)
    assert len(await _events("llm_call")) == 1


async def test_completion_round_timeout_is_billed(fresh_db, user_id):
    create = AsyncMock(side_effect=asyncio.TimeoutError)
    messages = [{"role": "user", "content": "вопрос " * 300}]
    with pytest.raises(asyncio.TimeoutError):
        await ai_trainer._completion_round(_novita_client(create), messages, user_id)
    (event,) = await _events("llm_call")
    assert event["prompt_tokens"] == ai_trainer._estimate_tokens_from_messages(messages)


async def test_web_search_timeout_logs_flat_estimate_and_tool_call(fresh_db, user_id, monkeypatch):
    class Session:
        async def sample(self):
            raise asyncio.TimeoutError

    sdk = SimpleNamespace(chat=SimpleNamespace(create=lambda **kw: Session()))
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=sdk))
    monkeypatch.setattr(ai_trainer, "_to_xai_messages", lambda *a, **k: [])

    assert await ai_trainer._web_search_findings(user_id, "что нового", []) is None

    (llm,) = await _events("llm_call")
    assert llm["prompt_tokens"] == config.SEARCH_ABORT_ESTIMATE_PROMPT_TOKENS
    assert len(await _events("server_tool")) == 1
    assert await db.get_cost_total_usd() > config.SERVER_TOOL_PRICE_USD_PER_CALL


# ---------- 6. «Понятно» не снимает HARD ----------


@pytest.mark.parametrize("kind", [ai_limits.KIND_FOOD, ai_limits.KIND_VIDEO, ai_limits.KIND_SEARCH])
async def test_ack_does_not_skip_hard_stop(fresh_db, user_id, hard_stop, monkeypatch, kind):
    monkeypatch.setattr(config, "limit_preview_ids", lambda: {user_id})
    await ai_limits.record_ack(user_id, kind)

    block = await ai_limits.check(user_id, kind)

    assert block is not None
    assert block.kind == kind and not block.preview
    assert block.log.startswith("spend_hard")


async def test_ack_still_skips_ordinary_quota_for_own_accounts(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "limit_preview_ids", lambda: {user_id})
    monkeypatch.setattr(config, "AI_VIDEO_DAILY_LIMIT", 1)
    await db.increment_ai_video_count(user_id)
    first = await ai_limits.check(user_id, ai_limits.KIND_VIDEO)
    assert first is not None and first.preview
    await ai_limits.record_ack(user_id, ai_limits.KIND_VIDEO)
    assert await ai_limits.check(user_id, ai_limits.KIND_VIDEO) is None


# ---------- 7. текстовый импорт ----------


async def test_text_import_has_daily_quota(fresh_db, user_id, monkeypatch):
    import datetime as dt

    monkeypatch.setattr(config, "AI_IMPORT_DAILY_LIMIT", 1)
    monkeypatch.setattr(text_import, "_extract_chunk", AsyncMock(return_value=([], 0, [])))

    await text_import.extract_sets(user_id, "заметки", dt.date(2026, 1, 1), lang="ru")
    with pytest.raises(ai_trainer.LimitBlocked) as err:
        await text_import.extract_sets(user_id, "заметки", dt.date(2026, 1, 1), lang="ru")

    assert err.value.block.kind == ai_limits.KIND_IMPORT


async def test_ai_name_matching_stops_when_import_quota_is_gone(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "AI_IMPORT_DAILY_LIMIT", 1)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"matches": []})))])
    create = AsyncMock(return_value=reply)
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: _novita_client(create))

    await ai_trainer.match_exercise_names_to_catalog(user_id, ["Bench"])
    assert create.await_count == 1
    assert await ai_trainer.match_exercise_names_to_catalog(user_id, ["Squat"]) == {}
    assert create.await_count == 1  # второй импорт за сутки до модели не дошёл


async def test_rest_text_convert_quota_is_429(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "AI_IMPORT_DAILY_LIMIT", 1)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(text_import, "_extract_chunk", AsyncMock(return_value=([], 0, [])))
    client = await _linked_client()
    await db.increment_ai_usage(111, ai_limits.KIND_IMPORT)

    resp = await client.post("/import/text/convert", json={"text": "12.01 жим 80 на 5"})

    assert resp.status_code == 429, resp.text
    assert resp.json()["error"] == "import_limit_exceeded"
    assert resp.json()["message"]
