"""REST `/v1` для AI-тренера: дневная квота вопросов (`/ai/limits`) и сам
вопрос-ответ (`/ai/ask`).

Гоняется тем же приёмом, что и tests/test_api_v1.py и tests/test_api_v1_food.py
— httpx поверх ASGI, без сокета, включая настоящую проверку Bearer-токена.
Модель не вызывается по-настоящему нигде: `ai_trainer.ask` подменяется целиком
(как analyze_food в test_api_v1_food.py), поскольку она и есть публичная точка
входа `/ai/ask` — мокировать нужно именно её, а не HTTP-клиент x.ai глубже.
"""

import httpx
import pytest

import ai_limits
import ai_trainer
import api_v1
import config


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------- GET /ai/limits ----------


@pytest.mark.asyncio
async def test_limits_requires_auth(client_factory):
    client = client_factory()
    resp = await client.get("/ai/limits")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_limits_fresh_user_has_full_quota(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.get("/ai/limits")
    assert resp.status_code == 200
    body = resp.json()
    assert body["question"]["used"] == 0
    assert body["question"]["limit"] == config.AI_QUESTION_DAILY_LIMIT
    assert body["question"]["remaining"] == config.AI_QUESTION_DAILY_LIMIT
    assert body["blocked"] is False
    assert body["block_reason"] is None
    assert body["configured"] is True


@pytest.mark.asyncio
async def test_limits_reports_exhausted_quota(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT", 1)
    ai_limits.reset_cache()
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.try_increment_ai_question_count(111, 1)

    resp = await client.get("/ai/limits")
    assert resp.status_code == 200
    body = resp.json()
    assert body["question"]["used"] == 1
    assert body["question"]["remaining"] == 0
    assert body["blocked"] is True
    assert body["block_reason"] == ai_limits.KIND_QUESTION


@pytest.mark.asyncio
async def test_limits_zero_config_means_unlimited(fresh_db, client_factory, monkeypatch):
    """limit <= 0 в конфиге значит «лимита нет» (см. ai_limits._exhausted) —
    отдаём тем же значением клиенту, а не отдельным флагом unlimited."""
    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT", 0)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.get("/ai/limits")
    body = resp.json()
    assert body["question"]["limit"] is None
    assert body["question"]["remaining"] is None
    assert body["blocked"] is False


# ---------- POST /ai/ask ----------


@pytest.mark.asyncio
async def test_ask_requires_auth(client_factory):
    client = client_factory()
    resp = await client.post("/ai/ask", json={"question": "как дела?"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ask_requires_configuration(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/ai/ask", json={"question": "как дела?"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "not_configured"


@pytest.mark.asyncio
async def test_ask_rejects_empty_question(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/ask", json={"question": "   "})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


@pytest.mark.asyncio
async def test_ask_rejects_missing_question_field(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/ask", json={})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_ask_rejects_too_long_question(fresh_db, client_factory, monkeypatch):
    import api_v1_ai

    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    too_long = "a" * (api_v1_ai.MAX_QUESTION_LENGTH + 1)
    resp = await client.post("/ai/ask", json={"question": too_long})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


@pytest.mark.asyncio
async def test_ask_returns_model_answer_and_charges_quota(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fake_ask(user_id, question, history, **kwargs):
        assert question == "Как мой прогресс?"
        assert history == []
        # Колбэки под инлайн-кнопки бота (программа/действие/опросник/стрим) не
        # подключены с HTTP-стороны — их сюда никто не передаёт.
        assert kwargs == {}
        return "Ты молодец, продолжай в том же духе!"

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)

    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/ai/ask", json={"question": "Как мой прогресс?"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == "Ты молодец, продолжай в том же духе!"
    assert body["limits"]["question"]["used"] == 1
    assert body["limits"]["question"]["remaining"] == config.AI_QUESTION_DAILY_LIMIT - 1

    # Квота реально списана в БД, а не только в ответе.
    assert await fresh_db.get_ai_question_count_today(111) == 1


@pytest.mark.asyncio
async def test_ask_does_not_charge_quota_on_provider_failure(fresh_db, client_factory, monkeypatch):
    """Сорвавшийся у провайдера запрос не должен стоить человеку вопроса —
    тот же приём, что в handlers/ai_trainer.py и api_v1_food.parse_food."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def boom(user_id, question, history, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(ai_trainer, "ask", boom)

    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/ai/ask", json={"question": "Как мой прогресс?"})
    assert resp.status_code == 502
    assert resp.json()["error"] == "answer_failed"
    assert await fresh_db.get_ai_question_count_today(111) == 0


@pytest.mark.asyncio
async def test_ask_respects_daily_limit(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    # 0 отключает лимит вовсе (limit > 0 в ai_limits._exhausted) — нужен именно
    # уже выбранный лимит 1, а не выключенный.
    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT", 1)
    ai_limits.reset_cache()

    async def fake_ask(user_id, question, history, **kwargs):
        return "ответ"

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)

    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.try_increment_ai_question_count(111, 1)

    resp = await client.post("/ai/ask", json={"question": "ещё один вопрос"})
    assert resp.status_code == 429
    assert resp.json()["error"] == "question_limit_exceeded"


@pytest.mark.asyncio
async def test_ask_times_out_without_hanging(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(config, "AI_TOTAL_ANSWER_SECONDS", 0.01)

    async def hangs_forever(user_id, question, history, **kwargs):
        import asyncio

        await asyncio.sleep(10)
        return "никогда не дойдёт"

    monkeypatch.setattr(ai_trainer, "ask", hangs_forever)

    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/ai/ask", json={"question": "долгий вопрос"})
    assert resp.status_code == 504
    assert resp.json()["error"] == "timeout"
    # Таймаут — не ответ, счётчик не движется.
    assert await fresh_db.get_ai_question_count_today(111) == 0
