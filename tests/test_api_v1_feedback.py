"""REST `/v1` для обратной связи (`/feedback`) и фактчека постов (`/factcheck`).

Маршруты ещё не подключены в api_v1.routes (см. докстринг api_v1_feedback.py
и задачу — их вплетает другой агент), поэтому здесь, в отличие от соседних
tests/test_api_v1_*.py, приложение собирается вручную поверх
`api_v1_feedback.routes` тем же способом, что и `api_v1.build_app()`. Это
временный костыль ровно на время, пока маршруты не влиты в общий файл.
"""

import base64

import httpx
import pytest
from starlette.applications import Starlette

import ai_limits
import ai_trainer
import api_v1_common as common
import api_v1_feedback
import config
from handlers import ai_trainer as ai_trainer_handlers

ApiError = common.ApiError


def _build_app() -> Starlette:
    # Временная сборка — см. докстринг модуля. Тот же набор exception_handlers,
    # что и в api_v1.build_app(), иначе ApiError долетал бы до клиента как 500.
    return Starlette(
        routes=api_v1_feedback.routes,
        exception_handlers={
            ApiError: common.api_error_handler,
            Exception: common.unhandled_error_handler,
        },
    )


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=_build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111):
    """Токен без /auth/link (его маршрут не входит в этот временный app):
    выпускаем напрямую через db, как это делает api_v1.auth_link внутри."""
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    token = await fresh_db.issue_api_token(telegram_id)
    client = client_factory()
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.fixture(autouse=True)
def _reset_feedback_state():
    """Антиспам-счётчик — модульный словарь в памяти процесса; тесты не должны
    делить его состояние друг с другом."""
    api_v1_feedback._daily_counts.clear()
    yield
    api_v1_feedback._daily_counts.clear()


# ---------- POST /feedback ----------


@pytest.mark.asyncio
async def test_feedback_requires_auth(client_factory):
    client = client_factory()
    resp = await client.post("/feedback", json={"text": "привет"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_feedback_requires_admin_configured(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/feedback", json={"text": "привет"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "not_configured"


@pytest.mark.asyncio
async def test_feedback_rejects_empty_text(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 999)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/feedback", json={"text": "   "})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


@pytest.mark.asyncio
async def test_feedback_rejects_missing_text_field(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 999)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/feedback", json={})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_feedback_rejects_too_long_text(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 999)
    client = await _linked_client(fresh_db, client_factory)

    too_long = "a" * (api_v1_feedback.MAX_FEEDBACK_LENGTH + 1)
    resp = await client.post("/feedback", json={"text": too_long})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


@pytest.mark.asyncio
async def test_feedback_sends_message_to_admin(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 999)
    sent = []

    class FakeBot:
        def __init__(self, token):
            self.session = self

        async def send_message(self, chat_id, text):
            sent.append((chat_id, text))

        async def close(self):
            pass

    monkeypatch.setattr("aiogram.Bot", FakeBot)
    client = await _linked_client(fresh_db, client_factory, telegram_id=111)

    resp = await client.post("/feedback", json={"text": "тут баг с подходами"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["delivered"] is True

    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == 999
    # Видно, от кого и что это из приложения, а не из бота — иначе на отзыв
    # нельзя ответить и непонятен контекст (см. докстринг api_v1_feedback.py).
    assert "111" in text
    assert "приложени" in text.lower()
    assert "тут баг с подходами" in text


@pytest.mark.asyncio
async def test_feedback_delivery_failure_returns_503_and_does_not_count(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 999)

    class BoomBot:
        def __init__(self, token):
            self.session = self

        async def send_message(self, chat_id, text):
            raise RuntimeError("telegram is down")

        async def close(self):
            pass

    monkeypatch.setattr("aiogram.Bot", BoomBot)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/feedback", json={"text": "привет"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "delivery_failed"
    # Неудачная попытка не должна стоить человеку места в суточной квоте.
    assert api_v1_feedback._feedback_quota_left(111) is True


@pytest.mark.asyncio
async def test_feedback_respects_daily_limit(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 999)

    class FakeBot:
        def __init__(self, token):
            self.session = self

        async def send_message(self, chat_id, text):
            pass

        async def close(self):
            pass

    monkeypatch.setattr("aiogram.Bot", FakeBot)
    client = await _linked_client(fresh_db, client_factory)

    for _ in range(api_v1_feedback.FEEDBACK_DAILY_LIMIT):
        resp = await client.post("/feedback", json={"text": "ещё один отзыв"})
        assert resp.status_code == 201

    resp = await client.post("/feedback", json={"text": "ещё один отзыв"})
    assert resp.status_code == 429
    assert resp.json()["error"] == "feedback_limit_exceeded"


def _image_data_url(mime="image/jpeg", payload=b"not-really-a-jpeg-but-fine-its-mocked"):
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


@pytest.mark.asyncio
async def test_feedback_with_photo_sends_text_then_photo(fresh_db, client_factory, monkeypatch):
    """Фото к отзыву — текст и фото двумя разными вызовами (см. докстринг
    api_v1_feedback._send_feedback_to_admin про CAPTION_LIMIT)."""
    monkeypatch.setattr(config, "ADMIN_ID", 999)
    payload = b"\xff\xd8\xff-jpeg-ish-bytes-for-the-test"
    sent_messages = []
    sent_photos = []

    class FakeBot:
        def __init__(self, token):
            self.session = self

        async def send_message(self, chat_id, text):
            sent_messages.append((chat_id, text))

        async def send_photo(self, chat_id, photo):
            sent_photos.append((chat_id, photo))

        async def close(self):
            pass

    monkeypatch.setattr("aiogram.Bot", FakeBot)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post(
        "/feedback",
        json={"text": "вот скрин бага", "image_data_url": _image_data_url(payload=payload)},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["delivered"] is True

    assert len(sent_messages) == 1
    assert sent_messages[0][0] == 999
    assert "вот скрин бага" in sent_messages[0][1]

    assert len(sent_photos) == 1
    chat_id, photo_file = sent_photos[0]
    assert chat_id == 999
    assert photo_file.data == payload


@pytest.mark.asyncio
async def test_feedback_without_photo_does_not_send_photo(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 999)
    sent_photos = []

    class FakeBot:
        def __init__(self, token):
            self.session = self

        async def send_message(self, chat_id, text):
            pass

        async def send_photo(self, chat_id, photo):
            sent_photos.append((chat_id, photo))

        async def close(self):
            pass

    monkeypatch.setattr("aiogram.Bot", FakeBot)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/feedback", json={"text": "без фото"})
    assert resp.status_code == 201, resp.text
    assert sent_photos == []


@pytest.mark.asyncio
async def test_feedback_rejects_too_big_photo(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 999)
    monkeypatch.setattr(ai_trainer_handlers, "MAX_IMAGE_BYTES", 4)
    monkeypatch.setattr(api_v1_feedback, "MAX_IMAGE_BYTES", 4)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post(
        "/feedback", json={"text": "фото слишком большое", "image_data_url": _image_data_url()}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "photo_too_big"
    # Отказ на слишком большое фото не должен тратить суточную квоту отзывов.
    assert api_v1_feedback._feedback_quota_left(111) is True


@pytest.mark.asyncio
async def test_feedback_rejects_unsupported_photo_format(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 999)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post(
        "/feedback",
        json={"text": "странный формат", "image_data_url": _image_data_url(mime="application/pdf")},
    )
    assert resp.status_code == 415
    assert resp.json()["error"] == "unsupported_media_type"


# ---------- POST /factcheck ----------


@pytest.mark.asyncio
async def test_factcheck_requires_auth(client_factory):
    client = client_factory()
    resp = await client.post("/factcheck", json={"text": "какой-то пост из канала про креатин"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_factcheck_requires_configuration(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/factcheck", json={"text": "пост"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "not_configured"


@pytest.mark.asyncio
async def test_factcheck_rejects_empty_body(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/factcheck", json={})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


@pytest.mark.asyncio
async def test_factcheck_accepts_image_without_text(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fake_fact_check_post(user_id, post_text, image_data_url=None):
        assert post_text == ""
        assert image_data_url == "data:image/png;base64,abc"
        return "Разбор по картинке: похоже на правду."

    monkeypatch.setattr(ai_trainer, "fact_check_post", fake_fact_check_post)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/factcheck", json={"image_data_url": "data:image/png;base64,abc"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["verdict"] == "Разбор по картинке: похоже на правду."


@pytest.mark.asyncio
async def test_factcheck_returns_verdict_and_charges_question_quota(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fake_fact_check_post(user_id, post_text, image_data_url=None):
        assert post_text == "семаглутид сжигает жир без диеты, ученые скрывают"
        assert image_data_url is None
        return "Дело: механизм похудения. Бред: «без диеты» и заговор."

    monkeypatch.setattr(ai_trainer, "fact_check_post", fake_fact_check_post)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post(
        "/factcheck",
        json={"text": "семаглутид сжигает жир без диеты, ученые скрывают"},
    )
    assert resp.status_code == 200, resp.text
    assert "Бред" in resp.json()["verdict"]

    # Та же квота, что у /ai/ask — форвард в боте её и тратит (см. докстринг
    # handlers.factcheck.factcheck_forward).
    assert await fresh_db.get_ai_question_count_today(111) == 1


@pytest.mark.asyncio
async def test_factcheck_does_not_charge_quota_on_model_failure(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def boom(user_id, post_text, image_data_url=None):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(ai_trainer, "fact_check_post", boom)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/factcheck", json={"text": "какой-то пост"})
    assert resp.status_code == 502
    assert resp.json()["error"] == "factcheck_failed"
    assert await fresh_db.get_ai_question_count_today(111) == 0


@pytest.mark.asyncio
async def test_factcheck_times_out_without_hanging(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(config, "AI_TOTAL_ANSWER_SECONDS", 0.01)

    async def hangs_forever(user_id, post_text, image_data_url=None):
        import asyncio

        await asyncio.sleep(10)
        return "никогда не дойдёт"

    monkeypatch.setattr(ai_trainer, "fact_check_post", hangs_forever)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/factcheck", json={"text": "долгий пост"})
    assert resp.status_code == 504
    assert resp.json()["error"] == "timeout"
    assert await fresh_db.get_ai_question_count_today(111) == 0


@pytest.mark.asyncio
async def test_factcheck_respects_daily_question_limit(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT", 1)
    ai_limits.reset_cache()

    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.try_increment_ai_question_count(111, 1)

    resp = await client.post("/factcheck", json={"text": "ещё один пост"})
    assert resp.status_code == 429
    assert resp.json()["error"] == "question_limit_exceeded"
