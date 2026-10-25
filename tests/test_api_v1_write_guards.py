"""Охрана записи в `/v1`, которую не исполнял ни один тест.

- «Добавить подход в законченную тренировку» с чужим или каталожным
  упражнением: подход лёг бы в свою тренировку под чужим id — дневник
  показал бы чужое имя, а снос того аккаунта упёрся бы во внешний ключ;
- подход без веса или без повторов (живой и в законченной): 400, а не
  «вес по умолчанию»;
- `DELETE /v1/account`, после которого в базе что-то осталось: 500, а не
  «удалено» — человеку нельзя сказать, что всё стёрто, если это не так;
- «↩️ Вернуть» удалённую тренировку дважды (двойной тап): вторая попытка —
  отказ, а не вторая копия подходов.
"""

import httpx
import pytest

import account_deletion
import api_v1

pytestmark = pytest.mark.asyncio

USER, OTHER = 111, 222


async def _client(db, telegram_id=USER):
    await db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    token = (await client.post("/auth/link", json={"code": code})).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def _finished_workout(db, user_id, exercise_id):
    workout = await db.create_finished_workout(
        user_id, started_at="2026-10-01T10:00:00", finished_at="2026-10-01T11:00:00"
    )
    block = await db.create_block(workout, "single")
    await db.add_block_exercise(block, exercise_id, 0)
    await db.add_set(block, exercise_id, 0, 0, 80.0, 8, None)
    return workout


async def _set_count(db, workout_id):
    cur = await db.conn().execute(
        "SELECT COUNT(*) FROM sets s JOIN workout_blocks b ON b.id = s.block_id WHERE b.workout_id = ?",
        (workout_id,),
    )
    return (await cur.fetchone())[0]


async def test_add_set_to_finished_workout_refuses_foreign_and_catalog_exercises(fresh_db):
    db = fresh_db
    client = await _client(db)
    await db.get_or_create_user(telegram_id=OTHER, username="other")
    mine = await db.create_exercise(USER, "Жим", await db.create_muscle_group(USER, "Грудь"))
    foreign = await db.create_exercise(OTHER, "Чужой жим", await db.create_muscle_group(OTHER, "Грудь"))
    cur = await db.conn().execute("SELECT id FROM exercises WHERE is_template = 1 LIMIT 1")
    template = (await cur.fetchone())[0]
    workout = await _finished_workout(db, USER, mine)

    for exercise_id in (foreign, template, 10**9):
        resp = await client.post(
            f"/workouts/{workout}/exercises/{exercise_id}/sets", json={"weight": 50, "reps": 5}
        )
        assert resp.status_code == 404, (exercise_id, resp.text)
    assert await _set_count(db, workout) == 1


async def test_add_set_to_finished_workout_needs_weight_and_reps(fresh_db):
    db = fresh_db
    client = await _client(db)
    mine = await db.create_exercise(USER, "Жим", await db.create_muscle_group(USER, "Грудь"))
    workout = await _finished_workout(db, USER, mine)

    for body in ({"reps": 5}, {"weight": 50}, {"weight": 50, "reps": "5"}):
        resp = await client.post(f"/workouts/{workout}/exercises/{mine}/sets", json=body)
        assert resp.status_code == 400, (body, resp.text)
    assert await _set_count(db, workout) == 1


async def test_add_set_into_someone_elses_finished_workout_is_404(fresh_db):
    db = fresh_db
    client = await _client(db)
    await db.get_or_create_user(telegram_id=OTHER, username="other")
    theirs = await db.create_exercise(OTHER, "Жим", await db.create_muscle_group(OTHER, "Грудь"))
    workout = await _finished_workout(db, OTHER, theirs)

    resp = await client.post(f"/workouts/{workout}/exercises/{theirs}/sets", json={"weight": 50, "reps": 5})

    assert resp.status_code == 404
    assert await _set_count(db, workout) == 1


async def test_live_set_needs_weight_and_reps(fresh_db):
    db = fresh_db
    client = await _client(db)
    mine = await db.create_exercise(USER, "Жим", await db.create_muscle_group(USER, "Грудь"))
    workout = await db.create_workout(USER)

    for body in ({"exercise_id": mine, "reps": 5}, {"exercise_id": mine, "weight": 50}):
        resp = await client.post(f"/workouts/{workout}/sets", json=body)
        assert resp.status_code == 400, (body, resp.text)
    assert await _set_count(db, workout) == 0


async def test_account_deletion_with_leftovers_is_500_not_deleted(fresh_db, monkeypatch):
    db = fresh_db
    client = await _client(db)

    async def partial(user_id, storage=None):
        return {"sets": 3}

    monkeypatch.setattr(account_deletion, "delete_account", partial)

    resp = await client.delete("/account", params={"confirm": "delete"})

    assert resp.status_code == 500
    assert resp.json().get("deleted") is not True


async def test_account_deletion_without_confirmation_touches_nothing(fresh_db, monkeypatch):
    db = fresh_db
    client = await _client(db)
    called = []

    async def spy(user_id, storage=None):
        called.append(user_id)
        return {}

    monkeypatch.setattr(account_deletion, "delete_account", spy)

    for params in ({}, {"confirm": "yes"}):
        assert (await client.delete("/account", params=params)).status_code == 400
    assert called == []
    assert await db.get_user(USER) is not None


async def test_restoring_a_deleted_workout_twice_does_not_duplicate(fresh_db, user_id):
    db = fresh_db
    mine = await db.create_exercise(user_id, "Жим", await db.create_muscle_group(user_id, "Грудь"))
    workout = await _finished_workout(db, user_id, mine)
    snapshot = await db.snapshot_workout(workout)
    await db.discard_workout(workout)

    assert await db.restore_workout(snapshot) is True
    assert await db.restore_workout(snapshot) is False
    assert await _set_count(db, workout) == 1


async def test_restore_refuses_a_snapshot_without_exactly_one_workout(fresh_db, user_id):
    db = fresh_db
    assert await db.restore_workout({"workouts": []}) is False
    assert await db.restore_workout({}) is False
