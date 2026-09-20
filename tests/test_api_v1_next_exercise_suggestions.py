"""`GET /exercises/next-suggestions` — подсказки на экране «упражнение не
выбрано» без плана, те же два источника, что в `handlers/workout._idle_view`
бота: «что шло следом в прошлый раз» (`_suggested_next_exercise`) и до двух
«обычно идёт после» / недавних (`db.list_common_followups`/
`db.list_recent_exercises`).
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


async def _finished_workout_with_order(user_id: int, day: int, exercise_ids: list[int]) -> int:
    """Законченная тренировка с упражнениями в заданном порядке блоков (по
    одному подходу на каждое) — ровно то, по чему `get_next_exercise_in_workout`
    решает, что шло следом.

    `last_used_at` после этого backdate-ится на дату тренировки: `db.add_set`
    ставит туда реальное время выполнения теста (сейчас), а не дату
    тренировки, и `not_used_since` (двое суток от реального «сейчас»)
    вырезал бы из подсказок вообще всё, что тест только что записал.
    """
    workout_id = await db.create_finished_workout(
        user_id, started_at=f"2026-03-{day:02d}T10:00:00", finished_at=f"2026-03-{day:02d}T11:00:00"
    )
    for ex_id in exercise_ids:
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.add_set(block_id, ex_id, round_index=1, order_in_round=0, weight=50.0, reps=8)
        await db.conn().execute(
            "UPDATE exercises SET last_used_at = ? WHERE id = ?",
            (f"2026-03-{day:02d}T11:00:00", ex_id),
        )
    return workout_id


# ---------- suggested: «что шло следом в прошлый раз» ----------


async def test_suggests_what_followed_last_time(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    a = await _exercise(user_id, "Жим лёжа")
    b = await _exercise(user_id, "Разводка гантелей")
    await _finished_workout_with_order(user_id, 1, [a, b])

    resp = await client.get("/exercises/next-suggestions", params={"last_finished_id": a})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["suggested"] == {"id": b, "name": "Разводка гантелей"}


async def test_no_suggestion_without_last_finished_id(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)

    resp = await client.get("/exercises/next-suggestions")
    assert resp.status_code == 200, resp.text
    assert resp.json()["suggested"] is None


async def test_suggestion_dropped_when_already_logged_today(fresh_db, client_factory):
    """Упражнение уже есть в done_ids этой тренировки — предлагать его снова
    незачем, оно и так на сегодняшнем списке."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    a = await _exercise(user_id, "Жим лёжа")
    b = await _exercise(user_id, "Разводка гантелей")
    await _finished_workout_with_order(user_id, 1, [a, b])

    resp = await client.get(
        "/exercises/next-suggestions", params={"last_finished_id": a, "done_ids": str(b)}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["suggested"] is None


async def test_last_finished_id_must_belong_to_user(fresh_db, client_factory):
    owner = await _exercise(222, "Чужое")
    client = await _linked_client(fresh_db, client_factory, telegram_id=111)

    resp = await client.get("/exercises/next-suggestions", params={"last_finished_id": owner})
    assert resp.status_code == 404


async def test_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/exercises/next-suggestions")
    assert resp.status_code == 401


# ---------- recent: «обычно идёт после» / просто недавние ----------


async def test_common_followups_need_at_least_two_workouts(fresh_db, client_factory):
    """Одна пара «после A шло C» — это ещё не привычка (_FOLLOWUP_MIN_WORKOUTS),
    и в recent она попасть не должна; вместо неё — просто недавние."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    a = await _exercise(user_id, "Присед")
    c = await _exercise(user_id, "Пресс")
    d = await _exercise(user_id, "Гиперэкстензия")
    await _finished_workout_with_order(user_id, 1, [a, c])
    await _finished_workout_with_order(user_id, 2, [d])

    resp = await client.get("/exercises/next-suggestions", params={"last_finished_id": a})
    assert resp.status_code == 200, resp.text
    names = {row["name"] for row in resp.json()["recent"]}
    # "Пресс" всего один раз шёл за приседом — это ниже порога привычки.
    assert "Пресс" not in names
    assert "Гиперэкстензия" in names


async def test_common_followup_appears_after_two_workouts(fresh_db, client_factory):
    """«Пресс» идёт следом за приседом (не обязательно сразу) в обеих
    тренировках — это уже привычка. Непосредственно следующим в последней
    тренировке при этом стоит «Гиперэкстензия» — она уходит в `suggested`, а
    не в `recent`, и «Пресс» этим не вытесняется."""
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    a = await _exercise(user_id, "Присед")
    c = await _exercise(user_id, "Пресс")
    d = await _exercise(user_id, "Гиперэкстензия")
    await _finished_workout_with_order(user_id, 1, [a, c])
    await _finished_workout_with_order(user_id, 2, [a, d, c])

    resp = await client.get("/exercises/next-suggestions", params={"last_finished_id": a})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["suggested"] == {"id": d, "name": "Гиперэкстензия"}
    names = {row["name"] for row in body["recent"]}
    assert "Пресс" in names


async def test_recent_excludes_done_and_suggested(fresh_db, client_factory):
    user_id = 111
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)
    a = await _exercise(user_id, "Присед")
    b = await _exercise(user_id, "Жим ногами")
    e = await _exercise(user_id, "Икры")
    await _finished_workout_with_order(user_id, 1, [a, e])

    resp = await client.get(
        "/exercises/next-suggestions", params={"last_finished_id": a, "done_ids": str(b)}
    )
    assert resp.status_code == 200, resp.text
    names = {row["name"] for row in resp.json()["recent"]}
    assert "Жим ногами" not in names
