"""`GET /exercises/{id}/superset-partners` — подсказки «⚡ партнёр по
суперсету» на экране выбора упражнения (`keyboards.py:504-507`,
`pick:partner:<id>`). Кандидатов считает `db.list_superset_partners` — тот же
вызов, что и у бота (`handlers/workout.py`, ветка `if open_ids:` в
`_picker_screen_groups`), исключения — те же две штуки: то, что открыто прямо
сейчас (`open_ids`), и всё, что в этой тренировке уже открывали
(`db.list_opened_exercise_ids_for_workout`).
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


async def _exercise(user_id: int, name: str) -> int:
    group_id = await db.create_muscle_group(user_id, "Группа")
    return await db.create_exercise(user_id, name, group_id)


async def _log_set(workout_id: int, exercise_id: int, order_in_round: int = 0) -> None:
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, exercise_id, 0)
    await db.add_set(block_id, exercise_id, round_index=1, order_in_round=order_in_round, weight=50.0, reps=8)


async def _open_only(workout_id: int, exercise_id: int) -> None:
    """Открыть вкладку без единого подхода — как `_on_exercise_chosen` в
    боте: блок уже завёлся, но `sets` в нём пока пусто."""
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, exercise_id, 0)


async def _finished_workout_pair(user_id: int, day: int, a: int, b: int) -> None:
    """Законченная тренировка, где подходы `a` и `b` шли вперемешку (A, B, A) —
    ровно то, что `db.list_superset_partners` считает суперсетом.

    Раньше здесь было по одному подходу на упражнение в одну и ту же секунду:
    окна лишь касались, а не пересекались. Строгое пересечение (см. комментарий у
    `db._RANGES_OVERLAP_SQL`) такое не засчитывает — и правильно, так выглядит
    импорт из CSV, а не чередование."""
    workout_id = await db.create_finished_workout(
        user_id, started_at=f"2026-03-{day:02d}T10:00:00", finished_at=f"2026-03-{day:02d}T11:00:00"
    )
    block_a = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_a, a, 0)
    block_b = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_b, b, 0)
    for block_id, exercise_id, minute in ((block_a, a, 0), (block_b, b, 1), (block_a, a, 2)):
        await db.conn().execute(
            "INSERT INTO sets (block_id, exercise_id, round_index, order_in_round, weight, reps, created_at) "
            "VALUES (?, ?, 1, 0, 50.0, 8, ?)",
            (block_id, exercise_id, f"2026-03-{day:02d}T10:0{minute}:00"),
        )
    await db.conn().commit()


async def test_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/exercises/1/superset-partners", params={"workout_id": 1})
    assert resp.status_code == 401


async def test_empty_without_history(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    a = await _exercise(user_id, "Жим лёжа")
    workout_id = await db.create_workout(user_id)
    await _open_only(workout_id, a)

    resp = await client.get(f"/exercises/{a}/superset-partners", params={"workout_id": workout_id})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"partners": []}


async def test_workout_id_is_required(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    a = await _exercise(user_id, "Жим лёжа")

    resp = await client.get(f"/exercises/{a}/superset-partners")
    assert resp.status_code == 400


async def test_suggests_overlapping_exercise(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    a = await _exercise(user_id, "Жим гантелей")
    b = await _exercise(user_id, "Разводка гантелей")
    await _finished_workout_pair(user_id, 1, a, b)

    workout_id = await db.create_workout(user_id)
    await _open_only(workout_id, a)

    resp = await client.get(f"/exercises/{a}/superset-partners", params={"workout_id": workout_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["partners"] == [{"id": b, "name": "Разводка гантелей"}]


async def test_excludes_currently_open_via_open_ids(fresh_db, client_factory):
    """`b` перекрывался с `a` в прошлом, но сейчас уже открыт на клиенте
    (передан в `open_ids`) — предлагать открыть его снова не нужно."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    a = await _exercise(user_id, "Жим гантелей")
    b = await _exercise(user_id, "Разводка гантелей")
    await _finished_workout_pair(user_id, 1, a, b)

    workout_id = await db.create_workout(user_id)
    await _open_only(workout_id, a)

    resp = await client.get(
        f"/exercises/{a}/superset-partners", params={"workout_id": workout_id, "open_ids": str(b)}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["partners"] == []


async def test_excludes_already_opened_this_workout(fresh_db, client_factory):
    """`b` уже открывали и закрыли в этой же тренировке (блок есть, `open_ids`
    его больше не называет) — кнопка не должна предлагать то, что человек
    только что закрыл."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    a = await _exercise(user_id, "Жим гантелей")
    b = await _exercise(user_id, "Разводка гантелей")
    await _finished_workout_pair(user_id, 1, a, b)

    workout_id = await db.create_workout(user_id)
    await _log_set(workout_id, b)  # b уже сделан и закрыт в этой тренировке
    await _open_only(workout_id, a)

    resp = await client.get(f"/exercises/{a}/superset-partners", params={"workout_id": workout_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["partners"] == []


async def test_isolated_between_users(fresh_db, client_factory):
    owner_id = 222
    a_owner = await _exercise(owner_id, "Чужое А")
    b_owner = await _exercise(owner_id, "Чужое Б")
    await _finished_workout_pair(owner_id, 1, a_owner, b_owner)
    owner_workout = await db.create_workout(owner_id)
    await _open_only(owner_workout, a_owner)

    other_client = await _linked_client(fresh_db, client_factory, telegram_id=111)

    # Чужое упражнение — вообще не ресурс этого пользователя.
    resp = await other_client.get(
        f"/exercises/{a_owner}/superset-partners", params={"workout_id": owner_workout}
    )
    assert resp.status_code == 404

    # Своё упражнение с чужой тренировкой — тоже отказ, а не подсказки по её данным.
    my_ex = await _exercise(111, "Моё")
    resp = await other_client.get(
        f"/exercises/{my_ex}/superset-partners", params={"workout_id": owner_workout}
    )
    assert resp.status_code == 404


async def test_ordered_by_overlap_frequency(fresh_db, client_factory):
    """Больше пересечений — выше в списке, ровно как `pair_count DESC` в
    `db.list_superset_partners`, и не больше двух кнопок."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    a = await _exercise(user_id, "Жим гантелей")
    frequent = await _exercise(user_id, "Разводка гантелей")
    rare = await _exercise(user_id, "Пуловер")
    await _finished_workout_pair(user_id, 1, a, frequent)
    await _finished_workout_pair(user_id, 2, a, frequent)
    await _finished_workout_pair(user_id, 3, a, rare)

    workout_id = await db.create_workout(user_id)
    await _open_only(workout_id, a)

    resp = await client.get(f"/exercises/{a}/superset-partners", params={"workout_id": workout_id})
    assert resp.status_code == 200, resp.text
    partners = resp.json()["partners"]
    assert [p["id"] for p in partners] == [frequent, rare]
    assert len(partners) <= 2
