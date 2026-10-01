"""`?exclude_workout=` у сводок `/v1` — главная, посещения, прогресс упражнения.

Приложение удаляет тренировку с окном «Вернуть»: `DELETE` уходит через
несколько секунд, а строки списков прячутся сразу. Сводка главной при этом
считала удаляемую (было 12 тренировок — после удаления 13), поэтому на время
окна приложение перечитывает её с `exclude_workout`. Главный инвариант здесь —
«сводка без тренировки» байт в байт та же, что после настоящего `DELETE`:
иначе по «Вернуть»/таймеру числа на главной прыгали бы дважды.

Клиент и фабрика — локально, по образцу tests/test_api_v1_dashboard.py.
"""

import datetime as dt

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
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _exercise(user_id: int, name: str = "Bench Press") -> int:
    group_id = (await db.list_muscle_groups(None, global_only=True))[0]["id"]
    return await db.create_exercise(user_id, name, group_id)


async def _train(user_id: int, ex_id: int, days_ago: int, weight: float, sets: int = 3) -> int:
    day = dt.datetime.now() - dt.timedelta(days=days_ago)
    workout_id = await db.create_finished_workout(
        user_id, day.isoformat(), (day + dt.timedelta(hours=1)).isoformat()
    )
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for _ in range(sets):
        await db.append_set(block_id, ex_id, 0, weight, 5)
    return workout_id


def _without_echo(body):
    body = dict(body)
    body.pop("excluded_workout_ids", None)
    return body


async def _history(user_id: int) -> tuple[int, list[int]]:
    """Упражнение и тренировки: старая база, рекорд в окне, свежая удаляемая
    — чтобы исключение двигало и счётчики, и объём, и рост, и рекорды."""
    ex_id = await _exercise(user_id)
    ids = [
        await _train(user_id, ex_id, days_ago=70, weight=80.0),
        await _train(user_id, ex_id, days_ago=20, weight=90.0),
        await _train(user_id, ex_id, days_ago=10, weight=95.0),
        await _train(user_id, ex_id, days_ago=1, weight=110.0, sets=5),
    ]
    return ex_id, ids


@pytest.mark.asyncio
async def test_dashboard_without_a_workout_equals_dashboard_after_deleting_it(
    fresh_db, client_factory
):
    client = await _linked_client(fresh_db, client_factory)
    _, ids = await _history(111)
    doomed = ids[-1]

    full = (await client.get("/dashboard")).json()
    resp = await client.get("/dashboard", params={"exclude_workout": doomed})
    assert resp.status_code == 200, resp.text
    excluded = resp.json()
    assert excluded["excluded_workout_ids"] == [doomed]
    assert _without_echo(excluded) != full, "исключение должно сдвинуть сводку"

    assert (await client.delete(f"/workouts/{doomed}")).status_code in (200, 204)
    after = (await client.get("/dashboard")).json()
    assert _without_echo(excluded) == after


@pytest.mark.asyncio
async def test_dashboard_without_param_has_no_echo_key(fresh_db, client_factory):
    """Старое приложение параметр не шлёт — и ответ у него прежний."""
    client = await _linked_client(fresh_db, client_factory)
    await _history(111)

    body = (await client.get("/dashboard")).json()

    assert "excluded_workout_ids" not in body


@pytest.mark.asyncio
async def test_dashboard_is_null_when_the_only_workout_is_excluded(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    ex_id = await _exercise(111)
    only = await _train(111, ex_id, days_ago=1, weight=100.0)

    resp = await client.get("/dashboard", params={"exclude_workout": only})

    assert resp.status_code == 200
    assert resp.json() is None


@pytest.mark.asyncio
async def test_foreign_workout_id_changes_nothing(fresh_db, client_factory):
    """Фильтр — внутри запросов по своему user_id: чужой id ни на что не влияет
    и ничего о чужой тренировке не сообщает."""
    client = await _linked_client(fresh_db, client_factory)
    await _history(111)
    await fresh_db.get_or_create_user(telegram_id=222, username="other")
    other_ex = await _exercise(222, "Squat")
    foreign = await _train(222, other_ex, days_ago=1, weight=100.0)

    full = (await client.get("/dashboard")).json()
    with_foreign = (await client.get("/dashboard", params={"exclude_workout": foreign})).json()

    assert _without_echo(with_foreign) == full


@pytest.mark.asyncio
async def test_several_ids_are_excluded_together(fresh_db, client_factory):
    """Прежнее удаление ещё в полёте, а новое ждёт окна — исключаются оба."""
    client = await _linked_client(fresh_db, client_factory)
    _, ids = await _history(111)

    resp = await client.get(
        "/dashboard", params=[("exclude_workout", ids[-1]), ("exclude_workout", ids[-2])]
    )
    assert resp.json()["excluded_workout_ids"] == [ids[-1], ids[-2]]

    for workout_id in ids[-2:]:
        await client.delete(f"/workouts/{workout_id}")
    after = (await client.get("/dashboard")).json()
    assert _without_echo(resp.json()) == after


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["abc", "1.5", ""])
async def test_bad_exclude_value_is_400(fresh_db, client_factory, raw):
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.get("/dashboard", params={"exclude_workout": raw})

    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


@pytest.mark.asyncio
async def test_too_many_excluded_ids_is_400(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.get(
        "/dashboard", params=[("exclude_workout", i) for i in range(1, 13)]
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_visits_without_a_workout_equals_visits_after_deleting_it(
    fresh_db, client_factory
):
    client = await _linked_client(fresh_db, client_factory)
    _, ids = await _history(111)
    doomed = ids[-1]

    full = (await client.get("/workouts/visits")).json()
    assert "excluded_workout_ids" not in full
    excluded = (await client.get("/workouts/visits", params={"exclude_workout": doomed})).json()
    assert excluded["excluded_workout_ids"] == [doomed]
    assert sum(excluded["days"].values()) == sum(full["days"].values()) - 1

    await client.delete(f"/workouts/{doomed}")
    assert _without_echo(excluded) == (await client.get("/workouts/visits")).json()


@pytest.mark.asyncio
async def test_progress_without_a_workout_equals_progress_after_deleting_it(
    fresh_db, client_factory
):
    """Точки, тренд, рекорды и сравнение — все без удаляемой тренировки."""
    client = await _linked_client(fresh_db, client_factory)
    ex_id, ids = await _history(111)
    doomed = ids[-1]
    path = f"/exercises/{ex_id}/progress/sessions"

    full = (await client.get(path)).json()
    assert "excluded_workout_ids" not in full
    excluded = (await client.get(path, params={"exclude_workout": doomed})).json()
    assert excluded["excluded_workout_ids"] == [doomed]
    assert doomed not in [p["workout_id"] for p in excluded["points"]]
    assert excluded["records"] != full["records"], "рекорд 110 кг был в удаляемой"

    await client.delete(f"/workouts/{doomed}")
    assert _without_echo(excluded) == (await client.get(path)).json()
