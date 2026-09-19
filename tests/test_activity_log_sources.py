"""Одна лента на два клиента: что пришло из бота, а что из приложения.

По образцу tests/test_api_v1_account.py: httpx поверх ASGI-приложения без
сокета, `client_factory` и `_linked_client` заведены локально (в чужой тестовый
файл не лезем — его может в это же время редактировать другой агент).
"""

import httpx
import pytest

import activity_log
import api_v1
import api_v1_activity
import db
from handlers import admin


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


# ---------- действие из /v1 попадает в ленту ----------


@pytest.mark.asyncio
async def test_action_from_v1_lands_in_the_feed_as_ios(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/workouts/active", json={})
    assert resp.status_code in (200, 201), resp.text

    rows = await fresh_db.list_user_events(111, limit=10)
    actions = [r for r in rows if r["kind"] == api_v1_activity.KIND_API_ACTION]
    assert len(actions) == 1
    assert actions[0]["source"] == activity_log.SOURCE_IOS
    # По payload должно быть можно докопаться до конкретного запроса.
    assert actions[0]["payload"] == "POST /workouts/active"


@pytest.mark.asyncio
async def test_content_is_a_human_phrase_not_a_raw_path(fresh_db, client_factory):
    """Рядом с «нажал кнопку "🏁 Завершить"» сырой путь читается как мусор."""
    client = await _linked_client(fresh_db, client_factory)
    started = await client.post("/workouts/active", json={})
    workout_id = started.json()["id"]
    group_id = (await client.get("/muscle-groups")).json()[0]["id"]
    exercise = await client.post(
        "/exercises", json={"name": "Жим лёжа", "primary_group_id": group_id}
    )
    assert exercise.status_code in (200, 201), exercise.text

    resp = await client.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise.json()["id"], "weight": 60, "reps": 8},
    )
    assert resp.status_code in (200, 201), resp.text

    rows = await fresh_db.list_user_events(111, limit=10)
    logged = next(r for r in rows if r["payload"] == f"POST /workouts/{workout_id}/sets")
    assert logged["content"] == "записал подход"


def test_route_template_keys_the_phrase_not_the_concrete_path():
    """Иначе в ленте была бы тысяча разных строк вместо одной повторяющейся."""
    assert (
        api_v1_activity.describe("POST", "/workouts/{workout_id}/sets") == "записал подход"
    )
    # Незнакомому маршруту — честное «метод путь», а не молчание.
    assert api_v1_activity.describe("POST", "/whatever") == "POST /whatever"


# ---------- чего в ленте быть не должно ----------


@pytest.mark.asyncio
async def test_get_requests_are_not_actions(fresh_db, client_factory):
    """Одно открытие приложения — десяток чтений; они утопили бы действия."""
    client = await _linked_client(fresh_db, client_factory)

    assert (await client.get("/me")).status_code == 200
    assert (await client.get("/workouts/active")).status_code == 200

    rows = await fresh_db.list_user_events(111, limit=50)
    assert [r for r in rows if r["kind"] == api_v1_activity.KIND_API_ACTION] == []


@pytest.mark.asyncio
async def test_unauthenticated_request_logs_nothing_and_does_not_blow_up(
    fresh_db, client_factory
):
    """Без токена неизвестно, чьё это действие, — в ленту его не положить."""
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    client = client_factory()

    resp = await client.post("/workouts/active", json={})
    assert resp.status_code == 401
    assert await fresh_db.count_all_events() == 0


@pytest.mark.asyncio
async def test_failed_request_is_not_logged_as_an_action(fresh_db, client_factory):
    """Тренировка не началась — «начал тренировку» в ленте было бы неправдой."""
    client = await _linked_client(fresh_db, client_factory)
    before = await fresh_db.count_all_events()

    # Такой тренировки нет — заканчивать нечего.
    resp = await client.post("/workouts/999999/finish", json={})
    assert resp.status_code >= 400, resp.text

    assert await fresh_db.count_all_events() == before


# ---------- телеграмный путь не сломан ----------


@pytest.mark.asyncio
async def test_telegram_event_is_still_written_as_tg(fresh_db):
    await fresh_db.get_or_create_user(111, "athlete")

    await activity_log.record_ai_reply(111, "Записал. Погнали дальше.")

    (row,) = await fresh_db.list_user_events(111, limit=10)
    assert row["source"] == activity_log.SOURCE_TG


# ---------- пометка в ленте ----------


def test_feed_line_shows_where_the_action_came_from(monkeypatch):
    import config

    monkeypatch.setattr(config, "ADMIN_TZ_OFFSET", 0)
    row = {
        "kind": api_v1_activity.KIND_API_ACTION,
        "content": "записал подход",
        "created_at": "2026-08-17T09:00:00",
        "source": activity_log.SOURCE_IOS,
        "username": "athlete",
        "telegram_id": 111,
    }

    assert "[ios]" in admin._activity_line(row)
    assert "[ios]" in admin._activity_line_all(row)
    row["source"] = activity_log.SOURCE_TG
    assert "[tg]" in admin._activity_line(row)


# ---------- падение лога не роняет запрос ----------


@pytest.mark.asyncio
async def test_broken_log_does_not_break_the_request(fresh_db, client_factory, monkeypatch):
    """Лог действий не тот повод, чтобы человеку не засчиталась тренировка."""
    client = await _linked_client(fresh_db, client_factory)

    async def boom(*args, **kwargs):
        raise RuntimeError("лог упал")

    monkeypatch.setattr(db, "log_user_event", boom)

    resp = await client.post("/workouts/active", json={})
    assert resp.status_code in (200, 201), resp.text
