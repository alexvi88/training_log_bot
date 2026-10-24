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
async def test_first_ever_workout_of_exercise_has_no_gold(fresh_db, client_factory):
    """Упражнение впервые — `is_gold` ни на одном подходе, иначе приложение
    проиграет анимацию рекорда на каждом новом упражнении, где бить нечего."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    for weight in (60, 80, 100):
        resp = await client.post(
            f"/workouts/{workout_id}/sets",
            json={"exercise_id": exercise_id, "weight": weight, "reps": 5},
        )
        assert resp.json()["is_record"] is False

    body = (await client.get("/workouts/active")).json()
    sets = _exercise_entry(body, exercise_id)["sets"]
    assert [s["is_gold"] for s in sets] == [False, False, False]
    assert _exercise_entry(body, exercise_id)["has_record"] is False


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


async def _reps_only_exercise(client, fresh_db) -> int:
    """Подтягивания своим весом — как их заводит каталог (bodyweight_load='full'):
    вес не вводится, подход — одни повторы (seed_data.is_reps_only)."""
    exercise_id = (await client.post("/exercises", json={"name": "Подтягивания"})).json()["id"]
    await fresh_db.conn().execute(
        "UPDATE exercises SET bodyweight_load = 'full', bodyweight_factor = 1.0 WHERE id = ?",
        (exercise_id,),
    )
    await fresh_db.conn().commit()
    return exercise_id


async def _reps_only_gold(client, fresh_db, prior_reps: int, today_reps: list[int]) -> list[bool]:
    exercise_id = await _reps_only_exercise(client, fresh_db)
    first = (await client.post("/workouts/backfill", json={"date": "2024-01-01"})).json()["id"]
    resp = await client.post(
        f"/workouts/{first}/sets", json={"exercise_id": exercise_id, "weight": 0, "reps": prior_reps}
    )
    assert resp.status_code == 201, resp.text
    await client.post(f"/workouts/{first}/finish", json={})
    second = (await client.post("/workouts/active")).json()["id"]
    for reps in today_reps:
        resp = await client.post(
            f"/workouts/{second}/sets", json={"exercise_id": exercise_id, "weight": 0, "reps": reps}
        )
        assert resp.status_code == 201, resp.text
    body = (await client.get("/workouts/active")).json()
    return [s["is_gold"] for s in _exercise_entry(body, exercise_id)["sets"]]


@pytest.mark.asyncio
async def test_reps_only_gold_moves_to_the_better_set(fresh_db, client_factory):
    """Своим весом рекорд — повторы в подходе (как 🔥 у view_builder._session_record):
    12 бьёт прошлые 10 — 🥇 на нём, следом 14 — 🥇 переезжает на 14. Раньше
    🥇 считался по e1RM, а подход длиннее config.E1RM_MAX_REPS (12) в e1RM
    не участвует: 14 повторов не перебивали 12 никогда (владелец)."""
    client = await _linked_client(fresh_db, client_factory)
    assert await _reps_only_gold(client, fresh_db, 10, [12, 14]) == [False, True]


@pytest.mark.asyncio
async def test_reps_only_gold_stays_on_best_when_later_set_is_weaker(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    assert await _reps_only_gold(client, fresh_db, 10, [14, 11, 9]) == [True, False, False]


@pytest.mark.asyncio
async def test_reps_only_set_that_only_ties_prior_best_is_not_gold(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    assert await _reps_only_gold(client, fresh_db, 15, [15, 13]) == [False, False]


@pytest.mark.asyncio
async def test_reps_only_gold_moves_when_bodyweight_is_known(fresh_db, client_factory):
    """Вес тела известен — нагрузка подхода уже не 0, и e1RM-🥇 ставился на 12,
    но 14 (длиннее config.E1RM_MAX_REPS) его не забирал. По повторам — забирает."""
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.add_bodyweight_log(111, 80.0, logged_at="2023-12-01T08:00:00")
    assert await _reps_only_gold(client, fresh_db, 10, [12, 14]) == [False, True]


def test_best_reps_gold_index_rules():
    import view_builder

    assert view_builder.best_reps_gold_index([12, 14], None) is None
    assert view_builder.best_reps_gold_index([12, 14], 10) == 1
    assert view_builder.best_reps_gold_index([14, 14], 10) == 0
    assert view_builder.best_reps_gold_index([8, 10], 10) is None
