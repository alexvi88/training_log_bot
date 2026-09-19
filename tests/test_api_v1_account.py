"""REST `/v1` для настроек аккаунта и правки/повтора тренировки — api_v1_account.py.

По образцу tests/test_api_v1.py: httpx поверх ASGI-приложения без сокета,
`_linked_client` заведён локально (в чужой тестовый файл не лезем — его
может в это же время редактировать другой агент).
"""

import httpx
import pytest

import api_v1
import db


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


async def _make_finished_workout(user_id: int, exercise_ids: list[int]) -> int:
    """A finished workout with one single-exercise block (and one set) per
    exercise id — enough shape for edit/delete/repeat tests."""
    workout_id = await db.create_finished_workout(
        user_id, started_at="2024-01-01T10:00:00", finished_at="2024-01-01T11:00:00"
    )
    for ex_id in exercise_ids:
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.append_set(block_id, ex_id, 0, 50.0, 8, rpe=7.0)
    return workout_id


# ---------- settings: GET ----------


@pytest.mark.asyncio
async def test_get_settings_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/settings")
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_get_settings_returns_defaults(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["unit"] == "kg"
    assert body["lang"] == "ru"
    assert isinstance(body["tz_offset"], int)
    assert body["e1rm_formula"] in ("epley", "brzycki")
    for field in (
        "pushes_enabled", "ai_comments_enabled", "progression_hint_enabled",
        "food_macros_enabled", "show_extra_stats",
    ):
        assert isinstance(body[field], bool)


# ---------- settings: PATCH — happy path & partial update ----------


@pytest.mark.asyncio
async def test_patch_settings_updates_only_sent_fields(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    before = (await client.get("/settings")).json()

    resp = await client.patch("/settings", json={"lang": "en"})
    assert resp.status_code == 200, resp.text
    after = resp.json()
    assert after["lang"] == "en"
    # everything else untouched
    for key in before:
        if key != "lang":
            assert after[key] == before[key], key


@pytest.mark.asyncio
async def test_patch_settings_bool_toggle(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.patch("/settings", json={"pushes_enabled": False})
    assert resp.status_code == 200
    assert resp.json()["pushes_enabled"] is False

    resp2 = await client.get("/settings")
    assert resp2.json()["pushes_enabled"] is False


@pytest.mark.asyncio
async def test_patch_settings_unit_rescales_history(fresh_db, client_factory):
    """Switching kg->lb must rescale existing set weights, not just flip the
    column — same effect as handlers.settings.settings_unit."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await db.create_exercise(user_id, "Bench", None)
    workout_id = await _make_finished_workout(user_id, [ex_id])

    resp = await client.patch("/settings", json={"unit": "lb"})
    assert resp.status_code == 200
    assert resp.json()["unit"] == "lb"

    sets = await db.list_sets_for_exercise(ex_id)
    assert len(sets) == 1
    # 50 kg -> ~110.2 lb (db.scale_user_set_weights rounds for display)
    assert sets[0]["weight"] == pytest.approx(50.0 * 2.20462, abs=0.1)
    assert workout_id  # sanity: fixture actually created the workout


@pytest.mark.asyncio
async def test_patch_settings_tz_out_of_range_is_400_and_nothing_changes(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    before = (await client.get("/settings")).json()
    resp = await client.patch("/settings", json={"tz_offset": 100})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"
    after = (await client.get("/settings")).json()
    assert after == before


@pytest.mark.asyncio
async def test_patch_settings_invalid_unit_is_400(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.patch("/settings", json={"unit": "stone"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


@pytest.mark.asyncio
async def test_patch_settings_invalid_lang_is_400(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.patch("/settings", json={"lang": "fr"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_patch_settings_invalid_formula_is_400(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.patch("/settings", json={"e1rm_formula": "bogus"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_patch_settings_invalid_bool_type_is_400(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.patch("/settings", json={"pushes_enabled": "yes"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_patch_settings_invalid_tz_type_is_400(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.patch("/settings", json={"tz_offset": "3"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_patch_settings_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.patch("/settings", json={"lang": "en"})
    assert resp.status_code == 401


# ---------- edit a set ----------


@pytest.mark.asyncio
async def test_patch_set_changes_only_that_set(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await db.create_exercise(user_id, "Squat", None)
    workout_id = await _make_finished_workout(user_id, [ex_id])
    sets = await db.list_sets_for_exercise(ex_id)
    set_id = sets[0]["id"]

    # a second, unrelated set must stay untouched
    other_ex_id = await db.create_exercise(user_id, "Deadlift", None)
    other_workout_id = await _make_finished_workout(user_id, [other_ex_id])
    other_set_id = (await db.list_sets_for_exercise(other_ex_id))[0]["id"]

    resp = await client.patch(
        f"/workouts/{workout_id}/sets/{set_id}", json={"weight": 55.0, "reps": 5}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["weight"] == 55.0
    assert body["reps"] == 5
    assert body["rpe"] == 7.0  # not sent -> unchanged

    unrelated = await db.get_set(other_set_id)
    assert unrelated["weight"] == 50.0
    assert unrelated["reps"] == 8
    assert other_workout_id  # sanity


@pytest.mark.asyncio
async def test_patch_set_rejects_empty_body(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await db.create_exercise(user_id, "Row", None)
    workout_id = await _make_finished_workout(user_id, [ex_id])
    set_id = (await db.list_sets_for_exercise(ex_id))[0]["id"]

    resp = await client.patch(f"/workouts/{workout_id}/sets/{set_id}", json={})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_patch_set_rejects_bad_types(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await db.create_exercise(user_id, "Curl", None)
    workout_id = await _make_finished_workout(user_id, [ex_id])
    set_id = (await db.list_sets_for_exercise(ex_id))[0]["id"]

    assert (await client.patch(
        f"/workouts/{workout_id}/sets/{set_id}", json={"weight": "heavy"}
    )).status_code == 400
    assert (await client.patch(
        f"/workouts/{workout_id}/sets/{set_id}", json={"reps": 0}
    )).status_code == 400
    assert (await client.patch(
        f"/workouts/{workout_id}/sets/{set_id}", json={"rpe": "max"}
    )).status_code == 400


@pytest.mark.asyncio
async def test_patch_set_requires_auth(fresh_db, client_factory):
    user_id = 111
    await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await db.create_exercise(user_id, "Press", None)
    workout_id = await _make_finished_workout(user_id, [ex_id])
    set_id = (await db.list_sets_for_exercise(ex_id))[0]["id"]

    anon = client_factory()
    resp = await anon.patch(f"/workouts/{workout_id}/sets/{set_id}", json={"reps": 5})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_set_of_another_user_is_404(fresh_db, client_factory):
    owner_id, intruder_id = 111, 222
    await _linked_client(fresh_db, client_factory, telegram_id=owner_id)
    ex_id = await db.create_exercise(owner_id, "Lunge", None)
    workout_id = await _make_finished_workout(owner_id, [ex_id])
    set_id = (await db.list_sets_for_exercise(ex_id))[0]["id"]

    intruder = await _linked_client(fresh_db, client_factory, telegram_id=intruder_id)
    resp = await intruder.patch(f"/workouts/{workout_id}/sets/{set_id}", json={"reps": 5})
    assert resp.status_code == 404

    # the set must be untouched
    untouched = await db.get_set(set_id)
    assert untouched["reps"] == 8


@pytest.mark.asyncio
async def test_patch_set_wrong_workout_id_is_404(fresh_db, client_factory):
    """The set exists and belongs to the caller, but not to the workout named
    in the path — must not silently edit it under the wrong workout."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await db.create_exercise(user_id, "Fly", None)
    await _make_finished_workout(user_id, [ex_id])
    other_ex_id = await db.create_exercise(user_id, "Extension", None)
    other_workout_id = await _make_finished_workout(user_id, [other_ex_id])
    set_id = (await db.list_sets_for_exercise(ex_id))[0]["id"]

    resp = await client.patch(f"/workouts/{other_workout_id}/sets/{set_id}", json={"reps": 5})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_set_nonexistent_is_404(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await db.create_exercise(user_id, "Pulldown", None)
    workout_id = await _make_finished_workout(user_id, [ex_id])
    resp = await client.patch(f"/workouts/{workout_id}/sets/999999", json={"reps": 5})
    assert resp.status_code == 404


# ---------- delete a set ----------


@pytest.mark.asyncio
async def test_delete_set_removes_it(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await db.create_exercise(user_id, "Dip", None)
    workout_id = await _make_finished_workout(user_id, [ex_id])
    set_id = (await db.list_sets_for_exercise(ex_id))[0]["id"]

    resp = await client.delete(f"/workouts/{workout_id}/sets/{set_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert await db.get_set(set_id) is None


@pytest.mark.asyncio
async def test_delete_set_of_another_user_is_404_and_keeps_it(fresh_db, client_factory):
    owner_id, intruder_id = 111, 222
    await _linked_client(fresh_db, client_factory, telegram_id=owner_id)
    ex_id = await db.create_exercise(owner_id, "Shrug", None)
    workout_id = await _make_finished_workout(owner_id, [ex_id])
    set_id = (await db.list_sets_for_exercise(ex_id))[0]["id"]

    intruder = await _linked_client(fresh_db, client_factory, telegram_id=intruder_id)
    resp = await intruder.delete(f"/workouts/{workout_id}/sets/{set_id}")
    assert resp.status_code == 404
    assert await db.get_set(set_id) is not None


@pytest.mark.asyncio
async def test_delete_set_requires_auth(fresh_db, client_factory):
    user_id = 111
    await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await db.create_exercise(user_id, "Crunch", None)
    workout_id = await _make_finished_workout(user_id, [ex_id])
    set_id = (await db.list_sets_for_exercise(ex_id))[0]["id"]

    anon = client_factory()
    resp = await anon.delete(f"/workouts/{workout_id}/sets/{set_id}")
    assert resp.status_code == 401


# ---------- delete a whole workout ----------


@pytest.mark.asyncio
async def test_delete_finished_workout(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await db.create_exercise(user_id, "Plank", None)
    workout_id = await _make_finished_workout(user_id, [ex_id])

    resp = await client.delete(f"/workouts/{workout_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert await db.get_workout(workout_id) is None


@pytest.mark.asyncio
async def test_delete_active_workout_is_conflict(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    workout_id, _ = await db.get_or_create_active_workout(user_id)

    resp = await client.delete(f"/workouts/{workout_id}")
    assert resp.status_code == 409
    assert resp.json()["error"] == "workout_active"
    assert await db.get_workout(workout_id) is not None


@pytest.mark.asyncio
async def test_delete_workout_of_another_user_is_404(fresh_db, client_factory):
    owner_id, intruder_id = 111, 222
    await _linked_client(fresh_db, client_factory, telegram_id=owner_id)
    ex_id = await db.create_exercise(owner_id, "Sit-up", None)
    workout_id = await _make_finished_workout(owner_id, [ex_id])

    intruder = await _linked_client(fresh_db, client_factory, telegram_id=intruder_id)
    resp = await intruder.delete(f"/workouts/{workout_id}")
    assert resp.status_code == 404
    assert await db.get_workout(workout_id) is not None


@pytest.mark.asyncio
async def test_delete_workout_nonexistent_is_404(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.delete("/workouts/999999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_workout_requires_auth(fresh_db, client_factory):
    user_id = 111
    await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await db.create_exercise(user_id, "Burpee", None)
    workout_id = await _make_finished_workout(user_id, [ex_id])

    anon = client_factory()
    resp = await anon.delete(f"/workouts/{workout_id}")
    assert resp.status_code == 401


# ---------- repeat a workout ----------


@pytest.mark.asyncio
async def test_repeat_workout_creates_active_with_same_exercises(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex1 = await db.create_exercise(user_id, "Bench Press", None)
    ex2 = await db.create_exercise(user_id, "Overhead Press", None)
    workout_id = await _make_finished_workout(user_id, [ex1, ex2])

    resp = await client.post(f"/workouts/{workout_id}/repeat")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "active"
    new_id = body["id"]
    assert new_id != workout_id

    plan = await db.workout_plan(new_id)
    new_exercise_ids = {e for entry in plan for e in entry["exercise_ids"]}
    assert new_exercise_ids == {ex1, ex2}
    # structure only, no sets carried over
    for entry in plan:
        for ex_id in entry["exercise_ids"]:
            assert await db.list_sets_for_workout_exercise(new_id, ex_id) == []


@pytest.mark.asyncio
async def test_repeat_workout_conflicts_with_existing_active(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await db.create_exercise(user_id, "Chin-up", None)
    workout_id = await _make_finished_workout(user_id, [ex_id])
    await db.get_or_create_active_workout(user_id)  # already has an active one

    resp = await client.post(f"/workouts/{workout_id}/repeat")
    assert resp.status_code == 409
    assert resp.json()["error"] == "active_workout_exists"


@pytest.mark.asyncio
async def test_repeat_workout_with_no_exercises_is_400(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    workout_id = await db.create_finished_workout(
        user_id, started_at="2024-01-01T10:00:00", finished_at="2024-01-01T11:00:00"
    )

    resp = await client.post(f"/workouts/{workout_id}/repeat")
    assert resp.status_code == 400
    assert resp.json()["error"] == "no_exercises"


@pytest.mark.asyncio
async def test_repeat_workout_of_another_user_is_404(fresh_db, client_factory):
    owner_id, intruder_id = 111, 222
    await _linked_client(fresh_db, client_factory, telegram_id=owner_id)
    ex_id = await db.create_exercise(owner_id, "Hip Thrust", None)
    workout_id = await _make_finished_workout(owner_id, [ex_id])

    intruder = await _linked_client(fresh_db, client_factory, telegram_id=intruder_id)
    resp = await intruder.post(f"/workouts/{workout_id}/repeat")
    assert resp.status_code == 404
    assert await db.get_active_workout(intruder_id) is None


@pytest.mark.asyncio
async def test_repeat_workout_requires_auth(fresh_db, client_factory):
    user_id = 111
    await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await db.create_exercise(user_id, "Cable Row", None)
    workout_id = await _make_finished_workout(user_id, [ex_id])

    anon = client_factory()
    resp = await anon.post(f"/workouts/{workout_id}/repeat")
    assert resp.status_code == 401


# ---------- перенос тренировки на другой день ----------


@pytest.mark.asyncio
async def test_update_workout_date_keeps_time_of_day_and_duration(fresh_db, client_factory):
    """Переносится день, а не «когда именно тренировался»: стирать утро в
    полдень нельзя — на времени старта стоят значки за ранний подъём, а на
    разнице start/finish — за длинную тренировку."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await db.create_exercise(user_id, "Жим лёжа", None)
    workout_id = await _make_finished_workout(user_id, [ex_id])

    resp = await client.patch(f"/workouts/{workout_id}/date", json={"date": "2024-03-05"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["started_at"] == "2024-03-05T10:00:00"
    assert body["finished_at"] == "2024-03-05T11:00:00"


@pytest.mark.asyncio
async def test_update_workout_date_rejects_garbage(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await db.create_exercise(user_id, "Присед", None)
    workout_id = await _make_finished_workout(user_id, [ex_id])

    for bad in ("05.03.2024", "2024-13-40", 20240305, None):
        resp = await client.patch(f"/workouts/{workout_id}/date", json={"date": bad})
        assert resp.status_code == 400, bad
        assert resp.json()["error"] == "bad_request"

    unchanged = await db.get_workout(workout_id)
    assert unchanged["started_at"] == "2024-01-01T10:00:00"


@pytest.mark.asyncio
async def test_update_workout_date_does_not_touch_someone_elses(fresh_db, client_factory):
    owner_id, intruder_id = 111, 222
    await _linked_client(fresh_db, client_factory, telegram_id=owner_id)
    ex_id = await db.create_exercise(owner_id, "Тяга", None)
    workout_id = await _make_finished_workout(owner_id, [ex_id])

    intruder = await _linked_client(fresh_db, client_factory, telegram_id=intruder_id)
    resp = await intruder.patch(f"/workouts/{workout_id}/date", json={"date": "2024-03-05"})
    assert resp.status_code == 404
    assert (await db.get_workout(workout_id))["started_at"] == "2024-01-01T10:00:00"


@pytest.mark.asyncio
async def test_update_workout_date_resyncs_achievements(fresh_db, client_factory):
    """Сдвиг даты работает в обе стороны — может и достроить серию, и разорвать
    уже засчитанную, — поэтому пересчёт целиком, а не только начисление."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await db.create_exercise(user_id, "Жим лёжа", None)
    workout_id = await _make_finished_workout(user_id, [ex_id])
    await db.award_achievements(user_id, {"__never_earned__"})
    assert "__never_earned__" in await db.list_achievement_codes(user_id)

    resp = await client.patch(f"/workouts/{workout_id}/date", json={"date": "2024-03-05"})
    assert resp.status_code == 200
    assert "__never_earned__" not in await db.list_achievement_codes(user_id)
