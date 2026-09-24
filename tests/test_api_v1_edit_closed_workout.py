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


async def _block_of(workout_id: int, exercise_id: int, weights=(50.0,)) -> int:
    """Одиночный блок упражнения с подходами — так пишут и бот, и /v1 log_set."""
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, exercise_id, 0)
    for weight in weights:
        await db.append_set(block_id, exercise_id, 0, weight, 5, rpe=None)
    return block_id


async def _exercise_ids(workout_id: int) -> set[int]:
    return {be["exercise_id"] for be in await db.list_block_exercises_for_workout(workout_id)}


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
async def test_remove_exercise_not_in_workout_is_idempotent(fresh_db, client_factory):
    """Своё упражнение, которого в этой тренировке нет (уже убрано первым
    тапом, а ответ оборвался; или его подходы так и не доехали до сервера), —
    не ошибка: итог тот, что просил клиент. Ничего не тронуто."""
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    ex1 = await _exercise(user_id, "Жим лёжа")
    ex2 = await _exercise(user_id, "Присед")
    workout_id = await _finished_workout_with_set(user_id, ex1)

    resp = await client.delete(f"/workouts/{workout_id}/exercises/{ex2}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": False}
    assert len(await db.list_blocks_for_workout(workout_id)) == 1


@pytest.mark.asyncio
async def test_remove_exercise_twice_is_200_not_error(fresh_db, client_factory):
    """Двойной тап / повтор после оборванного ответа: второй DELETE — 200."""
    client = await _linked_client(fresh_db, client_factory)
    ex1 = await _exercise(111)
    workout_id, _ = await db.get_or_create_active_workout(111)
    await _block_of(workout_id, ex1)

    first = await client.delete(f"/workouts/{workout_id}/exercises/{ex1}")
    second = await client.delete(f"/workouts/{workout_id}/exercises/{ex1}")
    assert first.status_code == 200 and first.json() == {"deleted": True}
    assert second.status_code == 200 and second.json() == {"deleted": False}


@pytest.mark.asyncio
async def test_remove_foreign_exercise_from_own_workout_is_404(fresh_db, client_factory):
    """Своя тренировка, но чужой или несуществующий exercise_id — 404."""
    other_id = 222
    await fresh_db.get_or_create_user(telegram_id=other_id, username="other")
    foreign_ex = await _exercise(other_id)
    client = await _linked_client(fresh_db, client_factory)
    ex1 = await _exercise(111)
    workout_id = await _finished_workout_with_set(111, ex1)

    resp = await client.delete(f"/workouts/{workout_id}/exercises/{foreign_ex}")
    assert resp.status_code == 404
    resp = await client.delete(f"/workouts/{workout_id}/exercises/999999")
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
async def test_remove_exercise_from_active_workout(fresh_db, client_factory):
    """Идущая тренировка — та же ручка, что и законченная: упражнение уходит
    вместе с подходами, остальное на месте. Раньше тут был 409, и убрать
    упражнение посреди тренировки приложение не могло."""
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    ex1 = await _exercise(user_id, "Жим лёжа")
    ex2 = await _exercise(user_id, "Присед")
    workout_id, _ = await db.get_or_create_active_workout(user_id)
    await _block_of(workout_id, ex1)
    await _block_of(workout_id, ex2, weights=(100.0, 100.0))

    resp = await client.delete(f"/workouts/{workout_id}/exercises/{ex2}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": True}

    assert await _exercise_ids(workout_id) == {ex1}
    sets = await db.list_sets_for_workout(workout_id)
    assert [s["exercise_id"] for s in sets] == [ex1]
    assert (await db.get_workout(workout_id))["status"] == "active"


@pytest.mark.asyncio
async def test_remove_exercise_from_backfill_workout(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    ex1 = await _exercise(111)
    workout_id, _ = await db.get_or_create_backfill_workout(111, "2024-01-01T10:00:00")
    await _block_of(workout_id, ex1)

    resp = await client.delete(f"/workouts/{workout_id}/exercises/{ex1}")
    assert resp.status_code == 200, resp.text
    assert list(await db.list_blocks_for_workout(workout_id)) == []


@pytest.mark.asyncio
async def test_remove_last_exercise_of_active_workout_leaves_it_empty(fresh_db, client_factory):
    """Убрали единственное упражнение — тренировка просто пустая и всё ещё
    идущая; что с ней делать, решает клиент (выход снимет пустую)."""
    client = await _linked_client(fresh_db, client_factory)
    ex1 = await _exercise(111)
    workout_id, _ = await db.get_or_create_active_workout(111)
    await _block_of(workout_id, ex1)

    resp = await client.delete(f"/workouts/{workout_id}/exercises/{ex1}")
    assert resp.status_code == 200, resp.text
    assert list(await db.list_blocks_for_workout(workout_id)) == []
    assert (await db.get_workout(workout_id))["status"] == "active"


@pytest.mark.asyncio
async def test_remove_superset_half_keeps_partner_block(fresh_db, client_factory):
    """Суперсет живого трекера — два одиночных блока с перемешанными по
    времени подходами. Убрали одно — блок второго цел со всеми подходами."""
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    ex1 = await _exercise(user_id, "Жим лёжа")
    ex2 = await _exercise(user_id, "Тяга")
    workout_id, _ = await db.get_or_create_active_workout(user_id)
    b1 = await db.create_block(workout_id, "single")
    await db.add_block_exercise(b1, ex1, 0)
    b2 = await db.create_block(workout_id, "single")
    await db.add_block_exercise(b2, ex2, 0)
    for _ in range(2):
        await db.append_set(b1, ex1, 0, 60.0, 8, rpe=None)
        await db.append_set(b2, ex2, 0, 50.0, 10, rpe=None)

    resp = await client.delete(f"/workouts/{workout_id}/exercises/{ex1}")
    assert resp.status_code == 200, resp.text

    assert [b["id"] for b in await db.list_blocks_for_workout(workout_id)] == [b2]
    assert len(await db.list_sets_for_block(b2)) == 2


@pytest.mark.asyncio
async def test_remove_one_exercise_of_multi_exercise_block_keeps_partner(fresh_db, client_factory):
    """Блок с двумя упражнениями (старые данные): раньше ручка сносила блок
    целиком — вместе с подходами напарника. Теперь напарник остаётся валидным
    блоком из одного упражнения со своими подходами."""
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    ex1 = await _exercise(user_id, "Жим лёжа")
    ex2 = await _exercise(user_id, "Тяга")
    workout_id = await db.create_finished_workout(
        user_id, started_at="2024-01-01T10:00:00", finished_at="2024-01-01T11:00:00"
    )
    block = await db.create_block(workout_id, "superset")
    await db.add_block_exercise(block, ex1, 0)
    await db.add_block_exercise(block, ex2, 1)
    await db.append_set(block, ex1, 0, 60.0, 8, rpe=None)
    await db.append_set(block, ex2, 1, 50.0, 10, rpe=None)

    resp = await client.delete(f"/workouts/{workout_id}/exercises/{ex1}")
    assert resp.status_code == 200, resp.text

    assert [b["id"] for b in await db.list_blocks_for_workout(workout_id)] == [block]
    assert [be["exercise_id"] for be in await db.get_block_exercises(block)] == [ex2]
    assert [s["exercise_id"] for s in await db.list_sets_for_block(block)] == [ex2]


@pytest.mark.asyncio
async def test_remove_exercise_in_active_workout_keeps_other_empty_block(fresh_db, client_factory):
    """Пустой блок другого упражнения в идущей тренировке — упражнение,
    открытое в боте, на котором человек стоит. Чистка пустых блоков — хвост
    только законченной тренировки."""
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    ex1 = await _exercise(user_id, "Жим лёжа")
    ex2 = await _exercise(user_id, "Присед")
    workout_id, _ = await db.get_or_create_active_workout(user_id)
    await _block_of(workout_id, ex1)
    opened = await _block_of(workout_id, ex2, weights=())

    resp = await client.delete(f"/workouts/{workout_id}/exercises/{ex1}")
    assert resp.status_code == 200, resp.text
    assert [b["id"] for b in await db.list_blocks_for_workout(workout_id)] == [opened]


@pytest.mark.asyncio
async def test_remove_exercise_with_app_written_sets(fresh_db, client_factory):
    """Подходы из приложения несут idempotency-запись (FK на sets) —
    удаление снимает и её, а не падает 500 на FOREIGN KEY."""
    client = await _linked_client(fresh_db, client_factory)
    ex1 = await _exercise(111)
    workout_id, _ = await db.get_or_create_active_workout(111)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex1, 0)
    await db.append_set(block_id, ex1, 0, 50.0, 5, rpe=None, user_id=111, idempotency_key="k-1")

    resp = await client.delete(f"/workouts/{workout_id}/exercises/{ex1}")
    assert resp.status_code == 200, resp.text
    assert list(await db.list_sets_for_workout(workout_id)) == []


@pytest.mark.asyncio
async def test_remove_exercise_drops_its_note_in_this_workout(fresh_db, client_factory):
    """Заметка к упражнению в этой тренировке уходит с ним: иначе, добавив
    его обратно, человек увидел бы заметку упражнения, которое убрал."""
    client = await _linked_client(fresh_db, client_factory)
    ex1 = await _exercise(111)
    workout_id, _ = await db.get_or_create_active_workout(111)
    await _block_of(workout_id, ex1)
    await db.set_workout_exercise_note(workout_id, ex1, "болит плечо")

    resp = await client.delete(f"/workouts/{workout_id}/exercises/{ex1}")
    assert resp.status_code == 200, resp.text
    assert await db.get_workout_exercise_note(workout_id, ex1) is None


@pytest.mark.asyncio
async def test_remove_exercise_from_another_users_active_workout_is_404(fresh_db, client_factory):
    other_id = 222
    await fresh_db.get_or_create_user(telegram_id=other_id, username="other")
    ex_id = await _exercise(other_id)
    workout_id, _ = await db.get_or_create_active_workout(other_id)
    await _block_of(workout_id, ex_id)

    client = await _linked_client(fresh_db, client_factory)
    resp = await client.delete(f"/workouts/{workout_id}/exercises/{ex_id}")
    assert resp.status_code == 404
    assert len(await db.list_sets_for_workout(workout_id)) == 1


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


# ---------- Форма ответа = SetLog в iOS ----------

# Ключи, без которых iOS (`SetLog` в Models.swift) не разбирает подход. Правка и
# добавление подхода в закрытую тренировку отвечали без `round_index` и
# `created_at`: сервер всё сохранял, а приложение показывало ошибку разбора, и
# повторное «Сохранить» клало подход второй раз.
_SET_LOG_KEYS = {"id", "exercise_id", "round_index", "weight", "reps", "rpe", "load_weight", "created_at"}


@pytest.mark.asyncio
async def test_add_and_update_set_answer_full_set_shape(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = await _exercise(111)
    workout_id = await _finished_workout_with_set(111, exercise_id)

    resp = await client.post(
        f"/workouts/{workout_id}/exercises/{exercise_id}/sets", json={"weight": 60, "reps": 5}
    )
    assert resp.status_code == 201, resp.text
    added = resp.json()
    assert added.keys() >= _SET_LOG_KEYS

    resp = await client.patch(f"/workouts/{workout_id}/sets/{added['id']}", json={"reps": 6})
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated.keys() >= _SET_LOG_KEYS
    assert updated["reps"] == 6
    assert updated["created_at"] == added["created_at"]
    assert updated["round_index"] == added["round_index"]
