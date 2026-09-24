"""Согласие на передачу данных стороннему AI в приложении (App Store 5.1.2(i)).

Что держат эти тесты:

- `GET /settings` отдаёт `ai_consent`/`ai_consent_at`, `PATCH` ставит метку
  времени на `true`, снимает на `false` и не двигает её повторным `true`;
- ручки, которые реально шлют данные модели (`/ai/ask`, `/ai/questions/answer`,
  `/ai/voice`, `/ai/video`), без согласия отвечают 403 `ai_consent_required`
  с человеческим текстом — но только клиенту, который сам показывает лист
  (заголовок `X-AI-Consent-Flow: 1`) или при включённом
  `config.AI_CONSENT_REQUIRED`;
- уже выпущенные сборки (без заголовка) при выключенном флаге работают как
  раньше — ради них флаг и заведён.
"""

import httpx
import pytest

import ai_trainer
import api_v1
import config

HEADER = {config.AI_CONSENT_CLIENT_HEADER: "1"}


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111, lang="ru"):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    await fresh_db.set_user_lang(telegram_id, lang)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


@pytest.fixture
def fake_model(monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    calls: list[str] = []

    async def fake_ask(user_id, question, history, **kwargs):
        calls.append(question)
        return "Ответ тренера"

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    return calls


# ---------- /settings ----------


@pytest.mark.asyncio
async def test_new_account_has_no_consent(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    body = (await client.get("/settings")).json()
    assert body["ai_consent"] is False
    assert body["ai_consent_at"] is None


@pytest.mark.asyncio
async def test_patch_consent_sets_and_clears_timestamp(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.patch("/settings", json={"ai_consent": True})
    assert resp.status_code == 200, resp.text
    first = resp.json()
    assert first["ai_consent"] is True
    assert first["ai_consent_at"]

    # Повторное «Согласен» не двигает метку: важен первый момент согласия.
    again = (await client.patch("/settings", json={"ai_consent": True})).json()
    assert again["ai_consent_at"] == first["ai_consent_at"]

    revoked = (await client.patch("/settings", json={"ai_consent": False})).json()
    assert revoked["ai_consent"] is False
    assert revoked["ai_consent_at"] is None
    assert (await fresh_db.get_user(111))["ai_consent_at"] is None


@pytest.mark.asyncio
async def test_patch_consent_rejects_non_bool(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.patch("/settings", json={"ai_consent": "yes", "lang": "en"})
    assert resp.status_code == 400
    # Невалидное поле — ни одна колонка не тронута, включая соседний lang.
    body = (await client.get("/settings")).json()
    assert body["ai_consent"] is False
    assert body["lang"] == "ru"


# ---------- защита ручек тренера ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path, payload",
    [
        ("/ai/ask", {"question": "Как мой прогресс?"}),
        ("/ai/questions/answer", {"question_index": 0, "answer": "3 дня"}),
        ("/ai/voice", {"audio_data_url": "data:audio/m4a;base64,AAAA", "duration_seconds": 2}),
        ("/ai/video", {"video_data_url": "data:video/mp4;base64,AAAA"}),
    ],
)
async def test_consent_aware_client_gets_403_without_consent(
    fresh_db, client_factory, fake_model, path, payload
):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post(path, json=payload, headers=HEADER)
    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert body["error"] == "ai_consent_required"
    assert "Согласен" in body["message"]
    # До модели дело не дошло.
    assert fake_model == []


@pytest.mark.asyncio
async def test_consent_required_message_is_in_account_language(fresh_db, client_factory, fake_model):
    client = await _linked_client(fresh_db, client_factory, lang="en")
    resp = await client.post("/ai/ask", json={"question": "How am I doing?"}, headers=HEADER)
    assert resp.status_code == 403
    assert "I agree" in resp.json()["message"]


@pytest.mark.asyncio
async def test_consent_aware_client_passes_after_consent(fresh_db, client_factory, fake_model):
    client = await _linked_client(fresh_db, client_factory)
    await client.patch("/settings", json={"ai_consent": True})
    resp = await client.post("/ai/ask", json={"question": "Как мой прогресс?"}, headers=HEADER)
    assert resp.status_code == 200, resp.text
    assert fake_model == ["Как мой прогресс?"]


@pytest.mark.asyncio
async def test_revoked_consent_blocks_again(fresh_db, client_factory, fake_model):
    client = await _linked_client(fresh_db, client_factory)
    await client.patch("/settings", json={"ai_consent": True})
    await client.patch("/settings", json={"ai_consent": False})
    resp = await client.post("/ai/ask", json={"question": "Как мой прогресс?"}, headers=HEADER)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_released_builds_without_header_keep_working(fresh_db, client_factory, fake_model, monkeypatch):
    """1.0 (3)/(4) листа не знают и заголовка не шлют — пока флаг выключен,
    тренер у них отвечает как раньше."""
    monkeypatch.setattr(config, "AI_CONSENT_REQUIRED", False)
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/ai/ask", json={"question": "Как мой прогресс?"})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_global_flag_enforces_for_every_client(fresh_db, client_factory, fake_model, monkeypatch):
    monkeypatch.setattr(config, "AI_CONSENT_REQUIRED", True)
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/ai/ask", json={"question": "Как мой прогресс?"})
    assert resp.status_code == 403
    assert resp.json()["error"] == "ai_consent_required"


@pytest.mark.asyncio
async def test_reading_endpoints_are_not_gated(fresh_db, client_factory, fake_model, monkeypatch):
    """История, лимиты и черновик ничего модели не шлют — закрывать их незачем."""
    monkeypatch.setattr(config, "AI_CONSENT_REQUIRED", True)
    client = await _linked_client(fresh_db, client_factory)
    for path in ("/ai/limits", "/ai/history", "/ai/pending"):
        resp = await client.get(path, headers=HEADER)
        assert resp.status_code == 200, (path, resp.text)
