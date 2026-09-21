"""Строка e1RM и эмодзи звания в JSON тренировки — сверка карточки бота и
приложения (шесть расхождений, два из них требуют этих полей).

По образцу tests/test_api_v1_block_records.py: httpx поверх ASGI-приложения
без сокета, `client_factory` и `_linked_client` заведены локально — в чужой
тестовый файл не лезем, его может в это же время править другой агент.
"""

import datetime as dt

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


# ---------- e1rm_text ----------


@pytest.mark.asyncio
async def test_e1rm_text_present_on_finished_workout_with_extra_stats(fresh_db, client_factory):
    """Готовая строка «↳ e1RM …» — тем же путём, что формулирует бот
    (formatting.format_block_e1rm / формат из formatting.py)."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await _log_set(client, workout_id, exercise_id, 100, 5)

    body = (await client.post(f"/workouts/{workout_id}/finish", json={})).json()
    entry = _exercise_entry(body, exercise_id)
    assert entry["e1rm_text"]
    assert entry["e1rm_text"].startswith("↳ e1RM")

    get_body = (await client.get(f"/workouts/{workout_id}")).json()
    assert _exercise_entry(get_body, exercise_id)["e1rm_text"] == entry["e1rm_text"]


@pytest.mark.asyncio
async def test_e1rm_text_absent_with_show_extra_stats_off(fresh_db, client_factory):
    """Формула e1RM и порог зависят от настроек аккаунта — при выключенной
    расширенной статистике поле молчит, как и record_text (docstring
    formatting.format_block_e1rm)."""
    client = await _linked_client(fresh_db, client_factory)
    await client.patch("/settings", json={"show_extra_stats": False})
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await _log_set(client, workout_id, exercise_id, 100, 5)

    body = (await client.post(f"/workouts/{workout_id}/finish", json={})).json()
    entry = _exercise_entry(body, exercise_id)
    assert entry["e1rm_text"] is None


@pytest.mark.asyncio
async def test_e1rm_text_absent_on_unfinished_workout(fresh_db, client_factory):
    """У ещё идущей тренировки e1RM-строки нет вовсе — как в боте, где она
    есть только в итоговом тексте карточки завершения, а не в live-трекере."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await _log_set(client, workout_id, exercise_id, 100, 5)

    body = (await client.get(f"/workouts/{workout_id}")).json()
    entry = _exercise_entry(body, exercise_id)
    assert entry["e1rm_text"] is None


@pytest.mark.asyncio
async def test_e1rm_text_is_isolated_between_users(fresh_db, client_factory):
    """Чужие настройки show_extra_stats и чужие тренировки не протекают друг
    в друга через это поле."""
    a = await _linked_client(fresh_db, client_factory, telegram_id=201)
    b = await _linked_client(fresh_db, client_factory, telegram_id=202)
    await b.patch("/settings", json={"show_extra_stats": False})

    ex_a = (await a.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    ex_b = (await b.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]

    wa = (await a.post("/workouts/active")).json()["id"]
    await _log_set(a, wa, ex_a, 100, 5)
    body_a = (await a.post(f"/workouts/{wa}/finish", json={})).json()
    assert _exercise_entry(body_a, ex_a)["e1rm_text"]

    wb = (await b.post("/workouts/active")).json()["id"]
    await _log_set(b, wb, ex_b, 100, 5)
    body_b = (await b.post(f"/workouts/{wb}/finish", json={})).json()
    assert _exercise_entry(body_b, ex_b)["e1rm_text"] is None


# ---------- rank_promotion.emoji ----------


async def _finished(db, user_id: int, when: dt.date, sets):
    """Заводит и сразу закрывает тренировку напрямую через db — по образцу
    tests/test_ranks.py:_finished, чтобы дособрать историю без REST-полёта на
    каждый подход."""
    workout_id = await db.create_workout(user_id, started_at=f"{when.isoformat()}T10:00:00")
    gid = await db.create_muscle_group(user_id, f"Г{when.isoformat()}")
    ex_id = await db.create_exercise(user_id, f"Упр {when.isoformat()}", gid)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for weight, reps in sets:
        await db.append_set(block_id, ex_id, 0, weight, reps)
    await db.finish_workout(workout_id, finished_at=f"{when.isoformat()}T11:00:00")


@pytest.mark.asyncio
async def test_rank_promotion_carries_the_ranks_own_emoji(fresh_db, client_factory):
    """rank.promotion бота — «🎖 <b>Новое звание: {emoji} {name}</b>»
    (locales/ru.json) — печатает СВОЙ эмодзи звания (analytics.Rank.emoji), не
    только общий 🎖. REST обязан отдавать то же самое поле."""
    client = await _linked_client(fresh_db, client_factory)
    user_id = 111

    today = dt.date.today()
    for i in range(6):
        await _finished(fresh_db, user_id, today - dt.timedelta(days=i * 3 + 1), [(100.0, 10)] * 6)

    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await _log_set(client, workout_id, exercise_id, 100, 10)
    body = (await client.post(f"/workouts/{workout_id}/finish", json={})).json()

    promotion = body["rewards"]["rank_promotion"]
    assert promotion is not None, body["rewards"]
    assert promotion["emoji"]
    assert isinstance(promotion["emoji"], str)
    assert promotion["level"] > 0
