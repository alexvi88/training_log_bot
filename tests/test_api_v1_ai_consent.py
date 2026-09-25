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

import asyncio

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


# ---------- фоновые вызовы модели, которые человек не заказывал ----------
#
# Тумблер «🤖 Комментарии тренера» и согласие — разные настройки. Отозвал
# согласие, а тумблер оставил — комментарий к тренировке всё равно не должен
# уходить модели; ошибки клиенту при этом нет: finish — карточка итога.


@pytest.fixture
def fake_comment(monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    calls: list[int] = []

    async def fake_comment_on_workout(user_id, workout_id):
        calls.append(workout_id)
        return "Хорошая работа."

    monkeypatch.setattr(ai_trainer, "comment_on_workout", fake_comment_on_workout)
    return calls


async def _finish_one_workout(client, headers=None) -> int:
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    resp = await client.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": 80, "reps": 5},
    )
    assert resp.status_code in (200, 201), resp.text
    resp = await client.post(f"/workouts/{workout_id}/finish", json={}, headers=headers or {})
    assert resp.status_code == 200, resp.text
    # Комментарий пишется фоновой задачей — дождаться её, иначе «модель не
    # позвали» было бы правдой только потому, что задача ещё не стартовала.
    pending = list(api_v1._ai_comment_tasks)
    if pending:
        await asyncio.gather(*pending)
    return workout_id


@pytest.mark.asyncio
async def test_finish_without_consent_does_not_call_model(fresh_db, client_factory, fake_comment):
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.update_user(111, ai_comments_enabled=1)
    await client.patch("/settings", json={"ai_consent": True})
    await client.patch("/settings", json={"ai_consent": False})

    workout_id = await _finish_one_workout(client, headers=HEADER)

    assert fake_comment == []
    resp = await client.get(f"/workouts/{workout_id}/ai-comment")
    assert resp.json() == {"comment": None}


@pytest.mark.asyncio
async def test_finish_without_consent_is_skipped_under_global_flag(
    fresh_db, client_factory, fake_comment, monkeypatch
):
    monkeypatch.setattr(config, "AI_CONSENT_REQUIRED", True)
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.update_user(111, ai_comments_enabled=1)

    await _finish_one_workout(client)

    assert fake_comment == []


@pytest.mark.asyncio
async def test_finish_with_consent_still_comments(fresh_db, client_factory, fake_comment):
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.update_user(111, ai_comments_enabled=1)
    await client.patch("/settings", json={"ai_consent": True})

    workout_id = await _finish_one_workout(client, headers=HEADER)

    assert fake_comment == [workout_id]
    resp = await client.get(f"/workouts/{workout_id}/ai-comment")
    assert resp.json() == {"comment": "Хорошая работа."}


@pytest.mark.asyncio
async def test_finish_from_released_build_still_comments(
    fresh_db, client_factory, fake_comment, monkeypatch
):
    """Сборки без листа согласия (без заголовка) при выключенном флаге
    получают комментарий как раньше."""
    monkeypatch.setattr(config, "AI_CONSENT_REQUIRED", False)
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.update_user(111, ai_comments_enabled=1)

    workout_id = await _finish_one_workout(client)

    assert fake_comment == [workout_id]


CSV_UNKNOWN_EXERCISE = "date,exercise,weight,reps\n2024-01-03,Жим Арнольда сидя,20,10\n"


@pytest.fixture
def fake_matcher(monkeypatch):
    calls: list[list[str]] = []

    async def fake_match(user_id, names):
        calls.append(list(names))
        return {}

    monkeypatch.setattr(ai_trainer, "match_exercise_names_to_catalog", fake_match)
    return calls


@pytest.mark.asyncio
async def test_import_without_consent_skips_ai_matching(fresh_db, client_factory, fake_matcher):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/import/csv", json={"csv": CSV_UNKNOWN_EXERCISE}, headers=HEADER)
    assert resp.status_code == 200, resp.text
    assert resp.json()["workouts_imported"] == 1
    assert fake_matcher == []
    # Имя из файла заведено как есть — тренировка не потерялась.
    assert await fresh_db.find_exercise_by_name(111, "Жим Арнольда сидя") is not None


@pytest.mark.asyncio
async def test_import_with_consent_uses_ai_matching(fresh_db, client_factory, fake_matcher):
    client = await _linked_client(fresh_db, client_factory)
    await client.patch("/settings", json={"ai_consent": True})
    resp = await client.post("/import/csv", json={"csv": CSV_UNKNOWN_EXERCISE}, headers=HEADER)
    assert resp.status_code == 200, resp.text
    assert fake_matcher == [["Жим Арнольда сидя"]]


async def _finish_payload(client, headers=None) -> dict:
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await client.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": 80, "reps": 5},
    )
    resp = await client.post(f"/workouts/{workout_id}/finish", json={}, headers=headers or {})
    assert resp.status_code == 200, resp.text
    pending = list(api_v1._ai_comment_tasks)
    if pending:
        await asyncio.gather(*pending)
    return resp.json()


@pytest.mark.asyncio
async def test_finish_reports_comment_pending_only_when_ordered(fresh_db, client_factory, fake_comment):
    """`ai_comment_pending` в ответе finish — приложение ждёт комментарий
    («тренер печатает…») только тогда, когда сервер его правда заказал."""
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.update_user(111, ai_comments_enabled=1)
    await client.patch("/settings", json={"ai_consent": True})
    assert (await _finish_payload(client, headers=HEADER))["ai_comment_pending"] is True

    await fresh_db.update_user(111, ai_comments_enabled=0)
    assert (await _finish_payload(client, headers=HEADER))["ai_comment_pending"] is False

    await fresh_db.update_user(111, ai_comments_enabled=1)
    await client.patch("/settings", json={"ai_consent": False})
    assert (await _finish_payload(client, headers=HEADER))["ai_comment_pending"] is False
