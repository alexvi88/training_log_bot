"""Идемпотентность записи подхода: ``idempotency_key`` в теле запроса.

Причина появления — не защита от злоумышленника, а связь через одну в подвале:
`APIError.transport` на клиенте ловит и настоящий обрыв, и таймаут запроса,
который сервер уже принял и записал, — тогда подход уходит на повтор и
задваивается. Сервер не может отличить "правда не дошло" от "дошло, просто
ответ потерялся", поэтому дедуп делает клиент, приложив ключ попытки, а
сервер только гарантирует: один ключ (в пределах пользователя) — одна запись.

Гоняется через httpx поверх ASGI, тем же приёмом, что и tests/test_api_v1.py.
"""

import asyncio

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
async def test_log_set_retry_with_same_key_returns_same_set(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]

    body = {"exercise_id": exercise_id, "weight": 100, "reps": 8, "idempotency_key": "attempt-1"}
    first = await client.post(f"/workouts/{workout_id}/sets", json=body)
    assert first.status_code == 201
    second = await client.post(f"/workouts/{workout_id}/sets", json=body)
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]

    active = await client.get("/workouts/active")
    sets = active.json()["blocks"][0]["exercises"][0]["sets"]
    assert len(sets) == 1, "повтор с тем же ключом не должен был завести второй подход"


@pytest.mark.asyncio
async def test_log_set_different_keys_create_two_sets(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]

    a = await client.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": 100, "reps": 8, "idempotency_key": "a"},
    )
    b = await client.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": 100, "reps": 8, "idempotency_key": "b"},
    )
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["id"] != b.json()["id"]

    active = await client.get("/workouts/active")
    sets = active.json()["blocks"][0]["exercises"][0]["sets"]
    assert len(sets) == 2


@pytest.mark.asyncio
async def test_log_set_without_key_works_as_before(fresh_db, client_factory):
    """Старые клиенты ключ не шлют вовсе — поведение не должно измениться:
    каждый запрос без ключа заводит свой подход, как и раньше."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]

    a = await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 100, "reps": 8}
    )
    b = await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 100, "reps": 8}
    )
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["id"] != b.json()["id"]


@pytest.mark.asyncio
async def test_log_set_concurrent_same_key_creates_one_set(fresh_db, client_factory):
    """Гонка: два одновременных запроса с одним ключом (двойная отправка из
    очереди, если сервер ответил, а клиент решил, что нет) — должен выжить
    ровно один подход, независимо от того, кто "выиграл гонку"."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]

    body = {"exercise_id": exercise_id, "weight": 100, "reps": 8, "idempotency_key": "race-1"}
    results = await asyncio.gather(
        *(client.post(f"/workouts/{workout_id}/sets", json=body) for _ in range(5))
    )
    assert all(r.status_code == 201 for r in results)
    ids = {r.json()["id"] for r in results}
    assert len(ids) == 1, f"ожидали один и тот же id у всех попыток, получили {ids}"

    active = await client.get("/workouts/active")
    sets = active.json()["blocks"][0]["exercises"][0]["sets"]
    assert len(sets) == 1


@pytest.mark.asyncio
async def test_log_sets_from_text_retry_returns_same_sets(fresh_db, client_factory):
    """Разбор строкой может дать сразу несколько подходов ("100 8, 100 7") —
    повтор того же ключа должен вернуть все, а не только первый."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]

    body = {"exercise_id": exercise_id, "text": "100 8, 100 7", "idempotency_key": "text-1"}
    first = await client.post(f"/workouts/{workout_id}/sets/parse", json=body)
    assert first.status_code == 201
    first_ids = [s["id"] for s in first.json()["sets"]]
    assert len(first_ids) == 2

    second = await client.post(f"/workouts/{workout_id}/sets/parse", json=body)
    assert second.status_code == 201
    second_ids = [s["id"] for s in second.json()["sets"]]
    assert second_ids == first_ids

    active = await client.get("/workouts/active")
    sets = active.json()["blocks"][0]["exercises"][0]["sets"]
    assert len(sets) == 2, "повтор не должен был удвоить пачку"


@pytest.mark.asyncio
async def test_add_workout_set_retry_with_same_key_returns_same_set(fresh_db, client_factory):
    """Добавление подхода в уже завершённую тренировку (api_v1_account) —
    тот же append_set, идемпотентность работает так же."""
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111
    group_id = await fresh_db.create_muscle_group(user_id, "Ноги")
    exercise_id = await fresh_db.create_exercise(user_id, "Присед", group_id)
    workout_id = await fresh_db.create_finished_workout(
        user_id, started_at="2024-01-01T10:00:00", finished_at="2024-01-01T11:00:00"
    )
    block_id = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_block_exercise(block_id, exercise_id, 0)

    body = {"weight": 120, "reps": 5, "idempotency_key": "finished-1"}
    first = await client.post(f"/workouts/{workout_id}/exercises/{exercise_id}/sets", json=body)
    assert first.status_code == 201, first.text
    second = await client.post(f"/workouts/{workout_id}/exercises/{exercise_id}/sets", json=body)
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
