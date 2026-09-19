"""REST `/v1` для программ и дней тренировок (api_v1_programs.py).

Тот же подход, что у tests/test_api_v1.py: httpx поверх ASGI-приложения без
сокета, свой Bearer-токен на каждого "пользователя". Основной фокус — проверка
владения: id программы/дня/строки упражнения угадываются, и чужой объект не
должен быть виден и правим по чужому токену.
"""

import httpx
import pytest

import api_v1
import config


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username=f"user{telegram_id}")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------- программы: happy path ----------

@pytest.mark.asyncio
async def test_program_create_list_get(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)

    created = await client.post("/programs", json={"name": "PPL", "description": "push/pull/legs"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "PPL"
    assert body["description"] == "push/pull/legs"
    assert body["days"] == []
    program_id = body["id"]

    listed = await client.get("/programs")
    assert listed.status_code == 200
    assert [p["id"] for p in listed.json()] == [program_id]
    assert listed.json()[0]["day_count"] == 0

    fetched = await client.get(f"/programs/{program_id}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == program_id
    assert fetched.json()["description"] == "push/pull/legs"


@pytest.mark.asyncio
async def test_program_create_rejects_duplicate_name(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    first = await client.post("/programs", json={"name": "PPL"})
    assert first.status_code == 201
    second = await client.post("/programs", json={"name": "PPL"})
    assert second.status_code == 409
    assert second.json()["error"] == "name_taken"


@pytest.mark.asyncio
async def test_program_create_rejects_empty_name(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/programs", json={"name": "   "})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_program_update_name_and_description(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    created = await client.post("/programs", json={"name": "PPL"})
    program_id = created.json()["id"]

    patched = await client.patch(f"/programs/{program_id}", json={"name": "PPL v2", "description": "new"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "PPL v2"
    assert patched.json()["description"] == "new"

    cleared = await client.patch(f"/programs/{program_id}", json={"description": None})
    assert cleared.status_code == 200
    assert cleared.json()["description"] is None


@pytest.mark.asyncio
async def test_program_update_name_collision(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    await client.post("/programs", json={"name": "PPL"})
    other = await client.post("/programs", json={"name": "Upper/Lower"})
    other_id = other.json()["id"]

    resp = await client.patch(f"/programs/{other_id}", json={"name": "PPL"})
    assert resp.status_code == 409
    assert resp.json()["error"] == "name_taken"


@pytest.mark.asyncio
async def test_program_delete(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    created = await client.post("/programs", json={"name": "PPL"})
    program_id = created.json()["id"]

    deleted = await client.delete(f"/programs/{program_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}

    gone = await client.get(f"/programs/{program_id}")
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_program_not_found(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/programs/999")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_program_days_add_and_next_day(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    program_id = (await client.post("/programs", json={"name": "PPL"})).json()["id"]

    day1 = await client.post(f"/programs/{program_id}/days", json={"name": "Push"})
    assert day1.status_code == 201, day1.text
    assert day1.json()["program_id"] == program_id
    assert day1.json()["exercise_count"] == 0

    day2 = await client.post(f"/programs/{program_id}/days", json={"name": "Pull"})
    assert day2.status_code == 201

    fetched = await client.get(f"/programs/{program_id}")
    assert [d["name"] for d in fetched.json()["days"]] == ["Push", "Pull"]

    next_day = await client.get(f"/programs/{program_id}/next-day")
    assert next_day.status_code == 200
    # программа ни разу не пройдена — следующий день первый по порядку
    assert next_day.json()["name"] == "Push"


@pytest.mark.asyncio
async def test_program_next_day_empty_program(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    program_id = (await client.post("/programs", json={"name": "PPL"})).json()["id"]
    resp = await client.get(f"/programs/{program_id}/next-day")
    assert resp.status_code == 200
    assert resp.json() is None


# ---------- самостоятельные дни ----------

@pytest.mark.asyncio
async def test_routine_create_list_get_update_delete(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)

    created = await client.post("/routines", json={"name": "Full body"})
    assert created.status_code == 201, created.text
    routine_id = created.json()["id"]
    assert created.json()["program_id"] is None
    assert created.json()["exercise_count"] == 0

    listed = await client.get("/routines")
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()] == [routine_id]

    fetched = await client.get(f"/routines/{routine_id}")
    assert fetched.status_code == 200
    assert fetched.json()["exercises"] == []

    patched = await client.patch(f"/routines/{routine_id}", json={"name": "Full body v2"})
    assert patched.status_code == 200
    assert patched.json()["name"] == "Full body v2"

    deleted = await client.delete(f"/routines/{routine_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}

    gone = await client.get(f"/routines/{routine_id}")
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_routine_not_found(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/routines/999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_routine_create_rejects_empty_name(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/routines", json={"name": ""})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_routine_budget_enforced(fresh_db, client_factory, monkeypatch):
    """Тот же потолок, что у бота (db.routine_budget) — эндпоинт создания не
    должен позволять обойти его."""
    monkeypatch.setattr(config, "MAX_ROUTINES_PER_USER", 2)
    client = await _linked_client(fresh_db, client_factory)

    first = await client.post("/routines", json={"name": "Day 1"})
    assert first.status_code == 201
    second = await client.post("/routines", json={"name": "Day 2"})
    assert second.status_code == 201

    third = await client.post("/routines", json={"name": "Day 3"})
    assert third.status_code == 403
    assert third.json()["error"] == "routine_limit_reached"


@pytest.mark.asyncio
async def test_program_day_budget_enforced(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "MAX_ROUTINES_PER_USER", 1)
    client = await _linked_client(fresh_db, client_factory)
    program_id = (await client.post("/programs", json={"name": "PPL"})).json()["id"]

    first = await client.post(f"/programs/{program_id}/days", json={"name": "Push"})
    assert first.status_code == 201

    second = await client.post(f"/programs/{program_id}/days", json={"name": "Pull"})
    assert second.status_code == 403
    assert second.json()["error"] == "routine_limit_reached"


# ---------- упражнения дня ----------

async def _make_exercise(fresh_db, user_id: int, name: str = "Bench press") -> int:
    return await fresh_db.create_exercise(user_id, name, group_id=None)


@pytest.mark.asyncio
async def test_routine_exercise_add_update_delete(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    routine_id = (await client.post("/routines", json={"name": "Push day"})).json()["id"]
    exercise_id = await _make_exercise(fresh_db, 111)

    added = await client.post(
        f"/routines/{routine_id}/exercises", json={"exercise_id": exercise_id, "target": "3x8-10"}
    )
    assert added.status_code == 201, added.text
    item = added.json()
    assert item["exercise_id"] == exercise_id
    assert item["routine_id"] == routine_id
    item_id = item["id"]

    fetched = await client.get(f"/routines/{routine_id}")
    assert fetched.json()["exercise_count"] == 1
    assert len(fetched.json()["exercises"]) == 1

    patched = await client.patch(f"/routine-exercises/{item_id}", json={"target": "5x5"})
    assert patched.status_code == 200, patched.text
    # normalize_routine_target приводит "x" к "×" — тот же вид, что у бота
    assert patched.json()["target"] == "5×5"

    with_progression = await client.patch(
        f"/routine-exercises/{item_id}",
        json={"progression": {"rule": "linear_load", "step": 2.5}},
    )
    assert with_progression.status_code == 200
    assert with_progression.json()["progression"] == {"rule": "linear_load", "step": 2.5}

    deleted = await client.delete(f"/routine-exercises/{item_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}

    fetched_after = await client.get(f"/routines/{routine_id}")
    assert fetched_after.json()["exercises"] == []


@pytest.mark.asyncio
async def test_routine_exercise_rejects_foreign_exercise(fresh_db, client_factory):
    """exercise_id из чужого каталога нельзя подставить в свой день."""
    client = await _linked_client(fresh_db, client_factory, telegram_id=111)
    other_client = await _linked_client(fresh_db, client_factory, telegram_id=222)

    routine_id = (await client.post("/routines", json={"name": "Push day"})).json()["id"]
    foreign_exercise_id = await _make_exercise(fresh_db, 222)

    resp = await client.post(
        f"/routines/{routine_id}/exercises", json={"exercise_id": foreign_exercise_id}
    )
    assert resp.status_code == 404
    await other_client.aclose()


@pytest.mark.asyncio
async def test_routine_exercise_not_found(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.patch("/routine-exercises/999", json={"target": "5x5"})
    assert resp.status_code == 404
    resp2 = await client.delete("/routine-exercises/999")
    assert resp2.status_code == 404


# ---------- 401 без токена ----------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/programs"),
        ("POST", "/programs"),
        ("GET", "/programs/1"),
        ("PATCH", "/programs/1"),
        ("DELETE", "/programs/1"),
        ("GET", "/programs/1/next-day"),
        ("POST", "/programs/1/days"),
        ("GET", "/routines"),
        ("POST", "/routines"),
        ("GET", "/routines/1"),
        ("PATCH", "/routines/1"),
        ("DELETE", "/routines/1"),
        ("POST", "/routines/1/exercises"),
        ("PATCH", "/routine-exercises/1"),
        ("DELETE", "/routine-exercises/1"),
    ],
)
async def test_requires_auth(fresh_db, client_factory, method, path):
    client = client_factory()
    resp = await client.request(method, path, json={} if method in ("POST", "PATCH") else None)
    assert resp.status_code == 401


# ---------- владение: чужой объект не виден и не правится ----------

@pytest.mark.asyncio
async def test_foreign_program_is_hidden(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, telegram_id=111)
    intruder = await _linked_client(fresh_db, client_factory, telegram_id=222)

    program_id = (await owner.post("/programs", json={"name": "PPL"})).json()["id"]

    assert (await intruder.get(f"/programs/{program_id}")).status_code == 404
    assert (await intruder.patch(f"/programs/{program_id}", json={"name": "Hacked"})).status_code == 404
    assert (await intruder.post(f"/programs/{program_id}/days", json={"name": "Day"})).status_code == 404
    assert (await intruder.get(f"/programs/{program_id}/next-day")).status_code == 404
    assert (await intruder.delete(f"/programs/{program_id}")).status_code == 404

    # владелец по-прежнему видит программу — правки чужого токена её не задели
    assert (await owner.get(f"/programs/{program_id}")).status_code == 200


@pytest.mark.asyncio
async def test_foreign_routine_is_hidden(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, telegram_id=111)
    intruder = await _linked_client(fresh_db, client_factory, telegram_id=222)

    routine_id = (await owner.post("/routines", json={"name": "Push day"})).json()["id"]

    assert (await intruder.get(f"/routines/{routine_id}")).status_code == 404
    assert (await intruder.patch(f"/routines/{routine_id}", json={"name": "Hacked"})).status_code == 404
    assert (await intruder.post(
        f"/routines/{routine_id}/exercises", json={"exercise_id": 1}
    )).status_code == 404
    assert (await intruder.delete(f"/routines/{routine_id}")).status_code == 404

    assert (await owner.get(f"/routines/{routine_id}")).status_code == 200


@pytest.mark.asyncio
async def test_foreign_routine_exercise_is_hidden(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, telegram_id=111)
    intruder = await _linked_client(fresh_db, client_factory, telegram_id=222)

    routine_id = (await owner.post("/routines", json={"name": "Push day"})).json()["id"]
    exercise_id = await _make_exercise(fresh_db, 111)
    item_id = (
        await owner.post(f"/routines/{routine_id}/exercises", json={"exercise_id": exercise_id})
    ).json()["id"]

    assert (await intruder.patch(f"/routine-exercises/{item_id}", json={"target": "5x5"})).status_code == 404
    assert (await intruder.delete(f"/routine-exercises/{item_id}")).status_code == 404

    # владелец по-прежнему может это делать — строка не тронута чужим токеном
    still_there = await owner.get(f"/routines/{routine_id}")
    assert len(still_there.json()["exercises"]) == 1
