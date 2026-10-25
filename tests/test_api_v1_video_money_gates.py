"""`POST /v1/ai/video` и фото в `/ai/ask` — отказы ДО платного вызова.

Разбор видео — самый дорогой шаг продукта (Qwen3-VL + ответ тренера).
Докстринг `ask_video` обещает: квоты video и question проверяются до единого
байта разбора, а режим «программа атлету» (чат тренера про клиента) видео и
фото не принимает вовсе. Покрыт был только отказ по квоте видео; отказ по
квоте вопросов (платный разбор не должен стартовать ради ответа, который
квота всё равно не пропустит) и отказ в режиме клиента — нет. Здесь
`video_analysis.analyze` и `ai_trainer.ask` подменены на ловушки: любой их
вызов — провал теста, то есть потраченные деньги.
"""

import base64

import httpx
import pytest

import ai_limits
import ai_trainer
import api_v1
import api_v1_ai
import config
import video_analysis

pytestmark = pytest.mark.asyncio

USER, CLIENT = 111, 222


def _video():
    return "data:video/mp4;base64," + base64.b64encode(b"fake-video").decode()


def _photo():
    return "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff fake jpeg").decode()


@pytest.fixture
def paid(monkeypatch):
    calls = []

    async def trap(*args, **kwargs):
        calls.append(args)
        raise AssertionError("платный вызов не должен был случиться")

    monkeypatch.setattr(config, "video_analysis_available", lambda: True)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(video_analysis, "analyze", trap)
    monkeypatch.setattr(ai_trainer, "ask", trap)
    ai_limits.reset_cache()
    yield calls
    ai_limits.reset_cache()


async def _client(db):
    await db.get_or_create_user(telegram_id=USER, username="tester")
    code = await db.issue_oauth_link_code(USER, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    token = (await client.post("/auth/link", json={"code": code})).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def test_exhausted_question_quota_stops_video_before_analysis(fresh_db, paid, monkeypatch):
    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT", 1)
    client = await _client(fresh_db)
    await fresh_db.increment_ai_question_count(USER)

    resp = await client.post("/ai/video", json={"video_data_url": _video()})

    assert resp.status_code == 429, resp.text
    assert resp.json()["error"] == "question_limit_exceeded"
    assert resp.json()["message"]  # человеку — текст лимита, а не пустая плашка
    assert paid == []
    assert await fresh_db.get_ai_video_count_today(USER) == 0
    # Бронь хода снята: человек не заперт до конца суток.
    assert USER not in api_v1_ai._busy


async def test_video_is_refused_in_client_program_mode(fresh_db, paid):
    client = await _client(fresh_db)
    await fresh_db.get_or_create_user(telegram_id=CLIENT, username="client")
    await fresh_db.start_coach_client_conversation(USER, CLIENT)

    resp = await client.post("/ai/video", json={"video_data_url": _video()})

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == "coach_client_mode"
    assert paid == []
    assert await fresh_db.get_ai_video_count_today(USER) == 0
    assert await fresh_db.get_ai_question_count_today(USER) == 0


async def test_photo_question_is_refused_in_client_program_mode(fresh_db, paid):
    client = await _client(fresh_db)
    await fresh_db.get_or_create_user(telegram_id=CLIENT, username="client")
    # У /ai/ask режим клиента живой только при настоящей связке и согласии
    # атлета на AI — иначе раньше сработает «not_linked».
    await fresh_db.create_coach_link(USER, CLIENT, "Клиент", show_bodyweight=False)
    await fresh_db.update_user(CLIENT, ai_consent_at="2026-10-01T00:00:00")
    await fresh_db.start_coach_client_conversation(USER, CLIENT)

    resp = await client.post("/ai/ask", json={"question": "что на фото?", "image_data_url": _photo()})

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == "coach_client_mode"
    assert paid == []
    assert await fresh_db.get_ai_question_count_today(USER) == 0


@pytest.mark.parametrize("duration", ["12", True, [12]])
async def test_non_numeric_duration_is_400_before_anything_paid(fresh_db, paid, duration):
    client = await _client(fresh_db)

    resp = await client.post("/ai/video", json={"video_data_url": _video(), "duration_seconds": duration})

    assert resp.status_code == 400, resp.text
    assert paid == []


async def test_video_without_configured_trainer_is_503(fresh_db, paid, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)
    client = await _client(fresh_db)

    resp = await client.post("/ai/video", json={"video_data_url": _video()})

    assert resp.status_code == 503
    assert paid == []
