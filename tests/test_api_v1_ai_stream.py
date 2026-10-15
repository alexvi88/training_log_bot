"""`POST /ai/ask` со `"stream": true` — ответ тренера Server-Sent Events.

Тот же ход, что без стрима (лимит, квота, история), только текст уезжает по
мере генерации событиями `chunk`, а итог — событием `done` с телом обычного
ответа. Модель подменяется целиком, как в tests/test_api_v1_ai.py.
"""

import asyncio
import json

import httpx
import pytest

import ai_limits
import ai_trainer
import api_v1
import api_v1_ai
import config
import i18n


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111, lang=None):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    if lang is not None:
        await fresh_db.set_user_lang(telegram_id, lang)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


def _events(text: str) -> list[tuple[str, object]]:
    """Разбор SSE: (имя события, data как JSON); комментарии — ("ping", None)."""
    events: list[tuple[str, object]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        if block.startswith(":"):
            events.append(("ping", None))
            continue
        name = data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        events.append((name, data))
    return events


@pytest.mark.asyncio
async def test_stream_sends_chunks_then_done_and_charges_quota(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fake_ask(user_id, question, history, on_chunk=None, **kwargs):
        assert on_chunk is not None
        await on_chunk("Смотрю на твои")
        await on_chunk("Смотрю на твои тренировки: жим растёт.")
        return "Смотрю на твои тренировки: жим растёт. Так держать!"

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/ask", json={"question": "Как прогресс?", "stream": True})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _events(resp.text)
    names = [name for name, _ in events if name != "ping"]
    assert names == ["chunk", "chunk", "done"]
    assert events[0][1] == {"text": "Смотрю на твои"}
    done = events[-1][1]
    assert done["answer"] == "Смотрю на твои тренировки: жим растёт. Так держать!"
    assert set(done) >= {"answer", "program", "questions", "actions", "mentions", "limits"}
    assert done["limits"]["question"]["used"] == 1
    assert await fresh_db.get_ai_question_count_today(111) == 1

    # Бронь снята: следующий вопрос проходит, а не получает 429 busy.
    again = await client.post("/ai/ask", json={"question": "А присед?", "stream": True})
    assert again.status_code == 200, again.text
    assert _events(again.text)[-1][0] == "done"


@pytest.mark.asyncio
async def test_stream_without_flag_keeps_plain_json(fresh_db, client_factory, monkeypatch):
    """Без `stream` — прежний JSON и прежний набор колбэков: on_chunk не
    подключается, иначе старые клиенты платили бы стримом ни за что."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fake_ask(user_id, question, history, **kwargs):
        assert "on_chunk" not in kwargs
        assert "on_status" not in kwargs
        return "ответ"

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/ai/ask", json={"question": "вопрос"})
    assert resp.status_code == 200
    assert resp.json()["answer"] == "ответ"


@pytest.mark.asyncio
async def test_stream_provider_failure_is_an_error_event_and_free(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def boom(user_id, question, history, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(ai_trainer, "ask", boom)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/ask", json={"question": "вопрос", "stream": True})
    assert resp.status_code == 200
    events = [e for e in _events(resp.text) if e[0] != "ping"]
    assert [name for name, _ in events] == ["error"]
    error = events[0][1]
    assert error["status"] == 502
    assert error["error"] == "answer_failed"
    assert error["message"]
    assert await fresh_db.get_ai_question_count_today(111) == 0

    async def fine(user_id, question, history, **kwargs):
        return "ответ"

    monkeypatch.setattr(ai_trainer, "ask", fine)
    again = await client.post("/ai/ask", json={"question": "ещё раз"})
    assert again.status_code == 200, again.text


@pytest.mark.asyncio
async def test_stream_error_message_speaks_athletes_language(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def boom(user_id, question, history, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(ai_trainer, "ask", boom)
    client = await _linked_client(fresh_db, client_factory, lang="en")

    resp = await client.post("/ai/ask", json={"question": "progress?", "stream": True})
    error = [data for name, data in _events(resp.text) if name == "error"][0]
    assert not any("а" <= ch.lower() <= "я" for ch in error["message"]), error["message"]


@pytest.mark.asyncio
async def test_stream_limit_is_a_plain_429(fresh_db, client_factory, monkeypatch):
    """Лимит проверяется до первого байта стрима — приходит обычным 429."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT", 1)
    ai_limits.reset_cache()

    async def fake_ask(user_id, question, history, **kwargs):
        raise AssertionError("модель не должна вызываться сверх лимита")

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.try_increment_ai_question_count(111, 1)

    resp = await client.post("/ai/ask", json={"question": "ещё", "stream": True})
    assert resp.status_code == 429
    assert resp.json()["error"] == "question_limit_exceeded"

    # Бронь после отказа снята — 429 busy следующему запросу не грозит.
    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT", 100)
    ai_limits.reset_cache()

    async def fine(user_id, question, history, **kwargs):
        return "ответ"

    monkeypatch.setattr(ai_trainer, "ask", fine)
    again = await client.post("/ai/ask", json={"question": "теперь можно"})
    assert again.status_code == 200, again.text


@pytest.mark.asyncio
async def test_stream_sends_heartbeat_while_model_is_silent(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(api_v1_ai, "STREAM_HEARTBEAT_SECONDS", 0.05)

    async def slow_ask(user_id, question, history, **kwargs):
        await asyncio.sleep(0.2)
        return "ответ"

    monkeypatch.setattr(ai_trainer, "ask", slow_ask)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/ask", json={"question": "вопрос", "stream": True})
    events = _events(resp.text)
    assert ("ping", None) in events
    assert events[-1][0] == "done"


@pytest.mark.asyncio
async def test_stream_fresh_start_seed_ignores_exhausted_limit(fresh_db, client_factory, monkeypatch):
    """Бесплатный старт «собери программу» лимит не запирает — и в стриме тоже."""
    import ai_setup_flow

    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT", 1)
    ai_limits.reset_cache()
    monkeypatch.setattr(ai_setup_flow, "is_build_program_seed", lambda text: True)

    async def fresh(user_id):
        return True

    monkeypatch.setattr(ai_setup_flow, "is_fresh_start", fresh)

    async def fake_ask(user_id, question, history, **kwargs):
        raise AssertionError("бесплатный старт идёт без модели")

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.try_increment_ai_question_count(111, 1)

    resp = await client.post("/ai/ask", json={"question": "Собери мне программу", "stream": True})
    assert resp.status_code == 200, resp.text
    assert [name for name, _ in _events(resp.text) if name != "ping"][-1] == "done"


@pytest.mark.asyncio
async def test_stream_limit_check_failure_releases_busy(fresh_db, client_factory, monkeypatch):
    """Упала проверка лимита — бронь снята, следующий вопрос не получает 429 busy."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    real_check = ai_limits.check

    async def broken_check(user_id, kind):
        raise RuntimeError("db down")

    monkeypatch.setattr(ai_limits, "check", broken_check)
    with pytest.raises(RuntimeError):
        await client.post("/ai/ask", json={"question": "вопрос", "stream": True})
    assert not api_v1_ai._busy

    monkeypatch.setattr(ai_limits, "check", real_check)

    async def fine(user_id, question, history, **kwargs):
        return "ответ"

    monkeypatch.setattr(ai_trainer, "ask", fine)
    again = await client.post("/ai/ask", json={"question": "ещё", "stream": True})
    assert again.status_code == 200, again.text


@pytest.mark.asyncio
async def test_stream_sends_tool_status_between_chunks(fresh_db, client_factory, monkeypatch):
    """Живой случай: «I'll pull your training history and profile» — и тишина на
    минуту, пока модель читает историю и собирает программу. Между кусками
    текста стрим говорит, что тренер делает (`status`, ai.tool_status.* на языке
    атлета), — иначе экран выглядит зависшим. Квота — одна за весь ход."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    seen: dict[str, object] = {}

    async def fake_ask(user_id, question, history, on_chunk=None, on_status=None, **kwargs):
        assert on_chunk is not None and on_status is not None
        seen["lang"] = i18n.get_lang()
        await on_chunk("I'll pull your training history and profile")
        await on_status(ai_trainer._tool_status_variants("get_full_workout_history")[0])
        await on_status("")  # пустую строку не шлём вовсе
        await on_status(ai_trainer._tool_status_variants("propose_program")[0])
        await on_chunk("Here's your program.")
        return "Here's your program. Three days a week."

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked_client(fresh_db, client_factory, lang="en")

    resp = await client.post("/ai/ask", json={"question": "Build me a program", "stream": True})
    assert resp.status_code == 200, resp.text
    events = [(name, data) for name, data in _events(resp.text) if name != "ping"]
    assert [name for name, _ in events] == ["chunk", "status", "status", "chunk", "done"]
    assert seen["lang"] == "en"
    assert events[1][1] == {"text": i18n.t_in("en", "ai.tool_status.get_full_workout_history.1")}
    assert events[2][1] == {"text": i18n.t_in("en", "ai.tool_status.propose_program.1")}
    for _, data in events[1:3]:
        assert not any("\u0400" <= ch <= "\u04ff" for ch in data["text"]), data
    assert events[-1][1]["limits"]["question"]["used"] == 1
    assert await fresh_db.get_ai_question_count_today(111) == 1


@pytest.mark.asyncio
async def test_stream_status_speaks_russian_to_russian_athlete(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fake_ask(user_id, question, history, on_status=None, **kwargs):
        await on_status(ai_trainer._tool_status_variants("list_recent_workouts")[0])
        return "ответ"

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked_client(fresh_db, client_factory, lang="ru")
    resp = await client.post("/ai/ask", json={"question": "вопрос", "stream": True})
    status = [data for name, data in _events(resp.text) if name == "status"]
    assert status == [{"text": i18n.t_in("ru", "ai.tool_status.list_recent_workouts.1")}]


# ---------- последний ответ опросника — тоже стримом ----------


def _fake_ask_with_questions(*texts):
    async def fake_ask(user_id, question, history, on_questions=None, **kwargs):
        if on_questions is not None:
            await on_questions([{"question": text, "choices": []} for text in texts])
        return "Уточню пару вещей."

    return fake_ask


@pytest.mark.asyncio
async def test_last_setup_answer_streams_with_status_and_heartbeat(fresh_db, client_factory, monkeypatch):
    """Живой случай: последний ответ опросника — ход со сборкой программы на
    полторы минуты — шёл обычным JSON без единого байта до конца, соединение
    рвалось по дороге, и приложение показывало «Связи нет», хотя ход доезжал.
    Со `stream` он идёт как /ai/ask: пульс, статусы, `done` с телом ответа."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(api_v1_ai, "STREAM_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_with_questions("Сколько дней?", "Травмы?"))
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.update_user(111, goal="набор массы")
    await client.post("/ai/ask", json={"question": "Собери программу"})

    # Шаг до последнего — обычный JSON и со `stream`: модель не зовётся.
    step = await client.post("/ai/questions/answer", json={"question_index": 0, "answer": "3", "stream": True})
    assert step.status_code == 200, step.text
    assert step.headers["content-type"].startswith("application/json")
    assert step.json()["questions"]["index"] == 1

    async def slow_turn(user_id, question, history, on_chunk=None, on_status=None, **kwargs):
        assert on_chunk is not None and on_status is not None
        await on_status("Собираю программу")
        # Пока ход идёт — /ai/pending говорит «тренер работает» и не отдаёт
        # уже отвеченный вопрос с живыми вариантами.
        pending = (await client.get("/ai/pending")).json()
        assert pending["turn_in_flight"] is True
        assert pending["questions"] is None
        await asyncio.sleep(0.05)
        return "Готово, программа под сообщением."

    monkeypatch.setattr(ai_trainer, "ask", slow_turn)
    resp = await client.post("/ai/questions/answer", json={"question_index": 1, "answer": None, "stream": True})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _events(resp.text)
    names = [name for name, _ in events]
    assert "ping" in names and "status" in names
    assert names[-1] == "done"
    assert events[-1][1]["answer"] == "Готово, программа под сообщением."
    assert events[-1][1]["questions"] is None
    assert await fresh_db.get_ai_setup_state(111) is None

    # Бронь снята, опросник закрыт.
    pending = (await client.get("/ai/pending")).json()
    assert pending["turn_in_flight"] is False
    assert pending["questions"] is None


@pytest.mark.asyncio
async def test_last_setup_answer_stream_limit_is_a_plain_429(fresh_db, client_factory, monkeypatch):
    """Отказ по лимиту — до первого байта, обычным JSON; опросник цел, бронь снята."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_with_questions("Сколько дней?"))
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.update_user(111, goal="набор массы")
    await client.post("/ai/ask", json={"question": "Собери программу"})

    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT", 1)
    ai_limits.reset_cache()
    await fresh_db.try_increment_ai_question_count(111, 1)
    resp = await client.post("/ai/questions/answer", json={"question_index": 0, "answer": "3", "stream": True})
    assert resp.status_code == 429, resp.text
    assert resp.json()["error"] == "question_limit_exceeded"
    assert 111 not in api_v1_ai._busy
    assert await fresh_db.get_ai_setup_state(111) is not None
