"""``is_record`` в ответе POST /workouts/{id}/sets — живой сигнал для приложения.

Тот же вопрос, что решает 🔥-реакция бота на подход (handlers.workout,
_sets_beat_record), только не Telegram-реакция, а поле в JSON: у REST-клиента
нет своих сообщений на подход, которые можно было бы пометить. Обе стороны
теперь зовут общую view_builder.sets_beat_record — разошлись бы, будь тут
вторая реализация.

По образцу tests/test_api_v1_block_records.py: httpx поверх ASGI-приложения
без сокета, `client_factory` и `_linked_client` заведены локально.
"""

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
async def test_first_ever_set_is_not_a_record(fresh_db, client_factory):
    """Первая тренировка с упражнением — бить ещё нечего."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]

    resp = await client.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": 100, "reps": 8},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_record"] is False


@pytest.mark.asyncio
async def test_heavier_set_beats_prior_session_is_a_record(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]

    first = (await client.post("/workouts/backfill", json={"date": "2024-01-01"})).json()["id"]
    await client.post(
        f"/workouts/{first}/sets", json={"exercise_id": exercise_id, "weight": 80, "reps": 5}
    )
    await client.post(f"/workouts/{first}/finish", json={})

    second = (await client.post("/workouts/active")).json()["id"]
    resp = await client.post(
        f"/workouts/{second}/sets", json={"exercise_id": exercise_id, "weight": 120, "reps": 5}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_record"] is True


@pytest.mark.asyncio
async def test_lighter_set_does_not_beat_prior_session(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]

    first = (await client.post("/workouts/backfill", json={"date": "2024-01-01"})).json()["id"]
    await client.post(
        f"/workouts/{first}/sets", json={"exercise_id": exercise_id, "weight": 120, "reps": 5}
    )
    await client.post(f"/workouts/{first}/finish", json={})

    second = (await client.post("/workouts/active")).json()["id"]
    resp = await client.post(
        f"/workouts/{second}/sets", json={"exercise_id": exercise_id, "weight": 80, "reps": 5}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_record"] is False


def _exercise_entry(body: dict, exercise_id: int) -> dict:
    for block in body["blocks"]:
        for ex in block["exercises"]:
            if ex["exercise_id"] == exercise_id:
                return ex
    raise AssertionError(f"exercise {exercise_id} not found in {body['blocks']}")


@pytest.mark.asyncio
async def test_active_workout_marks_the_one_gold_set(fresh_db, client_factory):
    """`is_gold` в GET /workouts/active — тот же gold_index, что бот рисует 🥇
    в живом трекере (mark_golds=True): только сет, бьющий прошлый рекорд, и
    только один, даже если несколько подряд его бьют."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]

    first = (await client.post("/workouts/backfill", json={"date": "2024-01-01"})).json()["id"]
    await client.post(
        f"/workouts/{first}/sets", json={"exercise_id": exercise_id, "weight": 80, "reps": 5}
    )
    await client.post(f"/workouts/{first}/finish", json={})

    second = (await client.post("/workouts/active")).json()["id"]
    await client.post(
        f"/workouts/{second}/sets", json={"exercise_id": exercise_id, "weight": 90, "reps": 5}
    )
    await client.post(
        f"/workouts/{second}/sets", json={"exercise_id": exercise_id, "weight": 120, "reps": 5}
    )
    await client.post(
        f"/workouts/{second}/sets", json={"exercise_id": exercise_id, "weight": 60, "reps": 5}
    )

    body = (await client.get("/workouts/active")).json()
    sets = _exercise_entry(body, exercise_id)["sets"]
    assert [s["is_gold"] for s in sets] == [False, True, False]


@pytest.mark.asyncio
async def test_finished_workout_has_no_gold_marks(fresh_db, client_factory):
    """У завершённой тренировки 🥇 не рисуется — там уже record_text (🔥)."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 100, "reps": 5}
    )
    await client.post(f"/workouts/{workout_id}/finish", json={})

    body = (await client.get(f"/workouts/{workout_id}")).json()
    sets = _exercise_entry(body, exercise_id)["sets"]
    assert all(s["is_gold"] is False for s in sets)
