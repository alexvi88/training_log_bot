"""Проверки тела запроса /v1, которых не хватало: отрицательные ккал/БЖУ и
`true` вместо числа в еде, NaN в весе подхода, название упражнения в 5000
символов, заметка не строкой — и идемпотентная «отмена последнего подхода»
с `set_id`.
"""

import httpx
import pytest

import api_v1
import config

pytestmark = pytest.mark.asyncio


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _client(fresh_db, client_factory, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _workout_with_exercise(client):
    wid = (await client.post("/workouts/active")).json()["id"]
    eid = (await client.post("/exercises", json={"name": "Жим"})).json()["id"]
    return wid, eid


async def _log(client, wid, eid, weight=100, reps=5):
    resp = await client.post(
        f"/workouts/{wid}/sets", json={"exercise_id": eid, "weight": weight, "reps": reps}
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def _set_ids(fresh_db, wid):
    cur = await fresh_db.conn().execute(
        "SELECT s.id FROM sets s JOIN workout_blocks b ON b.id = s.block_id "
        "WHERE b.workout_id = ? ORDER BY s.id",
        (wid,),
    )
    return [r[0] for r in await cur.fetchall()]


# ---------- еда ----------


@pytest.mark.parametrize("field", ["kcal", "protein", "fat", "carbs"])
async def test_food_rejects_negative_numbers(fresh_db, client_factory, field):
    client = await _client(fresh_db, client_factory)
    resp = await client.post("/food", json={"name": "еда", field: -9000})
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "bad_request"
    assert resp.json()["message"]
    cur = await fresh_db.conn().execute("SELECT COUNT(*) FROM food_entries")
    assert (await cur.fetchone())[0] == 0


@pytest.mark.parametrize("field", ["kcal", "protein", "fat", "carbs"])
async def test_food_rejects_bools(fresh_db, client_factory, field):
    client = await _client(fresh_db, client_factory)
    resp = await client.post("/food", json={"name": "еда", field: True})
    assert resp.status_code == 400, resp.text


async def test_food_rejects_nan(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    resp = await client.post(
        "/food", content=b'{"name": "f", "kcal": NaN}', headers={"content-type": "application/json"}
    )
    assert resp.status_code == 400, resp.text


async def test_food_still_accepts_zero_and_normal_numbers(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    resp = await client.post(
        "/food", json={"name": "еда", "kcal": 0, "protein": 12.5, "fat": None, "carbs": 30}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["calories"] == 0 and body["protein"] == 12.5 and body["fat"] is None


# ---------- вес подхода ----------


@pytest.mark.parametrize("raw", [b"NaN", b"Infinity", b"-Infinity"])
async def test_set_weight_rejects_non_finite(fresh_db, client_factory, raw):
    client = await _client(fresh_db, client_factory)
    wid, eid = await _workout_with_exercise(client)
    resp = await client.post(
        f"/workouts/{wid}/sets",
        content=b'{"exercise_id": %d, "weight": %s, "reps": 5}' % (eid, raw),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400, resp.text
    assert await _set_ids(fresh_db, wid) == []


# ---------- название упражнения ----------


async def test_create_exercise_rejects_too_long_name(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    resp = await client.post("/exercises", json={"name": "x" * 5000})
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "name_too_long"
    assert str(config.MAX_EXERCISE_NAME_LENGTH) in resp.json()["message"]

    ok = await client.post("/exercises", json={"name": "x" * config.MAX_EXERCISE_NAME_LENGTH})
    assert ok.status_code == 201, ok.text


async def test_rename_exercise_rejects_too_long_name(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    eid = (await client.post("/exercises", json={"name": "Жим"})).json()["id"]
    resp = await client.patch(f"/exercises/{eid}", json={"name": "y" * 61})
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "name_too_long"


# ---------- заметка ----------


@pytest.mark.parametrize("note", [5, {"a": 1}, ["x"], True])
async def test_finish_rejects_non_string_note(fresh_db, client_factory, note):
    client = await _client(fresh_db, client_factory)
    wid, eid = await _workout_with_exercise(client)
    await _log(client, wid, eid)
    resp = await client.post(f"/workouts/{wid}/finish", json={"note": note})
    assert resp.status_code == 400, resp.text
    workout = await fresh_db.get_workout(wid)
    assert workout["status"] == "active"


async def test_finish_rejects_too_long_note_and_accepts_normal(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    wid, eid = await _workout_with_exercise(client)
    await _log(client, wid, eid)
    too_long = "н" * (config.MAX_WORKOUT_NOTE_LENGTH + 1)
    resp = await client.post(f"/workouts/{wid}/finish", json={"note": too_long})
    assert resp.status_code == 400, resp.text
    assert (await fresh_db.get_workout(wid))["status"] == "active"

    resp = await client.post(f"/workouts/{wid}/finish", json={"note": "хорошо пошло"})
    assert resp.status_code == 200, resp.text
    assert (await fresh_db.get_workout(wid))["note"] == "хорошо пошло"


async def test_patch_note_rejects_too_long_note(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    wid, eid = await _workout_with_exercise(client)
    await _log(client, wid, eid)
    resp = await client.patch(
        f"/workouts/{wid}/note", json={"note": "x" * (config.MAX_WORKOUT_NOTE_LENGTH + 1)}
    )
    assert resp.status_code == 400, resp.text


# ---------- DELETE …/last-set с set_id ----------


async def test_last_set_retry_with_set_id_does_not_delete_a_second_set(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    wid, eid = await _workout_with_exercise(client)
    first = await _log(client, wid, eid, reps=5)
    second = await _log(client, wid, eid, reps=6)
    url = f"/workouts/{wid}/exercises/{eid}/last-set"

    resp = await client.delete(url, params={"set_id": second})
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == second
    # Повтор после потерянного ответа: удалять больше нечего.
    retry = await client.delete(url, params={"set_id": second})
    assert retry.status_code == 200, retry.text
    assert retry.json() == {"id": second, "already_deleted": True}
    assert await _set_ids(fresh_db, wid) == [first]


async def test_last_set_accepts_set_id_in_json_body(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    wid, eid = await _workout_with_exercise(client)
    only = await _log(client, wid, eid)
    url = f"/workouts/{wid}/exercises/{eid}/last-set"

    resp = await client.request("DELETE", url, json={"set_id": only})
    assert resp.status_code == 200, resp.text
    # Блок опустел и снят — повтор всё равно 200, а не 404.
    retry = await client.request("DELETE", url, json={"set_id": only})
    assert retry.status_code == 200, retry.text
    assert await _set_ids(fresh_db, wid) == []


async def test_last_set_with_stale_set_id_is_409_and_deletes_nothing(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    wid, eid = await _workout_with_exercise(client)
    first = await _log(client, wid, eid, reps=5)
    second = await _log(client, wid, eid, reps=6)

    resp = await client.delete(
        f"/workouts/{wid}/exercises/{eid}/last-set", params={"set_id": first}
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == "set_not_last"
    assert resp.json()["message"]
    assert await _set_ids(fresh_db, wid) == [first, second]


async def test_last_set_with_set_of_another_exercise_is_404(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    wid, eid = await _workout_with_exercise(client)
    other = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    await _log(client, wid, eid)
    other_set = await _log(client, wid, other)

    resp = await client.delete(
        f"/workouts/{wid}/exercises/{eid}/last-set", params={"set_id": other_set}
    )
    assert resp.status_code == 404, resp.text
    assert other_set in await _set_ids(fresh_db, wid)


async def test_last_set_rejects_non_int_set_id(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    wid, eid = await _workout_with_exercise(client)
    await _log(client, wid, eid)
    resp = await client.delete(
        f"/workouts/{wid}/exercises/{eid}/last-set", params={"set_id": "abc"}
    )
    assert resp.status_code == 400


async def test_last_set_without_set_id_keeps_old_behaviour(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    wid, eid = await _workout_with_exercise(client)
    first = await _log(client, wid, eid, reps=5)
    await _log(client, wid, eid, reps=6)
    url = f"/workouts/{wid}/exercises/{eid}/last-set"

    assert (await client.delete(url)).status_code == 200
    assert await _set_ids(fresh_db, wid) == [first]
