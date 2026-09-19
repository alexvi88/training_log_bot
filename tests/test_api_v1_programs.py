"""REST `/v1` для программ и дней тренировок (api_v1_programs.py).

Тот же подход, что у tests/test_api_v1.py: httpx поверх ASGI-приложения без
сокета, свой Bearer-токен на каждого "пользователя". Основной фокус — проверка
владения: id программы/дня/строки упражнения угадываются, и чужой объект не
должен быть виден и правим по чужому токену.
"""

import asyncio

import httpx
import pytest

import api_v1
import config
import db
import seed_data


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


# ---------- готовые программы (каталог) ----------

@pytest.mark.asyncio
async def test_catalog_is_returned(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/programs/catalog")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == len(seed_data.WORKOUT_PROGRAMS)
    ppl = next(p for p in body if p["key"] == "ppl")
    # Названия/дни/упражнения — данные каталога на языке пользователя (ru по
    # умолчанию), а не голые ключи.
    assert ppl["name"] == seed_data.localized_program_name("ppl", "ru")
    assert len(ppl["days"]) == len(seed_data.PROGRAM_BY_KEY["ppl"]["days"])
    assert ppl["days"][0]["exercises"][0]["name"]


@pytest.mark.asyncio
async def test_catalog_add_creates_program_with_days_and_exercises(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    key = seed_data.WORKOUT_PROGRAMS[0]["key"]
    catalog_program = seed_data.PROGRAM_BY_KEY[key]

    resp = await client.post(f"/programs/catalog/{key}")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"] == "catalog"
    assert body["source_ref"] == key
    assert len(body["days"]) == len(catalog_program["days"])

    first_day = await client.get(f"/routines/{body['days'][0]['id']}")
    assert first_day.status_code == 200
    expected_exercise_count = len(catalog_program["days"][0][1])
    assert first_day.json()["exercise_count"] == expected_exercise_count


@pytest.mark.asyncio
async def test_catalog_add_rejects_duplicate_name(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    key = seed_data.WORKOUT_PROGRAMS[0]["key"]

    first = await client.post(f"/programs/catalog/{key}")
    assert first.status_code == 201
    second = await client.post(f"/programs/catalog/{key}")
    assert second.status_code == 409
    assert second.json()["error"] == "name_taken"


@pytest.mark.asyncio
async def test_catalog_add_unknown_key_is_404(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/programs/catalog/does-not-exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_catalog_add_respects_routine_budget(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "MAX_ROUTINES_PER_USER", 0)
    client = await _linked_client(fresh_db, client_factory)
    key = seed_data.WORKOUT_PROGRAMS[0]["key"]
    resp = await client.post(f"/programs/catalog/{key}")
    assert resp.status_code == 403
    assert resp.json()["error"] == "routine_limit_reached"


# ---------- программа/день из уже сделанной тренировки ----------

async def _make_finished_workout(user_id: int, exercise_ids: list[int]) -> int:
    """Тот же приём, что и в tests/test_api_v1_account.py (не переиспользуем
    напрямую — разные тестовые файлы, каждый заводит помощник у себя)."""
    workout_id = await db.create_finished_workout(
        user_id, started_at="2024-01-01T10:00:00", finished_at="2024-01-01T11:00:00"
    )
    for ex_id in exercise_ids:
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.append_set(block_id, ex_id, 0, 50.0, 8, rpe=7.0)
    return workout_id


@pytest.mark.asyncio
async def test_routine_from_workout_repeats_its_composition(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    ex1 = await _make_exercise(fresh_db, 111, "Bench press")
    ex2 = await _make_exercise(fresh_db, 111, "Squat")
    workout_id = await _make_finished_workout(111, [ex1, ex2])

    resp = await client.post(f"/workouts/{workout_id}/routines", json={"name": "Snapshot day"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["program_id"] is None
    assert body["exercise_count"] == 2
    exercise_ids = {e["exercise_id"] for e in body["exercises"]}
    assert exercise_ids == {ex1, ex2}
    # Схема подходов подтягивается из фактически сделанного (workout_exercise_targets).
    assert all(e["target"] == "1×8" for e in body["exercises"])


@pytest.mark.asyncio
async def test_routine_from_workout_can_become_a_program_day(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    program_id = (await client.post("/programs", json={"name": "PPL"})).json()["id"]
    ex1 = await _make_exercise(fresh_db, 111, "Deadlift")
    workout_id = await _make_finished_workout(111, [ex1])

    resp = await client.post(
        f"/workouts/{workout_id}/routines", json={"name": "Pull", "program_id": program_id}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["program_id"] == program_id

    program = await client.get(f"/programs/{program_id}")
    assert [d["name"] for d in program.json()["days"]] == ["Pull"]


@pytest.mark.asyncio
async def test_routine_from_foreign_workout_is_404(fresh_db, client_factory):
    await _linked_client(fresh_db, client_factory, telegram_id=111)
    intruder = await _linked_client(fresh_db, client_factory, telegram_id=222)
    ex1 = await _make_exercise(fresh_db, 111, "Bench press")
    workout_id = await _make_finished_workout(111, [ex1])

    resp = await intruder.post(f"/workouts/{workout_id}/routines", json={"name": "Stolen"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_routine_from_workout_rejects_foreign_program(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, telegram_id=111)
    intruder = await _linked_client(fresh_db, client_factory, telegram_id=222)
    foreign_program_id = (await owner.post("/programs", json={"name": "PPL"})).json()["id"]

    ex1 = await _make_exercise(fresh_db, 222, "Bench press")
    workout_id = await _make_finished_workout(222, [ex1])

    resp = await intruder.post(
        f"/workouts/{workout_id}/routines", json={"name": "Day", "program_id": foreign_program_id}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_routine_from_workout_unknown_workout_is_404(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/workouts/999999/routines", json={"name": "Day"})
    assert resp.status_code == 404


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


# ---------- порядок дней программы («🔀 Порядок дней» у бота) ----------

@pytest.mark.asyncio
async def test_reorder_program_day_swaps_neighbours(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    program_id = (await client.post("/programs", json={"name": "PPL"})).json()["id"]
    push = (await client.post(f"/programs/{program_id}/days", json={"name": "Push"})).json()
    pull = (await client.post(f"/programs/{program_id}/days", json={"name": "Pull"})).json()

    resp = await client.post(f"/routines/{pull['id']}/reorder", json={"direction": "up"})
    assert resp.status_code == 200, resp.text
    assert [d["name"] for d in resp.json()["days"]] == ["Pull", "Push"]

    fetched = await client.get(f"/programs/{program_id}")
    assert [d["name"] for d in fetched.json()["days"]] == ["Pull", "Push"]
    assert [d["id"] for d in fetched.json()["days"]] == [pull["id"], push["id"]]


@pytest.mark.asyncio
async def test_reorder_program_day_rejects_bad_direction(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    program_id = (await client.post("/programs", json={"name": "PPL"})).json()["id"]
    day = (await client.post(f"/programs/{program_id}/days", json={"name": "Push"})).json()

    resp = await client.post(f"/routines/{day['id']}/reorder", json={"direction": "sideways"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


@pytest.mark.asyncio
async def test_reorder_standalone_routine_is_rejected(fresh_db, client_factory):
    """Одиночный день (program_id=None) переставлять не относительно чего —
    ровно как rt:daymv в боте доступен только внутри программы."""
    client = await _linked_client(fresh_db, client_factory)
    routine_id = (await client.post("/routines", json={"name": "Push day"})).json()["id"]

    resp = await client.post(f"/routines/{routine_id}/reorder", json={"direction": "up"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "not_in_program"


@pytest.mark.asyncio
async def test_reorder_program_day_foreign_is_404(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, telegram_id=111)
    intruder = await _linked_client(fresh_db, client_factory, telegram_id=222)
    program_id = (await owner.post("/programs", json={"name": "PPL"})).json()["id"]
    day = (await owner.post(f"/programs/{program_id}/days", json={"name": "Push"})).json()

    resp = await intruder.post(f"/routines/{day['id']}/reorder", json={"direction": "up"})
    assert resp.status_code == 404


# ---------- порядок упражнений дня (rt:mvex у бота) ----------

@pytest.mark.asyncio
async def test_reorder_routine_exercise_swaps_neighbours(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    routine_id = (await client.post("/routines", json={"name": "Push day"})).json()["id"]
    ex1 = await _make_exercise(fresh_db, 111, "Bench press")
    ex2 = await _make_exercise(fresh_db, 111, "Overhead press")
    item1 = (
        await client.post(f"/routines/{routine_id}/exercises", json={"exercise_id": ex1})
    ).json()
    item2 = (
        await client.post(f"/routines/{routine_id}/exercises", json={"exercise_id": ex2})
    ).json()

    resp = await client.post(f"/routine-exercises/{item2['id']}/reorder", json={"direction": "up"})
    assert resp.status_code == 200, resp.text
    assert [e["exercise_id"] for e in resp.json()["exercises"]] == [ex2, ex1]

    fetched = await client.get(f"/routines/{routine_id}")
    assert [e["id"] for e in fetched.json()["exercises"]] == [item2["id"], item1["id"]]


@pytest.mark.asyncio
async def test_reorder_routine_exercise_foreign_is_404(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, telegram_id=111)
    intruder = await _linked_client(fresh_db, client_factory, telegram_id=222)
    routine_id = (await owner.post("/routines", json={"name": "Push day"})).json()["id"]
    exercise_id = await _make_exercise(fresh_db, 111)
    item = (
        await owner.post(f"/routines/{routine_id}/exercises", json={"exercise_id": exercise_id})
    ).json()

    resp = await intruder.post(f"/routine-exercises/{item['id']}/reorder", json={"direction": "up"})
    assert resp.status_code == 404


# ---------- 401 без токена ----------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/programs"),
        ("POST", "/programs"),
        ("GET", "/programs/catalog"),
        ("POST", "/programs/catalog/ppl"),
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
        ("POST", "/routines/1/reorder"),
        ("POST", "/routines/1/exercises"),
        ("PATCH", "/routine-exercises/1"),
        ("DELETE", "/routine-exercises/1"),
        ("POST", "/routine-exercises/1/reorder"),
        ("POST", "/workouts/1/routines"),
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


async def test_concurrent_reorder_keeps_day_order_intact(fresh_db, client_factory):
    """Две одновременные перестановки не дают ни дубля, ни дыры в порядке.

    Сценарий не выдуманный: у одного человека две поверхности (бот и
    приложение), плюс двойной тап по стрелке с повтором запроса. Пока чтение
    списка было вне `_write_lock`, оба запроса читали одно состояние, каждый
    считал позиции по нему, и второй записывал числа, посчитанные по уже
    устаревшему списку — день получал чужой day_order, а освободившееся место
    оставалось пустым.

    Проверяется инвариант, а не конкретный итоговый порядок: кто из двух
    запросов лёг первым — дело планировщика, и требовать от него
    определённости значило бы проверять не то.
    """
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    program_id = await db.create_program(user_id, "Сплит")
    day_ids = [
        await db.create_routine(user_id, name, program_id=program_id)
        for name in ("Грудь", "Спина", "Ноги")
    ]

    await asyncio.gather(
        client.post(f"/routines/{day_ids[0]}/reorder", json={"direction": "up"}),
        client.post(f"/routines/{day_ids[1]}/reorder", json={"direction": "down"}),
    )

    days = await db.list_program_days_by_id(program_id)
    orders = sorted(d["day_order"] for d in days)
    assert orders == list(range(len(day_ids))), (
        f"порядок дней разъехался: {[(d['name'], d['day_order']) for d in days]}"
    )


async def test_concurrent_reorder_keeps_exercise_order_intact(fresh_db, client_factory):
    """То же для упражнений внутри дня — та же функция, та же гонка."""
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    routine_id = await db.create_routine(user_id, "День")
    group_id = await db.create_muscle_group(user_id, "Грудь")
    for index, name in enumerate(("Жим", "Разводка", "Отжимания")):
        exercise_id = await db.create_exercise(user_id, name, group_id)
        await db.add_routine_exercise(routine_id, exercise_id, index)
    # Идентификаторы строк дня заводит сама вставка — перечитываем их, а не
    # угадываем по порядку создания.
    item_ids = [item["id"] for item in await db.list_routine_exercises(routine_id)]

    await asyncio.gather(
        client.post(f"/routine-exercises/{item_ids[0]}/reorder", json={"direction": "up"}),
        client.post(f"/routine-exercises/{item_ids[1]}/reorder", json={"direction": "down"}),
    )

    items = await db.list_routine_exercises(routine_id)
    orders = sorted(item["order_index"] for item in items)
    assert orders == list(range(len(item_ids))), (
        f"порядок упражнений разъехался: {[(i['id'], i['order_index']) for i in items]}"
    )
