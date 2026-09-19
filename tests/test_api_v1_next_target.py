"""REST `/v1` для подсказки прогрессии — api_v1_progress.next_target.

Главное, что здесь проверяется: приложение получает ту же подсказку, что
человек видит в боте, и НЕ считает её само. Поэтому тесты смотрят не только
на числа, но и на условия отказа: выключенный тумблер, занесение задним
числом, отсутствие истории — во всех трёх случаях подсказки нет, и это
разные причины одного и того же ответа.

По образцу tests/test_api_v1_progress.py: httpx поверх ASGI-приложения без
сокета, фикстуры заведены локально.
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
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _exercise(user_id: int, name: str = "Жим лёжа") -> int:
    group_id = await db.create_muscle_group(user_id, "Грудь")
    return await db.create_exercise(user_id, name, group_id)


async def _finished(user_id: int, ex_id: int, day: int, sets: list[tuple[float, int]]) -> int:
    workout_id = await db.create_finished_workout(
        user_id,
        started_at=f"2026-03-{day:02d}T10:00:00",
        finished_at=f"2026-03-{day:02d}T11:00:00",
    )
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for i, (weight, reps) in enumerate(sets):
        await db.add_set(
            block_id, ex_id, round_index=i + 1, order_in_round=0, weight=weight, reps=reps
        )
    return workout_id


async def _open_workout(user_id: int, status: str = "active") -> int:
    return await db.create_workout(user_id, started_at="2026-03-20T10:00:00", status=status)


async def _log_into(workout_id: int, ex_id: int, sets: list[tuple[float, int]]) -> None:
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for i, (weight, reps) in enumerate(sets):
        await db.add_set(
            block_id, ex_id, round_index=i + 1, order_in_round=0, weight=weight, reps=reps
        )


# ---------- есть что предложить ----------


async def test_hint_is_built_from_previous_session(fresh_db, client_factory):
    """Прошлый раз — 50×10, диапазон не исчерпан, значит цель тот же вес и
    больше повторов. Строка приходит готовой, числа — числами."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await _exercise(user_id)
    await _finished(user_id, ex_id, 1, [(50.0, 10), (50.0, 9)])
    workout_id = await _open_workout(user_id)

    resp = await client.get(f"/workouts/{workout_id}/exercises/{ex_id}/next-target")
    assert resp.status_code == 200, resp.text
    hint = resp.json()["hint"]

    assert hint is not None
    assert hint["text"]
    assert hint["target_weight"] >= 50.0
    assert hint["target_reps"] >= 1
    assert hint["achieved"] is False


async def test_achieved_flips_once_the_target_is_taken_today(fresh_db, client_factory):
    """Цель уже взята сегодня — бот пишет не «возьми», а «взял»; флаг обязан
    это отразить, иначе приложение будет звать сделать сделанное."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await _exercise(user_id)
    await _finished(user_id, ex_id, 1, [(50.0, 10)])
    workout_id = await _open_workout(user_id)

    before = (await client.get(f"/workouts/{workout_id}/exercises/{ex_id}/next-target")).json()["hint"]
    assert before["achieved"] is False

    await _log_into(workout_id, ex_id, [(before["target_weight"], before["target_reps"])])
    after = (await client.get(f"/workouts/{workout_id}/exercises/{ex_id}/next-target")).json()["hint"]
    assert after["achieved"] is True


async def test_todays_sets_do_not_become_the_baseline(fresh_db, client_factory):
    """Отталкиваться надо от ПРОШЛОЙ тренировки, а не от сегодняшней: иначе
    подсказка сравнивала бы сегодняшний подход сам с собой и росла бы после
    каждой записи."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await _exercise(user_id)
    await _finished(user_id, ex_id, 1, [(50.0, 10)])
    workout_id = await _open_workout(user_id)
    first = (await client.get(f"/workouts/{workout_id}/exercises/{ex_id}/next-target")).json()["hint"]

    await _log_into(workout_id, ex_id, [(80.0, 12)])
    second = (await client.get(f"/workouts/{workout_id}/exercises/{ex_id}/next-target")).json()["hint"]

    assert second["target_weight"] == first["target_weight"]
    assert second["target_reps"] == first["target_reps"]


# ---------- когда подсказки нет ----------


async def test_no_hint_without_history(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await _exercise(user_id)
    workout_id = await _open_workout(user_id)

    resp = await client.get(f"/workouts/{workout_id}/exercises/{ex_id}/next-target")
    assert resp.status_code == 200, resp.text
    assert resp.json()["hint"] is None


async def test_toggle_off_hides_the_hint(fresh_db, client_factory):
    """Выключенный тумблер прячет подсказку целиком — ровно как в боте."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await _exercise(user_id)
    await _finished(user_id, ex_id, 1, [(50.0, 10)])
    workout_id = await _open_workout(user_id)

    assert (await client.get(f"/workouts/{workout_id}/exercises/{ex_id}/next-target")).json()["hint"]
    await db.update_user(user_id, progression_hint_enabled=0)
    assert (await client.get(f"/workouts/{workout_id}/exercises/{ex_id}/next-target")).json()["hint"] is None


async def test_backfill_gets_no_hint(fresh_db, client_factory):
    """Заднее число — перенос уже случившегося, а не решение, с чем подходить
    к снаряду."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    ex_id = await _exercise(user_id)
    await _finished(user_id, ex_id, 1, [(50.0, 10)])
    workout_id = await _open_workout(user_id, status="backfill")

    resp = await client.get(f"/workouts/{workout_id}/exercises/{ex_id}/next-target")
    assert resp.status_code == 200, resp.text
    assert resp.json()["hint"] is None


# ---------- чужое и незалогиненное ----------


async def test_other_users_workout_is_not_found(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory, telegram_id=111)
    await fresh_db.get_or_create_user(telegram_id=222, username="other")
    ex_id = await _exercise(222)
    workout_id = await _open_workout(222)

    resp = await client.get(f"/workouts/{workout_id}/exercises/{ex_id}/next-target")
    assert resp.status_code == 404


async def test_without_token_it_is_unauthorized(fresh_db, client_factory):
    user_id = 111
    await fresh_db.get_or_create_user(telegram_id=user_id, username="tester")
    ex_id = await _exercise(user_id)
    workout_id = await _open_workout(user_id)

    resp = await client_factory().get(f"/workouts/{workout_id}/exercises/{ex_id}/next-target")
    assert resp.status_code == 401
