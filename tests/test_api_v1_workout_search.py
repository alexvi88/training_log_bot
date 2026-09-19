"""REST `/v1` для поиска по истории — GET /workouts/search (api_v1.search_workouts).

Тот же приём, что у tests/test_api_v1_progress.py: httpx поверх ASGI-
приложения без сокета, `client_factory`/`_linked_client` заведены локально.
"""

import httpx
import pytest

import api_v1
import db

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
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def _workout_with_exercise(user_id: int, ex_id: int, day: int) -> int:
    workout_id = await db.create_finished_workout(
        user_id,
        started_at=f"2026-03-{day:02d}T10:00:00",
        finished_at=f"2026-03-{day:02d}T11:00:00",
    )
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, round_index=1, order_in_round=0, weight=80.0, reps=8)
    return workout_id


async def test_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/workouts/search", params={"exercise": "жим"})
    assert resp.status_code == 401


async def test_empty_exercise_param_is_bad_request(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/workouts/search", params={"exercise": "  "})
    assert resp.status_code == 400


async def test_finds_workouts_with_matching_exercise(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    squat_id = await db.create_exercise(user_id, "Присед", group_id)
    w1 = await _workout_with_exercise(user_id, bench_id, day=1)
    await _workout_with_exercise(user_id, squat_id, day=2)

    resp = await client.get("/workouts/search", params={"exercise": "жим"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [w1]
    assert body["items"][0]["exercise_names"] == ["Жим лёжа"]
    assert body["items"][0]["set_count"] == 1


async def test_does_not_find_another_users_workouts(fresh_db, client_factory):
    """Поиск не должен утекать по границе пользователя — тренировка чужого
    аккаунта с тем же названием упражнения не может попасть в чужую выдачу."""
    owner_id, other_id = 111, 222
    owner_client = await _linked_client(fresh_db, client_factory, telegram_id=owner_id)
    await _linked_client(fresh_db, client_factory, telegram_id=other_id)

    other_group = await db.create_muscle_group(other_id, "Грудь")
    other_ex = await db.create_exercise(other_id, "Жим лёжа", other_group)
    await _workout_with_exercise(other_id, other_ex, day=1)

    resp = await owner_client.get("/workouts/search", params={"exercise": "жим"})
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}


async def test_pagination_offset_and_total(fresh_db, client_factory):
    """total считает ВСЕ совпадения независимо от limit — без этого старые
    тренировки частого упражнения были бы физически недостижимы."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    ids = [await _workout_with_exercise(user_id, ex_id, day=d) for d in range(1, 6)]

    first_page = await client.get(
        "/workouts/search", params={"exercise": "жим", "limit": 2, "offset": 0}
    )
    body = first_page.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    # Самые новые сначала — id последней по дате тренировки первым.
    assert body["items"][0]["id"] == ids[-1]

    second_page = await client.get(
        "/workouts/search", params={"exercise": "жим", "limit": 2, "offset": 2}
    )
    assert second_page.json()["total"] == 5
    assert len(second_page.json()["items"]) == 2
    assert {item["id"] for item in first_page.json()["items"]} & {
        item["id"] for item in second_page.json()["items"]
    } == set()
