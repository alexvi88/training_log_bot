"""Упражнения без группы мышц не бывает (решение владельца).

Без группы упражнение не видно в «Моих упражнениях» (список идёт по
группам), а в недельном объёме оно стояло строкой «Без группы». Каждый путь
создания, который раньше оставлял `primary_group_id IS NULL`, теперь кладёт
его во встроенную «Другое» (или в группу, открытую в живой тренировке), а
разовая миграция v9 подбирает то, что успело накопиться.
"""
import httpx
import pytest

import ai_trainer
import api_v1
from fsm import WorkoutFlow
from handlers import workout
from seed_data import OTHER_GROUP_NAME
from tests.test_workout_picker import _make_callback, _make_message, _make_state

pytestmark = pytest.mark.asyncio


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
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _other_id(db) -> int:
    other_id = await db.other_muscle_group_id()
    assert other_id is not None
    group = await db.get_muscle_group(other_id)
    assert group["name"] == OTHER_GROUP_NAME
    assert group["user_id"] is None
    return other_id


async def _force_null_group(db, ex_id: int) -> None:
    """Старая база: упражнение, заведённое тогда, когда так ещё было можно."""
    await db.conn().execute(
        "UPDATE exercises SET primary_group_id = NULL WHERE id = ?", (ex_id,)
    )
    await db.conn().commit()


# ---------- db ----------


async def test_create_exercise_without_group_lands_in_other(fresh_db, user_id):
    ex_id = await fresh_db.create_exercise(user_id, "Своё без группы", None)
    assert (await fresh_db.get_exercise(ex_id))["primary_group_id"] == await _other_id(fresh_db)


async def test_create_exercise_keeps_an_explicit_group(fresh_db, user_id):
    gid = await fresh_db.create_muscle_group(user_id, "Своя")
    ex_id = await fresh_db.create_exercise(user_id, "Своё", gid)
    assert (await fresh_db.get_exercise(ex_id))["primary_group_id"] == gid


async def test_recreating_an_ungrouped_exercise_gives_it_the_group(fresh_db, user_id):
    ex_id = await fresh_db.create_exercise(user_id, "Старое", None)
    await _force_null_group(fresh_db, ex_id)
    gid = await fresh_db.create_muscle_group(user_id, "Своя")

    assert await fresh_db.create_exercise(user_id, "Старое", gid) == ex_id
    assert (await fresh_db.get_exercise(ex_id))["primary_group_id"] == gid


async def test_recreating_a_grouped_exercise_does_not_move_it(fresh_db, user_id):
    gid = await fresh_db.create_muscle_group(user_id, "Своя")
    ex_id = await fresh_db.create_exercise(user_id, "Своё", gid)
    other_gid = await fresh_db.create_muscle_group(user_id, "Другая своя")

    assert await fresh_db.create_exercise(user_id, "Своё", other_gid) == ex_id
    assert (await fresh_db.get_exercise(ex_id))["primary_group_id"] == gid


async def test_fork_returning_an_ungrouped_exercise_gives_it_the_template_group(fresh_db, user_id):
    cur = await fresh_db.conn().execute(
        "SELECT * FROM exercises WHERE is_template = 1 AND user_id IS NULL AND name = ?",
        ("Жим штанги лёжа",),
    )
    template = await cur.fetchone()
    ex_id = await fresh_db.fork_exercise_from_template(user_id, template["id"])
    await _force_null_group(fresh_db, ex_id)

    assert await fresh_db.fork_exercise_from_template(user_id, template["id"]) == ex_id
    row = await fresh_db.get_exercise(ex_id)
    assert row["primary_group_id"] == template["primary_group_id"]


async def test_update_exercise_group_never_clears_the_group(fresh_db, user_id):
    gid = await fresh_db.create_muscle_group(user_id, "Своя")
    ex_id = await fresh_db.create_exercise(user_id, "Своё", gid)

    await fresh_db.update_exercise_group(ex_id, None)

    assert (await fresh_db.get_exercise(ex_id))["primary_group_id"] == await _other_id(fresh_db)


# ---------- миграция v9 ----------


async def test_migration_moves_ungrouped_exercises_to_other(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Своя")
    grouped = await db.create_exercise(user_id, "С группой", gid)
    orphans = [await db.create_exercise(user_id, f"Сирота {i}", None) for i in range(2)]
    for ex_id in orphans:
        await _force_null_group(db, ex_id)

    await db.conn().execute("PRAGMA user_version = 8")
    await db.conn().commit()
    await db._run_one_shot_migrations()

    other_id = await _other_id(db)
    for ex_id in orphans:
        assert (await db.get_exercise(ex_id))["primary_group_id"] == other_id
    assert (await db.get_exercise(grouped))["primary_group_id"] == gid
    cur = await db.conn().execute("SELECT COUNT(*) FROM exercises WHERE primary_group_id IS NULL")
    assert (await cur.fetchone())[0] == 0
    cur = await db.conn().execute("PRAGMA user_version")
    assert (await cur.fetchone())[0] == db._SCHEMA_VERSION


async def test_migration_is_idempotent(fresh_db, user_id):
    db = fresh_db
    ex_id = await db.create_exercise(user_id, "Сирота", None)
    await _force_null_group(db, ex_id)

    await db._move_ungrouped_exercises_to_other()
    await db._move_ungrouped_exercises_to_other()

    assert (await db.get_exercise(ex_id))["primary_group_id"] == await _other_id(db)


# ---------- REST ----------


async def test_api_create_exercise_without_group_id_lands_in_other(fresh_db, client_factory):
    """Старые сборки iOS не шлют group_id — не отказываем, а кладём в «Другое»."""
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/exercises", json={"name": "Со старого айфона"})
    assert resp.status_code == 201, resp.text
    row = await fresh_db.get_exercise(resp.json()["id"])
    assert row["primary_group_id"] == await _other_id(fresh_db)


async def test_api_import_unmatched_exercise_lands_in_other(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)
    client = await _linked_client(fresh_db, client_factory)
    csv = "date,exercise,weight,reps\n2024-01-01,Zxq невиданное движение,50,5\n"

    resp = await client.post("/import/csv", json={"csv": csv, "create_missing_exercises": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["workouts_imported"] == 1

    row = await fresh_db.find_exercise_by_name(111, "Zxq невиданное движение")
    assert row is not None
    assert row["primary_group_id"] == await _other_id(fresh_db)


# ---------- живая тренировка в боте ----------


async def _create_from_query(db, user_id, state) -> dict:
    workout_id = await db.create_workout(user_id)
    await state.update_data(
        workout_id=workout_id, open_exercises=[], open_blocks={}, active_exercise_id=None,
        pick_query="Гиперэкстензия боком",
    )
    await state.set_state(WorkoutFlow.picking_exercise)
    await workout.pick_new_from_query(_make_callback(user_id, "pick:newquery"), state)
    return next(
        ex for ex in await db.list_user_exercises(user_id)
        if ex["display_name"] == "Гиперэкстензия боком"
    )


async def test_workout_create_without_group_context_lands_in_other(fresh_db, user_id):
    """Поиск с экрана групп или «📋 Все»: группы нет — «Другое», без вопроса."""
    state = await _make_state(user_id)
    ex = await _create_from_query(fresh_db, user_id, state)
    assert ex["primary_group_id"] == await _other_id(fresh_db)


async def test_workout_create_inside_an_open_group_uses_that_group(fresh_db, user_id):
    gid = await fresh_db.create_muscle_group(user_id, "Своя")
    state = await _make_state(user_id)
    await state.update_data(pending_group_id=gid)
    ex = await _create_from_query(fresh_db, user_id, state)
    assert ex["primary_group_id"] == gid


async def test_search_from_group_screen_forgets_a_group_left_earlier(fresh_db, user_id):
    """Открыл группу, вернулся «назад», набрал поиск на экране групп — старая
    группа не должна молча стать группой нового упражнения."""
    gid = await fresh_db.create_muscle_group(user_id, "Своя")
    state = await _make_state(user_id)
    await state.update_data(pending_group_id=gid)
    await state.set_state(WorkoutFlow.picking_group)

    await workout.pick_exercise_search(_make_message(user_id, "zxqvnonexistent"), state)

    assert (await state.get_data())["pending_group_id"] is None


async def test_search_inside_an_open_group_keeps_it(fresh_db, user_id):
    gid = await fresh_db.create_muscle_group(user_id, "Своя")
    state = await _make_state(user_id)  # picking_exercise
    await state.update_data(pending_group_id=gid)

    await workout.pick_exercise_search(_make_message(user_id, "zxqvnonexistent"), state)

    assert (await state.get_data())["pending_group_id"] == gid
