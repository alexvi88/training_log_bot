"""Цель таймера отдыха: авто по каталогу, своя у упражнения, общая у атлета."""

from __future__ import annotations

import httpx

import api_v1
import rest_defaults


def test_auto_rest_is_three_minutes_for_everything():
    import seed_data

    # Владелец: «скорее 3-3-3-3» — без деления на базу, изоляцию и пресс.
    for identity in ("Присед со штангой", "Сгибание ног в тренажёре", "Планка", "Моё странное упражнение", None):
        assert rest_defaults.auto_rest_seconds(identity) == 180
    for _group, name in seed_data.EXERCISE_TEMPLATES:
        assert rest_defaults.equipment_of(name) in rest_defaults.EQUIPMENT, name


def test_effective_order_override_then_user_default_then_auto():
    row = {"original_name": "Присед со штангой", "media_from": None, "rest_seconds": None}
    assert rest_defaults.effective_rest_seconds(row) == 180
    assert rest_defaults.effective_rest_seconds(row, 75) == 75
    assert rest_defaults.effective_rest_seconds({**row, "rest_seconds": 200}, 75) == 200
    # Копия каталожного упражнения — как источник.
    copy = {"original_name": None, "media_from": "Планка", "rest_seconds": None}
    assert rest_defaults.effective_rest_seconds(copy) == 180


async def _client(fresh_db, lang="en"):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    await fresh_db.set_user_lang(111, lang)
    code = await fresh_db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _squat(client):
    templates = (await client.get("/exercise-templates?query=squat")).json()
    template = next(t for t in templates if t["original_name"] == "Присед со штангой")
    return (await client.post(f"/exercise-templates/{template['id']}/add")).json()


async def test_exercise_json_carries_rest_fields_everywhere(fresh_db):
    client = await _client(fresh_db)
    squat = await _squat(client)
    assert squat["rest_seconds"] == 180 and squat["rest_seconds_custom"] is False
    own = (await client.post("/exercises", json={"name": "Weird thing"})).json()
    assert own["rest_seconds"] == 180 and own["rest_seconds_custom"] is False
    for row in (await client.get("/exercises")).json():
        assert isinstance(row["rest_seconds"], int)
        assert isinstance(row["rest_seconds_custom"], bool)


async def test_patch_rest_seconds_set_reset_and_bounds(fresh_db):
    client = await _client(fresh_db)
    squat = await _squat(client)
    url = f"/exercises/{squat['id']}"
    body = (await client.patch(url, json={"rest_seconds": 150})).json()
    assert body["rest_seconds"] == 150 and body["rest_seconds_custom"] is True
    listed = {e["id"]: e for e in (await client.get("/exercises")).json()}
    assert listed[squat["id"]]["rest_seconds"] == 150
    body = (await client.patch(url, json={"rest_seconds": None})).json()
    assert body["rest_seconds"] == 180 and body["rest_seconds_custom"] is False
    for bad in (5, 14, 901, "90", True, 90.5):
        resp = await client.patch(url, json={"rest_seconds": bad, "name": "Renamed"})
        assert resp.status_code == 400, bad
        assert resp.json()["error"] == "invalid_rest_seconds"
        assert resp.json()["message"] == "Rest goes from 15 to 900 seconds. Enter a number, like 90."
    # Отказ ничего не поменял — даже имя из того же тела.
    assert (await client.get("/exercises")).json()[0]["display_name"] == "Barbell Squat"
    assert (await client.patch(url, json={"rest_seconds": 15})).json()["rest_seconds"] == 15
    assert (await client.patch(url, json={"rest_seconds": 900})).json()["rest_seconds"] == 900


async def test_copy_keeps_own_rest_target(fresh_db):
    client = await _client(fresh_db)
    squat = await _squat(client)
    await client.patch(f"/exercises/{squat['id']}", json={"rest_seconds": 200})
    copy = (await client.post(f"/exercises/{squat['id']}/copy", json={"name": "Squat 2"})).json()
    assert copy["rest_seconds"] == 200 and copy["rest_seconds_custom"] is True


async def test_settings_round_trip_and_user_default(fresh_db):
    client = await _client(fresh_db, lang="ru")
    settings = (await client.get("/settings")).json()
    assert settings["rest_alert"] is True
    assert settings["rest_default_seconds"] is None
    squat = await _squat(client)
    own = (await client.post("/exercises", json={"name": "Своё"})).json()
    await client.patch(f"/exercises/{own['id']}", json={"rest_seconds": 45})

    body = (await client.patch("/settings", json={"rest_alert": False, "rest_default_seconds": 100})).json()
    assert body["rest_alert"] is False and body["rest_default_seconds"] == 100
    settings = (await client.get("/settings")).json()
    assert settings["rest_alert"] is False and settings["rest_default_seconds"] == 100
    listed = {e["id"]: e for e in (await client.get("/exercises")).json()}
    assert listed[squat["id"]]["rest_seconds"] == 100
    assert listed[squat["id"]]["rest_seconds_custom"] is False
    assert listed[own["id"]]["rest_seconds"] == 45

    resp = await client.patch("/settings", json={"rest_default_seconds": 10, "rest_alert": True})
    assert resp.status_code == 400
    assert resp.json()["message"] == "Отдых ставлю от 15 до 900 секунд. Напиши число, например 90."
    assert (await client.get("/settings")).json()["rest_alert"] is False
    assert (await client.patch("/settings", json={"rest_alert": "yes"})).status_code == 400

    body = (await client.patch("/settings", json={"rest_default_seconds": None})).json()
    assert body["rest_default_seconds"] is None
    listed = {e["id"]: e for e in (await client.get("/exercises")).json()}
    assert listed[squat["id"]]["rest_seconds"] == 180
