"""Голос в `/v1`: `POST /ai/voice` (расшифровка вопроса тренеру) и
`POST /workouts/{id}/sets/voice` (подход голосом).

Провайдер транскрипции нигде не вызывается по-настоящему: `ai_trainer.transcribe_voice`
подменяется целиком — тем же приёмом, что `ai_trainer.ask`/`analyze_food` в
tests/test_api_v1_ai.py и tests/test_api_v1_food.py, — потому что она и есть
общая точка входа, которую делят бот и оба этих маршрута (см. api_v1_voice.py).
Гоняется через httpx поверх ASGI, без сокета, с настоящей проверкой Bearer-токена.
"""

import base64

import httpx
import pytest

import ai_trainer
import api_v1
import api_v1_voice


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


async def _active_workout_with_exercise(client, name="Жим лёжа"):
    resp = await client.post("/workouts/active")
    workout_id = resp.json()["id"]
    resp = await client.post("/exercises", json={"name": name})
    return workout_id, resp.json()["id"]


def _audio_data_url(mime="audio/m4a", payload=b"not-really-audio-but-fine-its-mocked"):
    """Голос с телефона — не OGG, как у Telegram, а M4A/AAC (см. докстринг
    api_v1_voice) — вот его и шлём по умолчанию."""
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


def _fake_transcribe(text):
    async def _fn(file_obj, user_id=None):
        # То же, что и в боте: имя файла несёт расширение, по которому
        # провайдер понимает формат (api_v1_voice декодирует data URL и
        # выставляет buf.name сам, до вызова этой функции).
        assert file_obj.name.startswith("voice.")
        return text

    return _fn


# ---------- POST /ai/voice ----------


@pytest.mark.asyncio
async def test_ai_voice_requires_auth(client_factory):
    client = client_factory()
    resp = await client.post("/ai/voice", json={"audio_data_url": _audio_data_url()})
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_ai_voice_returns_503_when_not_configured(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: False)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/voice", json={"audio_data_url": _audio_data_url()})
    assert resp.status_code == 503
    assert resp.json()["error"] == "not_configured"


@pytest.mark.asyncio
async def test_ai_voice_transcribes_successfully(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "transcribe_voice", _fake_transcribe("сколько подходов делать на массу?"))
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post(
        "/ai/voice", json={"audio_data_url": _audio_data_url(), "duration_seconds": 4}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"question": "сколько подходов делать на массу?"}


@pytest.mark.asyncio
async def test_ai_voice_rejects_missing_audio(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/voice", json={})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_ai_voice_rejects_unsupported_format(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post(
        "/ai/voice", json={"audio_data_url": _audio_data_url(mime="video/mp4")}
    )
    assert resp.status_code == 415
    assert resp.json()["error"] == "unsupported_media_type"


@pytest.mark.asyncio
async def test_ai_voice_rejects_too_long_duration(fresh_db, client_factory, monkeypatch):
    """Тот же лимит, что у бота (`handlers.ai_trainer.MAX_VOICE_SECONDS`), не
    новое число — но опущен здесь до 5с ради скорости теста."""
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(api_v1_voice, "MAX_VOICE_SECONDS", 5)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post(
        "/ai/voice", json={"audio_data_url": _audio_data_url(), "duration_seconds": 6}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "voice_too_long"


@pytest.mark.asyncio
async def test_ai_voice_rejects_too_big_payload(fresh_db, client_factory, monkeypatch):
    """Тот же лимит, что у бота (`handlers.ai_trainer.MAX_VOICE_BYTES`), не
    новое число — но опущен здесь до нескольких байт ради скорости теста."""
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(api_v1_voice, "MAX_VOICE_BYTES", 4)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/voice", json={"audio_data_url": _audio_data_url()})
    assert resp.status_code == 400
    assert resp.json()["error"] == "voice_too_big"


@pytest.mark.asyncio
async def test_ai_voice_returns_error_on_empty_transcript(fresh_db, client_factory, monkeypatch):
    """Тихая/невнятная запись — провайдер вернул пустую строку, как и боту."""
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "transcribe_voice", _fake_transcribe(""))
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/voice", json={"audio_data_url": _audio_data_url()})
    assert resp.status_code == 422
    assert resp.json()["error"] == "voice_empty"


@pytest.mark.asyncio
async def test_ai_voice_returns_502_when_provider_fails(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)

    async def _boom(file_obj, user_id=None):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(ai_trainer, "transcribe_voice", _boom)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/voice", json={"audio_data_url": _audio_data_url()})
    assert resp.status_code == 502
    assert resp.json()["error"] == "voice_transcribe_failed"


# ---------- POST /workouts/{id}/sets/voice ----------


@pytest.mark.asyncio
async def test_log_set_voice_requires_auth(client_factory):
    client = client_factory()
    resp = await client.post(
        "/workouts/1/sets/voice", json={"exercise_id": 1, "audio_data_url": _audio_data_url()}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_log_set_voice_success(fresh_db, client_factory, monkeypatch):
    """«сто на восемь» голосом должно записаться как 100×8 — тот же voice_parse
    и тот же parser.parse_sets_line, что использует бот."""
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "transcribe_voice", _fake_transcribe("сто на восемь"))
    client = await _linked_client(fresh_db, client_factory)
    workout_id, exercise_id = await _active_workout_with_exercise(client)

    resp = await client.post(
        f"/workouts/{workout_id}/sets/voice",
        json={"exercise_id": exercise_id, "audio_data_url": _audio_data_url()},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["transcript"] == "сто на восемь"
    (logged,) = body["sets"]
    assert (logged["weight"], logged["reps"]) == (100.0, 8)


@pytest.mark.asyncio
async def test_log_set_voice_not_configured(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: False)
    client = await _linked_client(fresh_db, client_factory)
    workout_id, exercise_id = await _active_workout_with_exercise(client)

    resp = await client.post(
        f"/workouts/{workout_id}/sets/voice",
        json={"exercise_id": exercise_id, "audio_data_url": _audio_data_url()},
    )
    assert resp.status_code == 503
    assert resp.json()["error"] == "not_configured"


@pytest.mark.asyncio
async def test_log_set_voice_rejects_too_big_payload(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(api_v1_voice, "MAX_VOICE_BYTES", 4)
    client = await _linked_client(fresh_db, client_factory)
    workout_id, exercise_id = await _active_workout_with_exercise(client)

    resp = await client.post(
        f"/workouts/{workout_id}/sets/voice",
        json={"exercise_id": exercise_id, "audio_data_url": _audio_data_url()},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "voice_too_big"


@pytest.mark.asyncio
async def test_log_set_voice_rejects_too_long_duration(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(api_v1_voice, "MAX_VOICE_SECONDS", 5)
    client = await _linked_client(fresh_db, client_factory)
    workout_id, exercise_id = await _active_workout_with_exercise(client)

    resp = await client.post(
        f"/workouts/{workout_id}/sets/voice",
        json={"exercise_id": exercise_id, "audio_data_url": _audio_data_url(), "duration_seconds": 999},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "voice_too_long"


@pytest.mark.asyncio
async def test_log_set_voice_unintelligible_audio_is_unparsed(fresh_db, client_factory, monkeypatch):
    """Нет узнаваемых чисел в расшифровке (в т.ч. пустая строка на невнятной
    записи) — та же ошибка, что бот показывает единым сообщением
    (`workout.voice_parse_failed`), без 500."""
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "transcribe_voice", _fake_transcribe(""))
    client = await _linked_client(fresh_db, client_factory)
    workout_id, exercise_id = await _active_workout_with_exercise(client)

    resp = await client.post(
        f"/workouts/{workout_id}/sets/voice",
        json={"exercise_id": exercise_id, "audio_data_url": _audio_data_url()},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "unparsed_input"

    # ничего не должно было записаться
    workout = await client.get(f"/workouts/{workout_id}")
    assert workout.json()["blocks"] == []


@pytest.mark.asyncio
async def test_log_set_voice_gibberish_transcript_is_unparsed(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "transcribe_voice", _fake_transcribe("превед медвед"))
    client = await _linked_client(fresh_db, client_factory)
    workout_id, exercise_id = await _active_workout_with_exercise(client)

    resp = await client.post(
        f"/workouts/{workout_id}/sets/voice",
        json={"exercise_id": exercise_id, "audio_data_url": _audio_data_url()},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "unparsed_input"


@pytest.mark.asyncio
async def test_log_set_voice_rejects_someone_elses_exercise(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "transcribe_voice", _fake_transcribe("сто на восемь"))
    owner = await _linked_client(fresh_db, client_factory, telegram_id=111)
    stranger = await _linked_client(fresh_db, client_factory, telegram_id=222)

    resp = await stranger.post("/exercises", json={"name": "Чужое упражнение"})
    stranger_exercise_id = resp.json()["id"]
    workout_id = (await owner.post("/workouts/active")).json()["id"]

    resp = await owner.post(
        f"/workouts/{workout_id}/sets/voice",
        json={"exercise_id": stranger_exercise_id, "audio_data_url": _audio_data_url()},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_log_set_voice_rejects_nonexistent_workout(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "transcribe_voice", _fake_transcribe("сто на восемь"))
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/exercises", json={"name": "Присед"})
    exercise_id = resp.json()["id"]

    resp = await client.post(
        "/workouts/999999/sets/voice",
        json={"exercise_id": exercise_id, "audio_data_url": _audio_data_url()},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_log_set_voice_rejects_someone_elses_workout(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "transcribe_voice", _fake_transcribe("сто на восемь"))
    owner = await _linked_client(fresh_db, client_factory, telegram_id=111)
    intruder = await _linked_client(fresh_db, client_factory, telegram_id=222)

    workout_id, exercise_id_owner = await _active_workout_with_exercise(owner)
    resp = await intruder.post("/exercises", json={"name": "Своё упражнение"})
    intruder_exercise_id = resp.json()["id"]

    resp = await intruder.post(
        f"/workouts/{workout_id}/sets/voice",
        json={"exercise_id": intruder_exercise_id, "audio_data_url": _audio_data_url()},
    )
    assert resp.status_code == 404
