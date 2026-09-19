"""REST `/v1` для правки уже завершённой тренировки, которой раньше не было:
добавить подход (и новое упражнение — тот же путь, что и в боте) и удалить
упражнение целиком. api_v1_account.add_workout_set / remove_workout_exercise.

Главное здесь — не сами числа, а хвост из workout_edit_data.on_workout_edited,
общий с handlers.edit_workout: пустой блок не должен застревать в истории, а
закешированный AI-комментарий — протухать молча. Без этой проверки правка из
приложения выглядела бы рабочей, но тихо портила бы то, что бот всегда чинил
попутно.

По образцу tests/test_api_v1_account.py: httpx поверх ASGI-приложения без
сокета, `_linked_client`/`_make_finished_workout` заведены локально (в чужой
тестовый файл не лезем).
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


async def _exercise(user_id: int, name: str = "Жим лёжа") -> int:
    group_id = await db.create_muscle_group(user_id, "Грудь")
    return await db.create_exercise(user_id, name, group_id)


async def _finished_workout_with_set(user_id: int, exercise_id: int) -> int:
    workout_id = await db.create_finished_workout(
        user_id, started_at="2024-01-01T10:00:00", finished_at="2024-01-01T11:00:00"
    )
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, exercise_id, 0)
    await db.append_set(block_id, exercise_id, 0, 50.0, 8, rpe=7.0)
    return workout_id


# ---------- POST .../exercises/{exercise_id}/sets ----------


@pytest.mark.asyncio
async def test_add_set_to_existing_exercise(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    ex_id = await _exercise(user_id)
    workout_id = await _finished_workout_with_set(user_id, ex_id)

    resp = await client.post(
        f"/workouts/{workout_id}/exercises/{ex_id}/sets",
        json={"weight": 55.0, "reps": 6, "rpe": 8.5},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["exercise_id"] == ex_id
    assert body["weight"] == 55.0
    assert body["reps"] == 6
    assert body["rpe"] == 8.5

    sets = []
    for block in await db.list_blocks_for_workout(workout_id):
        sets.extend(await db.list_sets_for_block(block["id"]))
    assert len(sets) == 2


@pytest.mark.asyncio
async def test_add_set_creates_new_exercise_block(fresh_db, client_factory):
    """Тот же путь, что бот проходит через «➕ Новое упражнение»: блока для
    этого упражнения в тренировке ещё нет — заводится тут же, на первый
    подход."""
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    ex1 = await _exercise(user_id, "Жим лёжа")
    ex2 = await _exercise(user_id, "Присед")
    workout_id = await _finished_workout_with_set(user_id, ex1)

    resp = await client.post(
        f"/workouts/{workout_id}/exercises/{ex2}/sets",
        json={"weight": 100.0, "reps": 5},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["exercise_id"] == ex2

    blocks = await db.list_blocks_for_workout(workout_id)
    assert len(blocks) == 2
    new_block = None
    for b in blocks:
        if any(be["exercise_id"] == ex2 for be in await db.get_block_exercises(b["id"])):
            new_block = b
            break
    assert new_block is not None
    sets = await db.list_sets_for_block(new_block["id"])
    assert len(sets) == 1
    assert sets[0]["weight"] == 100.0


@pytest.mark.asyncio
async def test_add_set_rejects_bad_weight_type(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    ex_id = await _exercise(user_id)
    workout_id = await _finished_workout_with_set(user_id, ex_id)

    resp = await client.post(
        f"/workouts/{workout_id}/exercises/{ex_id}/sets",
        json={"weight": "heavy", "reps": 5},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


@pytest.mark.asyncio
async def test_add_set_to_active_workout_is_conflict(fresh_db, client_factory):
    """Активная/заносимая задним числом тренировка пишется через POST
    /workouts/{id}/sets (api_v1.log_set) — эта ручка только для завершённых."""
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    ex_id = await _exercise(user_id)
    workout_id, _ = await db.get_or_create_active_workout(user_id)

    resp = await client.post(
        f"/workouts/{workout_id}/exercises/{ex_id}/sets",
        json={"weight": 50.0, "reps": 5},
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "workout_active"


@pytest.mark.asyncio
async def test_add_set_of_another_user_workout_is_404(fresh_db, client_factory):
    other_id = 222
    await fresh_db.get_or_create_user(telegram_id=other_id, username="other")
    ex_id = await _exercise(other_id)
    workout_id = await _finished_workout_with_set(other_id, ex_id)

    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post(
        f"/workouts/{workout_id}/exercises/{ex_id}/sets",
        json={"weight": 50.0, "reps": 5},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_add_set_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.post(
        "/workouts/1/exercises/1/sets", json={"weight": 50.0, "reps": 5}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_add_set_resets_ai_comment_and_resyncs_achievements(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    ex_id = await _exercise(user_id)
    workout_id = await _finished_workout_with_set(user_id, ex_id)
    await db.set_workout_ai_comment(workout_id, "стало сильнее с прошлого раза")

    resp = await client.post(
        f"/workouts/{workout_id}/exercises/{ex_id}/sets",
        json={"weight": 55.0, "reps": 6},
    )
    assert resp.status_code == 201, resp.text

    workout = await db.get_workout(workout_id)
    assert workout["ai_comment"] is None


# ---------- DELETE .../exercises/{exercise_id} ----------


@pytest.mark.asyncio
async def test_remove_exercise_deletes_its_sets_and_block(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    ex1 = await _exercise(user_id, "Жим лёжа")
    ex2 = await _exercise(user_id, "Присед")
    workout_id = await _finished_workout_with_set(user_id, ex1)
    block2 = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block2, ex2, 0)
    await db.append_set(block2, ex2, 0, 100.0, 5, rpe=None)

    resp = await client.delete(f"/workouts/{workout_id}/exercises/{ex2}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": True}

    blocks = await db.list_blocks_for_workout(workout_id)
    assert len(blocks) == 1
    remaining_exs = {
        be["exercise_id"] for be in await db.get_block_exercises(blocks[0]["id"])
    }
    assert remaining_exs == {ex1}


@pytest.mark.asyncio
async def test_remove_exercise_not_in_workout_is_404(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    ex1 = await _exercise(user_id, "Жим лёжа")
    ex2 = await _exercise(user_id, "Присед")
    workout_id = await _finished_workout_with_set(user_id, ex1)

    resp = await client.delete(f"/workouts/{workout_id}/exercises/{ex2}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_remove_exercise_of_another_user_is_404(fresh_db, client_factory):
    other_id = 222
    await fresh_db.get_or_create_user(telegram_id=other_id, username="other")
    ex_id = await _exercise(other_id)
    workout_id = await _finished_workout_with_set(other_id, ex_id)

    client = await _linked_client(fresh_db, client_factory)
    resp = await client.delete(f"/workouts/{workout_id}/exercises/{ex_id}")
    assert resp.status_code == 404

    # Ничего не тронуто у настоящего владельца.
    blocks = await db.list_blocks_for_workout(workout_id)
    assert len(blocks) == 1


@pytest.mark.asyncio
async def test_remove_exercise_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.delete("/workouts/1/exercises/1")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_remove_exercise_from_active_workout_is_conflict(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    ex_id = await _exercise(user_id)
    workout_id, _ = await db.get_or_create_active_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.append_set(block_id, ex_id, 0, 50.0, 5, rpe=None)

    resp = await client.delete(f"/workouts/{workout_id}/exercises/{ex_id}")
    assert resp.status_code == 409
    assert resp.json()["error"] == "workout_active"


@pytest.mark.asyncio
async def test_remove_exercise_resets_ai_comment_and_resyncs_achievements(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    ex1 = await _exercise(user_id, "Жим лёжа")
    ex2 = await _exercise(user_id, "Присед")
    workout_id = await _finished_workout_with_set(user_id, ex1)
    block2 = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block2, ex2, 0)
    await db.append_set(block2, ex2, 0, 150.0, 1, rpe=None)
    await db.set_workout_ai_comment(workout_id, "personal record on this lift")

    resp = await client.delete(f"/workouts/{workout_id}/exercises/{ex2}")
    assert resp.status_code == 200, resp.text

    workout = await db.get_workout(workout_id)
    assert workout["ai_comment"] is None


@pytest.mark.asyncio
async def test_add_then_remove_leaves_no_empty_block(fresh_db, client_factory):
    """Регрессия на саму причину задачи: добавить новое упражнение, потом
    убрать его целиком — блок не должен ни зависнуть пустым (add создаёт
    блок только на реальный подход), ни остаться после delete."""
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    ex1 = await _exercise(user_id, "Жим лёжа")
    ex2 = await _exercise(user_id, "Присед")
    workout_id = await _finished_workout_with_set(user_id, ex1)

    resp = await client.post(
        f"/workouts/{workout_id}/exercises/{ex2}/sets", json={"weight": 80.0, "reps": 5}
    )
    assert resp.status_code == 201, resp.text

    resp = await client.delete(f"/workouts/{workout_id}/exercises/{ex2}")
    assert resp.status_code == 200, resp.text

    blocks = await db.list_blocks_for_workout(workout_id)
    assert len(blocks) == 1
    for block in blocks:
        sets = await db.list_sets_for_block(block["id"])
        assert len(sets) > 0
