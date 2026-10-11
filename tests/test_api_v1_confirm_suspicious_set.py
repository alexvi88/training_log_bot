"""Подозрительная строка подхода в приложении: сначала вопрос, потом запись.

«8 60» — это 8 кг × 60 повторов и мгновенный ложный рекорд. Бот такое
переспрашивает (handlers.workout._weight_confirm_prompt), `/v1` раньше писал
молча. Теперь клиент, приславший `confirm_suspicious: true`, получает 200 с
вопросом тренера и предложением поменять числа местами, а запись — только
после `confirmed: true` или исправленной строки. Без флага — как было.
"""

import httpx
import pytest

import api_v1
import set_sanity
from handlers import workout as workout_handlers
from parser import ParsedSet


async def _client(fresh_db, lang="ru"):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    if lang != "ru":
        await fresh_db.set_user_lang(111, lang)
    code = await fresh_db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _setup(client, name="Жим лёжа"):
    exercise_id = (await client.post("/exercises", json={"name": name})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    return workout_id, exercise_id


async def _post(client, workout_id, exercise_id, text, **extra):
    return await client.post(
        f"/workouts/{workout_id}/sets/parse", json={"exercise_id": exercise_id, "text": text, **extra}
    )


async def _logged(fresh_db, workout_id, exercise_id):
    return [(r["weight"], r["reps"]) for r in await fresh_db.list_sets_for_workout_exercise(workout_id, exercise_id)]


async def _attempt_rows(fresh_db) -> int:
    cur = await fresh_db.conn().execute("SELECT COUNT(*) AS n FROM set_write_attempts")
    return (await cur.fetchone())["n"]


@pytest.mark.asyncio
async def test_swapped_line_asks_first_with_suggestion_and_writes_nothing(fresh_db):
    client = await _client(fresh_db)
    workout_id, exercise_id = await _setup(client)

    resp = await _post(client, workout_id, exercise_id, "8 60", confirm_suspicious=True, idempotency_key="k1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["needs_confirmation"] is True
    assert body["sets"] == []
    assert "8×60" in body["question"]
    assert body["question"].endswith("Записываем?")
    assert body["suggestion"] == {"text": "60 8", "weight": 60.0, "reps": 8}
    assert await _logged(fresh_db, workout_id, exercise_id) == []
    # Ключ попытки не съеден: тот же ключ с подтверждением пишет как в первый раз.
    assert await _attempt_rows(fresh_db) == 0

    resp = await _post(
        client, workout_id, exercise_id, "8 60", confirm_suspicious=True, confirmed=True, idempotency_key="k1"
    )
    assert resp.status_code == 201, resp.text
    assert [(s["weight"], s["reps"]) for s in resp.json()["sets"]] == [(8, 60)]
    assert "needs_confirmation" not in resp.json()
    assert await _logged(fresh_db, workout_id, exercise_id) == [(8, 60)]


@pytest.mark.asyncio
async def test_suggested_line_is_written_without_a_second_question(fresh_db):
    client = await _client(fresh_db)
    workout_id, exercise_id = await _setup(client)
    asked = (await _post(client, workout_id, exercise_id, "8 60 @9", confirm_suspicious=True)).json()
    assert asked["suggestion"]["text"] == "60 8 @9"

    resp = await _post(client, workout_id, exercise_id, asked["suggestion"]["text"], confirm_suspicious=True)
    assert resp.status_code == 201, resp.text
    assert [(s["weight"], s["reps"], s["rpe"]) for s in resp.json()["sets"]] == [(60, 8, 9)]


@pytest.mark.asyncio
async def test_without_flag_line_is_written_as_before(fresh_db):
    client = await _client(fresh_db)
    workout_id, exercise_id = await _setup(client)
    resp = await _post(client, workout_id, exercise_id, "8 60")
    assert resp.status_code == 201, resp.text
    assert await _logged(fresh_db, workout_id, exercise_id) == [(8, 60)]


@pytest.mark.asyncio
async def test_normal_line_with_flag_is_written_straight_away(fresh_db):
    client = await _client(fresh_db)
    workout_id, exercise_id = await _setup(client)
    resp = await _post(client, workout_id, exercise_id, "60 8", confirm_suspicious=True)
    assert resp.status_code == 201, resp.text
    assert await _logged(fresh_db, workout_id, exercise_id) == [(60, 8)]


@pytest.mark.asyncio
async def test_heavy_typo_against_last_session_asks_without_suggestion(fresh_db):
    client = await _client(fresh_db, lang="en")
    workout_id, exercise_id = await _setup(client, name="Squat")
    assert (await _post(client, workout_id, exercise_id, "100 8, 100 8")).status_code == 201
    assert (await client.post(f"/workouts/{workout_id}/finish")).status_code == 200

    second = (await client.post("/workouts/active")).json()["id"]
    resp = await _post(client, second, exercise_id, "1000 5", confirm_suspicious=True)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["question"] == "⚠️ 1000kg? last time it was 100kg\nLog it?"
    assert body["suggestion"] is None
    assert await _logged(fresh_db, second, exercise_id) == []


@pytest.mark.asyncio
async def test_today_working_weight_is_the_baseline(fresh_db):
    """Восемь подходов по 200, потом «500 5» — живой случай бота, тот же здесь."""
    client = await _client(fresh_db)
    workout_id, exercise_id = await _setup(client)
    assert (await _post(client, workout_id, exercise_id, "200 5, 200 5")).status_code == 201
    resp = await _post(client, workout_id, exercise_id, "500 5", confirm_suspicious=True)
    assert resp.status_code == 200
    assert "сегодня 200" in resp.json()["question"]
    # Голые повторы берут вес прошлого подхода и тоже проходят проверку как 200×5.
    assert (await _post(client, workout_id, exercise_id, "5", confirm_suspicious=True)).status_code == 201


@pytest.mark.asyncio
async def test_units_named_in_words_skip_the_swap_question(fresh_db):
    client = await _client(fresh_db)
    workout_id, exercise_id = await _setup(client)
    resp = await _post(client, workout_id, exercise_id, "15 кг 90 раз", confirm_suspicious=True)
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_replayed_key_returns_what_was_written_instead_of_asking(fresh_db):
    client = await _client(fresh_db)
    workout_id, exercise_id = await _setup(client)
    first = await _post(client, workout_id, exercise_id, "8 60", idempotency_key="k2")
    assert first.status_code == 201
    again = await _post(client, workout_id, exercise_id, "8 60", confirm_suspicious=True, idempotency_key="k2")
    assert again.status_code == 201, again.text
    assert again.json()["sets"] == first.json()["sets"]
    assert await _logged(fresh_db, workout_id, exercise_id) == [(8, 60)]


def test_bot_keeps_using_the_shared_checks():
    """Бот спрашивает ровно то же, что и раньше, — через тот же модуль."""
    assert workout_handlers._suspicious_weight_warning is set_sanity.suspicious_weight_warning
    assert workout_handlers._suspicious_reps_warning is set_sanity.suspicious_reps_warning
    data = {"last_session_sets": {7: [(100.0, 8, None)]}}
    assert workout_handlers._weight_confirm_prompt(data, 7, [ParsedSet(weight=100, reps=8)]) is None
    assert "1000" in workout_handlers._weight_confirm_prompt(data, 7, [ParsedSet(weight=1000, reps=5)])
    assert "8×60" in workout_handlers._weight_confirm_prompt({}, 7, [ParsedSet(weight=8, reps=60)])
    assert workout_handlers._weight_confirm_prompt(
        {}, 7, [ParsedSet(weight=8, reps=60, unit_explicit=True)]
    ) is None
    # Бэкфилл: «прошлого раза» нет, сверять вес не с чем.
    assert workout_handlers._weight_confirm_prompt(
        {**data, "is_backfill": True}, 7, [ParsedSet(weight=1000, reps=5)]
    ) is None


def test_swapped():
    assert set_sanity.swapped(8, 60) == (60.0, 8)
    assert set_sanity.swapped(8.5, 60) is None
    assert set_sanity.swapped(45, 60) is None
    assert set_sanity.swapped(0, 60) is None
