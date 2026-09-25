"""POST /v1/workouts/{id}/ai-comment — комментарий тренера по запросу.

Что держат эти тесты:

- без согласия на передачу данных AI — 403 `ai_consent_required`, модель
  не зовётся;
- выключенный тумблер «🤖 Комментарии тренера» ручной запрос не запирает;
- уже сохранённый комментарий отдаётся без модели;
- два запроса подряд и запрос, заставший фоновый комментарий после finish, —
  один вызов модели на всех;
- чужая тренировка — 404, незаконченная — 409;
- срыв модели — 502 с человеческим текстом, а не 500; HARD-стоп по деньгам —
  429 с текстом лимита.
"""

import asyncio

import httpx
import pytest

import ai_limits
import ai_trainer
import api_v1
import config
import i18n

HEADER = {config.AI_CONSENT_CLIENT_HEADER: "1"}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")


async def _linked_client(fresh_db, telegram_id=111, lang="ru", comments_enabled=0):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    await fresh_db.set_user_lang(telegram_id, lang)
    await fresh_db.update_user(telegram_id, ai_comments_enabled=comments_enabled)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = _client()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _consent(client):
    resp = await client.patch("/settings", json={"ai_consent": True})
    assert resp.status_code == 200, resp.text


async def _workout(client, finish=True) -> int:
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    resp = await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 80, "reps": 5}
    )
    assert resp.status_code in (200, 201), resp.text
    if finish:
        resp = await client.post(f"/workouts/{workout_id}/finish", json={}, headers=HEADER)
        assert resp.status_code == 200, resp.text
    return workout_id


@pytest.fixture
def model(monkeypatch):
    """Подменённая модель. `gate` держит ответ, пока тест его не отпустит;
    `fail` превращает вызов в исключение."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    state = {"calls": [], "gate": None, "fail": False}

    async def fake_comment_on_workout(user_id, workout_id):
        state["calls"].append(workout_id)
        if state["gate"] is not None:
            await state["gate"].wait()
        if state["fail"]:
            raise RuntimeError("model is down")
        return "Хорошая работа."

    monkeypatch.setattr(ai_trainer, "comment_on_workout", fake_comment_on_workout)
    return state


async def _wait_for_call(state):
    for _ in range(500):
        if state["calls"]:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("model was never called")


@pytest.mark.asyncio
async def test_without_consent_is_403_and_model_is_not_called(fresh_db, model):
    client = await _linked_client(fresh_db)
    workout_id = await _workout(client)

    resp = await client.post(f"/workouts/{workout_id}/ai-comment", headers=HEADER)

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"] == "ai_consent_required"
    assert "Согласен" in resp.json()["message"]
    assert model["calls"] == []


@pytest.mark.asyncio
async def test_toggle_off_with_consent_generates_and_stores(fresh_db, model):
    client = await _linked_client(fresh_db, comments_enabled=0)
    await _consent(client)
    workout_id = await _workout(client)
    # Тумблер выключен — фон после finish модель не звал.
    assert model["calls"] == []

    resp = await client.post(f"/workouts/{workout_id}/ai-comment", headers=HEADER)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"comment": "Хорошая работа."}
    assert model["calls"] == [workout_id]
    assert (await client.get(f"/workouts/{workout_id}/ai-comment")).json() == {"comment": "Хорошая работа."}


@pytest.mark.asyncio
async def test_existing_comment_is_returned_without_model(fresh_db, model):
    client = await _linked_client(fresh_db)
    workout_id = await _workout(client)
    await fresh_db.set_workout_ai_comment(workout_id, "Уже написал.")

    # Даже без согласия: наружу ничего не уходит.
    resp = await client.post(f"/workouts/{workout_id}/ai-comment", headers=HEADER)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"comment": "Уже написал."}
    assert model["calls"] == []


@pytest.mark.asyncio
async def test_double_request_makes_one_model_call(fresh_db, model):
    client = await _linked_client(fresh_db)
    await _consent(client)
    workout_id = await _workout(client)
    model["gate"] = asyncio.Event()

    first = asyncio.create_task(client.post(f"/workouts/{workout_id}/ai-comment", headers=HEADER))
    await _wait_for_call(model)
    second = asyncio.create_task(client.post(f"/workouts/{workout_id}/ai-comment", headers=HEADER))
    # Второй запрос успевает дойти до ожидания, пока модель ещё «думает».
    await asyncio.sleep(0.1)
    model["gate"].set()
    responses = await asyncio.gather(first, second)

    assert [r.status_code for r in responses] == [200, 200]
    assert all(r.json() == {"comment": "Хорошая работа."} for r in responses)
    assert model["calls"] == [workout_id]
    assert api_v1._ai_comment_inflight == {}


@pytest.mark.asyncio
async def test_request_joins_background_comment_after_finish(fresh_db, model):
    client = await _linked_client(fresh_db, comments_enabled=1)
    await _consent(client)
    model["gate"] = asyncio.Event()
    workout_id = await _workout(client)  # finish запускает фоновый комментарий
    await _wait_for_call(model)

    pending = asyncio.create_task(client.post(f"/workouts/{workout_id}/ai-comment", headers=HEADER))
    await asyncio.sleep(0.1)
    model["gate"].set()
    resp = await pending

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"comment": "Хорошая работа."}
    assert model["calls"] == [workout_id]


@pytest.mark.asyncio
async def test_not_owner_is_404(fresh_db, model):
    owner = await _linked_client(fresh_db, telegram_id=111)
    workout_id = await _workout(owner)
    stranger = await _linked_client(fresh_db, telegram_id=222)
    await _consent(stranger)

    resp = await stranger.post(f"/workouts/{workout_id}/ai-comment", headers=HEADER)

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "not_found"
    assert model["calls"] == []


@pytest.mark.asyncio
async def test_unfinished_workout_is_409(fresh_db, model):
    client = await _linked_client(fresh_db)
    await _consent(client)
    workout_id = await _workout(client, finish=False)

    resp = await client.post(f"/workouts/{workout_id}/ai-comment", headers=HEADER)

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "workout_not_finished"
    assert body["message"] == i18n.t_in("ru", "api.error.workout_not_finished")
    assert model["calls"] == []


@pytest.mark.asyncio
async def test_model_failure_is_502_with_message_and_retry_works(fresh_db, model):
    client = await _linked_client(fresh_db, lang="en")
    await _consent(client)
    workout_id = await _workout(client)
    model["fail"] = True

    resp = await client.post(f"/workouts/{workout_id}/ai-comment", headers=HEADER)

    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert body["error"] == "comment_failed"
    assert body["message"] == i18n.t_in("en", "ai.screen.comment_failed")
    assert (await client.get(f"/workouts/{workout_id}/ai-comment")).json() == {"comment": None}

    # Срыв не оставляет запись «генерируется» — следующий тап идёт к модели.
    model["fail"] = False
    retry = await client.post(f"/workouts/{workout_id}/ai-comment", headers=HEADER)
    assert retry.status_code == 200, retry.text
    assert len(model["calls"]) == 2


@pytest.mark.asyncio
async def test_spend_hard_stop_is_429_with_limit_text(fresh_db, model, monkeypatch):
    client = await _linked_client(fresh_db)
    await _consent(client)
    workout_id = await _workout(client)

    async def blocked():
        return ai_limits.Block(
            kind=ai_limits.KIND_SPEND_HARD, log="spend_hard: test", user_text=i18n.t("limit.spend_hard")
        )

    monkeypatch.setattr(ai_limits, "hard_stop_block", blocked)

    resp = await client.post(f"/workouts/{workout_id}/ai-comment", headers=HEADER)

    assert resp.status_code == 429, resp.text
    body = resp.json()
    assert body["error"] == "spend_limit_exceeded"
    assert body["message"] == i18n.t_in("ru", "limit.spend_hard")
    assert model["calls"] == []


# ---------- дефолт тумблера у новых аккаунтов ----------


@pytest.mark.asyncio
async def test_new_accounts_have_comments_on(fresh_db):
    tg = await fresh_db.get_or_create_user(telegram_id=333, username="new")
    app_only = await fresh_db.create_app_only_user(language_code="en")
    assert tg["ai_comments_enabled"] == 1
    assert app_only["ai_comments_enabled"] == 1


@pytest.mark.asyncio
async def test_live_db_with_old_default_keeps_existing_and_turns_on_new(tmp_path):
    """Живая база: колонка уже есть с DEFAULT 0, и SQLite его не поменяет.
    Старые записи остаются как были, новые получают 1 явной вставкой."""
    import sqlite3

    import db

    path = tmp_path / "live.sqlite"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        db.SCHEMA.replace(
            "ai_comments_enabled INTEGER NOT NULL DEFAULT 1", "ai_comments_enabled INTEGER NOT NULL DEFAULT 0"
        )
    )
    legacy.execute(
        "INSERT INTO users (telegram_id, username, created_at) VALUES (444, 'old', '2025-01-01T00:00:00')"
    )
    legacy.commit()
    legacy.close()

    db._write_lock = asyncio.Lock()
    await db.init_db(str(path))
    try:
        assert (await db.get_user(444))["ai_comments_enabled"] == 0
        assert (await db.get_or_create_user(555, "new"))["ai_comments_enabled"] == 1
        assert (await db.create_app_only_user())["ai_comments_enabled"] == 1
    finally:
        await db.close_db()
