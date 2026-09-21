"""`group_tag` в JSON блока упражнения (GET /v1/workouts/{id} и активная/
бэкофилл-тренировка) — тег группы мышц, готовый к печати клиентом без
дополнительных запросов (см. formatting.format_group_tag и CLAUDE.md
приложения). Раньше клиент тянул api.exercises() + api.muscleGroups() ради
одного тега на экране предупреждения о зависшей тренировке — теперь сервер
отдаёт готовую строку прямо в блоке.

По образцу tests/test_api_v1_block_records.py: httpx поверх ASGI-приложения
без сокета, `client_factory` и `_linked_client` заведены локально.
"""

import httpx
import pytest

import api_v1
import formatting


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


def _exercise_entry(body: dict, exercise_id: int) -> dict:
    for block in body["blocks"]:
        for ex in block["exercises"]:
            if ex["exercise_id"] == exercise_id:
                return ex
    raise AssertionError(f"exercise {exercise_id} not found in {body['blocks']}")


async def _log_set(client, workout_id: int, exercise_id: int, weight: float, reps: int):
    resp = await client.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": weight, "reps": reps},
    )
    assert resp.status_code in (200, 201), resp.text


@pytest.mark.asyncio
async def test_group_tag_matches_exercise_group_on_active_workout(fresh_db, client_factory):
    """Ровно то же название, что и у самой группы — предупреждение о зависшей
    тренировке показывается именно для незакрытой, поэтому проверяем активную,
    без user в _workout_detail_json (та же ветка, что GET активной у бота)."""
    client = await _linked_client(fresh_db, client_factory)
    group_id = (await client.get("/muscle-groups")).json()[0]["id"]
    group_name = (await client.get("/muscle-groups")).json()[0]["name"]
    exercise_id = (
        await client.post("/exercises", json={"name": "Жим лёжа", "group_id": group_id})
    ).json()["id"]

    workout_id = (await client.post("/workouts/active")).json()["id"]
    await _log_set(client, workout_id, exercise_id, 80, 5)

    body = (await client.get(f"/workouts/{workout_id}")).json()
    entry = _exercise_entry(body, exercise_id)
    assert entry["group_tag"] == formatting.format_group_tag(group_name)


@pytest.mark.asyncio
async def test_group_tag_present_on_backfill_workout(fresh_db, client_factory):
    """Бэкофилл-тренировка — тоже незакрытая до финиша, тег должен быть
    доступен и там же (то, ради чего заведено поле)."""
    client = await _linked_client(fresh_db, client_factory)
    group_id = (await client.get("/muscle-groups")).json()[0]["id"]
    group_name = (await client.get("/muscle-groups")).json()[0]["name"]
    exercise_id = (
        await client.post("/exercises", json={"name": "Присед", "group_id": group_id})
    ).json()["id"]

    workout_id = (
        await client.post("/workouts/backfill", json={"date": "2024-01-01"})
    ).json()["id"]
    await _log_set(client, workout_id, exercise_id, 80, 5)

    body = (await client.get(f"/workouts/{workout_id}")).json()
    entry = _exercise_entry(body, exercise_id)
    assert entry["group_tag"] == formatting.format_group_tag(group_name)


@pytest.mark.asyncio
async def test_group_tag_null_when_exercise_has_no_group(fresh_db, client_factory):
    """Упражнение без группы (`group_id` не передан) — `group_tag` держит
    `null`, а не падает и не подставляет пустую строку."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Своё без группы"})).json()["id"]

    workout_id = (await client.post("/workouts/active")).json()["id"]
    await _log_set(client, workout_id, exercise_id, 80, 5)

    body = (await client.get(f"/workouts/{workout_id}")).json()
    entry = _exercise_entry(body, exercise_id)
    assert entry["group_tag"] is None


@pytest.mark.asyncio
async def test_group_tag_isolated_between_users(fresh_db, client_factory):
    """Своя группа одного пользователя не протекает в ответ другого —
    у каждого свой набор упражнений и своя видимая тренировка."""
    alice = await _linked_client(fresh_db, client_factory, telegram_id=201)
    bob = await _linked_client(fresh_db, client_factory, telegram_id=202)

    own_group_id = (
        await alice.post("/muscle-groups", json={"name": "Своя группа Алисы"})
    ).json()["id"]
    alice_exercise = (
        await alice.post("/exercises", json={"name": "Алисино упражнение", "group_id": own_group_id})
    ).json()["id"]
    alice_workout = (await alice.post("/workouts/active")).json()["id"]
    await _log_set(alice, alice_workout, alice_exercise, 50, 5)

    bob_group_id = (await bob.get("/muscle-groups")).json()[0]["id"]
    bob_group_name = (await bob.get("/muscle-groups")).json()[0]["name"]
    bob_exercise = (
        await bob.post("/exercises", json={"name": "Бобово упражнение", "group_id": bob_group_id})
    ).json()["id"]
    bob_workout = (await bob.post("/workouts/active")).json()["id"]
    await _log_set(bob, bob_workout, bob_exercise, 60, 5)

    alice_body = (await alice.get(f"/workouts/{alice_workout}")).json()
    alice_entry = _exercise_entry(alice_body, alice_exercise)
    assert alice_entry["group_tag"] == formatting.format_group_tag("Своя группа Алисы")

    bob_body = (await bob.get(f"/workouts/{bob_workout}")).json()
    bob_entry = _exercise_entry(bob_body, bob_exercise)
    assert bob_entry["group_tag"] == formatting.format_group_tag(bob_group_name)

    # Бобу недоступна тренировка Алисы вовсе — своя группа не протекла бы
    # через чужой ответ, даже если бы протекла в саму видимость тренировки.
    forbidden = await bob.get(f"/workouts/{alice_workout}")
    assert forbidden.status_code in (403, 404)
