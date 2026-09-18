"""REST `/v1` для iOS-клиента: связка аккаунта, подход, история, вес тела.

Гоняется через httpx поверх ASGI-приложения без сокета — тот же путь запроса,
что увидит настоящий клиент, включая проверку Bearer-токена.
"""

import httpx
import pytest

import api_v1


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


@pytest.mark.asyncio
async def test_auth_link_rejects_unknown_code(fresh_db, client_factory):
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": "nope"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_code"


@pytest.mark.asyncio
async def test_auth_link_issues_token_and_consumes_code(fresh_db, client_factory):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    code = await fresh_db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == 111
    assert body["unit"] == "kg"

    # a used code doesn't work twice
    resp2 = await client.post("/auth/link", json={"code": code})
    assert resp2.status_code == 400


@pytest.mark.asyncio
async def test_auth_link_rate_limits_repeated_bad_codes(fresh_db, client_factory):
    """6-8 цифр — перебираемо без лимита попыток; после mcp_oauth.CONSENT_FAILURE_LIMIT_PER_IP
    неудач подряд дальнейшие попытки должны запираться, а не пробовать код."""
    import mcp_oauth

    client = client_factory()
    for _ in range(mcp_oauth.CONSENT_FAILURE_LIMIT_PER_IP):
        resp = await client.post("/auth/link", json={"code": "00000000"})
        assert resp.status_code == 400

    resp = await client.post("/auth/link", json={"code": "00000000"})
    assert resp.status_code == 429
    assert resp.json()["error"] == "rate_limited"


@pytest.mark.asyncio
async def test_me_requires_bearer_token(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_linked_user(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/me")
    assert resp.status_code == 200
    assert resp.json()["user_id"] == 111


@pytest.mark.asyncio
async def test_linking_ios_does_not_revoke_mcp_token(fresh_db, client_factory):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    mcp_token = await fresh_db.issue_mcp_token(111)
    await _linked_client(fresh_db, client_factory)
    assert await fresh_db.resolve_mcp_token(mcp_token) == 111


@pytest.mark.asyncio
async def test_full_workout_flow(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)

    exercise_resp = await client.post("/exercises", json={"name": "Жим лёжа"})
    assert exercise_resp.status_code == 201
    exercise_id = exercise_resp.json()["id"]

    assert (await client.get("/workouts/active")).json() is None

    start_resp = await client.post("/workouts/active")
    assert start_resp.status_code == 201
    workout_id = start_resp.json()["id"]

    # idempotent: asking again returns the same active workout
    again = await client.post("/workouts/active")
    assert again.status_code == 200
    assert again.json()["id"] == workout_id

    set_resp = await client.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": 80, "reps": 5, "rpe": 8},
    )
    assert set_resp.status_code == 201
    assert set_resp.json()["weight"] == 80
    assert set_resp.json()["reps"] == 5

    active = await client.get("/workouts/active")
    assert active.status_code == 200
    blocks = active.json()["blocks"]
    assert len(blocks) == 1
    assert blocks[0]["exercises"][0]["display_name"] == "Жим лёжа"
    assert len(blocks[0]["exercises"][0]["sets"]) == 1

    finish_resp = await client.post(f"/workouts/{workout_id}/finish", json={"note": "норм"})
    assert finish_resp.status_code == 200
    assert finish_resp.json()["status"] == "finished"

    # a finished workout no longer accepts new sets
    late_set = await client.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": 80, "reps": 5},
    )
    assert late_set.status_code == 409

    history = await client.get("/workouts")
    assert history.status_code == 200
    items = history.json()
    assert len(items) == 1
    assert items[0]["id"] == workout_id
    assert items[0]["exercise_names"] == ["Жим лёжа"]
    assert items[0]["set_count"] == 1

    detail = await client.get(f"/workouts/{workout_id}")
    assert detail.status_code == 200
    assert detail.json()["note"] == "норм"


@pytest.mark.asyncio
async def test_delete_last_set(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]

    missing = await client.delete(f"/workouts/{workout_id}/exercises/{exercise_id}/last-set")
    assert missing.status_code == 404

    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 100, "reps": 5}
    )
    second = await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 110, "reps": 3}
    )
    assert second.status_code == 201

    deleted = await client.delete(f"/workouts/{workout_id}/exercises/{exercise_id}/last-set")
    assert deleted.status_code == 200
    assert deleted.json()["weight"] == 110

    active = await client.get("/workouts/active")
    sets = active.json()["blocks"][0]["exercises"][0]["sets"]
    assert len(sets) == 1
    assert sets[0]["weight"] == 100


@pytest.mark.asyncio
async def test_discard_active_workout(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)

    missing = await client.delete("/workouts/active")
    assert missing.status_code == 404

    workout_id = (await client.post("/workouts/active")).json()["id"]
    discarded = await client.delete("/workouts/active")
    assert discarded.status_code == 200
    assert discarded.json()["discarded"] is True

    assert (await client.get("/workouts/active")).json() is None
    # gone for good, not just unlinked from "active"
    history = await client.get("/workouts")
    assert all(item["id"] != workout_id for item in history.json())


@pytest.mark.asyncio
async def test_set_logging_rejects_another_users_exercise(fresh_db, client_factory):
    client_a = await _linked_client(fresh_db, client_factory, telegram_id=111)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=222)

    exercise_resp = await client_a.post("/exercises", json={"name": "Присед"})
    exercise_id = exercise_resp.json()["id"]

    start_resp = await client_b.post("/workouts/active")
    workout_id = start_resp.json()["id"]

    resp = await client_b.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": 60, "reps": 10},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_exercises_filters_by_group(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    legs_id = await fresh_db.create_muscle_group(111, "Ноги", "🦵")
    chest_id = await fresh_db.create_muscle_group(111, "Грудь", "💪")
    await client.post("/exercises", json={"name": "Присед", "group_id": legs_id})
    await client.post("/exercises", json={"name": "Жим лёжа", "group_id": chest_id})
    await client.post("/exercises", json={"name": "Без группы"})

    resp = await client.get(f"/exercises?group_id={legs_id}")
    assert resp.status_code == 200
    names = [e["display_name"] for e in resp.json()]
    assert names == ["Присед"]

    bad = await client.get("/exercises?group_id=not-a-number")
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_exercise_progress_lists_sets_from_finished_workouts_only(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]

    workout_id = (await client.post("/workouts/active")).json()["id"]
    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 80, "reps": 5}
    )
    # a set logged in the still-active workout shouldn't show up in progress yet
    empty = await client.get(f"/exercises/{exercise_id}/progress")
    assert empty.status_code == 200
    assert empty.json() == []

    await client.post(f"/workouts/{workout_id}/finish", json={})
    progress = await client.get(f"/exercises/{exercise_id}/progress")
    assert progress.status_code == 200
    entries = progress.json()
    assert len(entries) == 1
    assert entries[0]["workout_id"] == workout_id
    assert entries[0]["weight"] == 80
    assert entries[0]["reps"] == 5


@pytest.mark.asyncio
async def test_exercise_progress_rejects_another_users_exercise(fresh_db, client_factory):
    client_a = await _linked_client(fresh_db, client_factory, telegram_id=111)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=222)
    exercise_id = (await client_a.post("/exercises", json={"name": "Присед"})).json()["id"]

    resp = await client_b.get(f"/exercises/{exercise_id}/progress")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_bodyweight_crud(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)

    add_resp = await client.post("/bodyweight", json={"weight": 82.5})
    assert add_resp.status_code == 201
    log_id = add_resp.json()["id"]

    list_resp = await client.get("/bodyweight")
    assert list_resp.status_code == 200
    assert list_resp.json()[0]["weight"] == 82.5

    delete_resp = await client.delete(f"/bodyweight/{log_id}")
    assert delete_resp.status_code == 200
    assert (await client.get("/bodyweight")).json() == []

    missing = await client.delete(f"/bodyweight/{log_id}")
    assert missing.status_code == 404
