"""«🎯 Цель» для упражнения без единого подхода в дневнике.

Строка не называет вес (брать его не от чего), а говорит, как его найти: вес на
столько повторов, сколько стоит в настройке «Диапазон повторов». Бот и /v1
берут её из одного места — `progression_data.no_history_hint`.
"""

import json

import httpx
import pytest

import api_v1
import db
import i18n
import progression_data
from handlers.workout import _logging_hint

RU_DEFAULT = "🎯 Цель: вес, с которым сделаешь 5–12 раз. Последние повторы — тяжело, но чисто."
EN_DEFAULT = "🎯 Goal: a weight you can do 5–12 reps with. The last reps should be hard but clean."


# ---------- живой трекер в боте ----------

def test_bot_shows_the_range_line_without_history():
    text = _logging_hint(None, False, show_instruction=False)
    assert RU_DEFAULT in text
    assert "прошлого раза" not in text and "не знаешь" not in text


def test_bot_uses_the_athletes_rep_range():
    text = _logging_hint(None, False, show_instruction=False, rep_range=(8, 15))
    assert "сделаешь 8–15 раз" in text
    with i18n.use_lang("en"):
        text = _logging_hint(None, False, show_instruction=False, rep_range=(8, 15))
    assert "a weight you can do 8–15 reps with" in text


def test_bot_english_default():
    with i18n.use_lang("en"):
        assert EN_DEFAULT in _logging_hint(None, False, show_instruction=False)


def test_bot_with_history_is_as_before():
    text = _logging_hint([(100.0, 8, None)], False, show_instruction=False)
    assert "Цель: 100кг×9" in text or "🎯 Цель: 100" in text
    assert "вес, с которым сделаешь" not in text


def test_bot_scheme_wins_over_the_range_line():
    text = _logging_hint(None, False, show_instruction=False, target="3×6–12")
    assert "План" in text and "вес, с которым сделаешь" not in text


def test_bot_program_rule_wins_over_the_range_line():
    rule = {"rule": "double_progression", "reps_top": 8, "step": 2.5}
    text = _logging_hint(None, False, show_instruction=False, progression_rule=rule)
    assert "вес, с которым сделаешь" not in text


@pytest.mark.parametrize("kind", ["timed", "bodyweight", "no_load"])
def test_bot_no_line_for_time_and_bodyweight(kind):
    text = _logging_hint(None, False, show_instruction=False, progression_kind=kind)
    assert "🎯" not in text


def test_bot_no_line_when_the_toggle_is_off_or_a_set_is_logged():
    assert "🎯" not in _logging_hint(None, False, show_instruction=False, show_progression=False)
    assert "🎯" not in _logging_hint(None, True, show_instruction=False, today_sets=[(50.0, 8)])


# ---------- /v1 ----------

@pytest.fixture
def client_factory():
    def _make():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test"
        )

    return _make


async def _client(fresh_db, client_factory, lang="ru"):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester", language_code=lang)
    code = await fresh_db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    client = client_factory()
    token = (await client.post("/auth/link", json={"code": code})).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def _hint(client, workout_id, ex_id):
    resp = await client.get(f"/workouts/{workout_id}/exercises/{ex_id}/next-target")
    assert resp.status_code == 200, resp.text
    return resp.json()["hint"]


async def _setup(name="Тяга блока", routine=False):
    group_id = await db.create_muscle_group(111, "Спина")
    ex_id = await db.create_exercise(111, name, group_id)
    routine_id = await db.create_routine(111, "День 1") if routine else None
    if routine:
        await db.append_routine_exercise(routine_id, ex_id, "3×8–12")
    workout_id = await db.create_workout(
        111, started_at="2026-03-02T10:00:00", routine_id=routine_id
    )
    return ex_id, workout_id


@pytest.mark.parametrize("lang,default", [("ru", RU_DEFAULT), ("en", EN_DEFAULT)])
@pytest.mark.asyncio
async def test_api_returns_the_line_for_both_languages(fresh_db, client_factory, lang, default):
    client = await _client(fresh_db, client_factory, lang)
    ex_id, workout_id = await _setup()
    hint = await _hint(client, workout_id, ex_id)
    assert hint["text"] == default
    assert hint["no_history"] is True and hint["achieved"] is False
    # Поля старой формы на месте — прежние сборки приложения декодируют ответ.
    for key in ("target_weight", "target_reps", "is_bodyweight", "is_deload", "role"):
        assert key in hint


@pytest.mark.asyncio
async def test_api_uses_custom_rep_range(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    await db.conn().execute(
        "UPDATE users SET rep_range_min = 8, rep_range_max = 15 WHERE telegram_id = 111"
    )
    await db.conn().commit()
    ex_id, workout_id = await _setup()
    assert "сделаешь 8–15 раз" in (await _hint(client, workout_id, ex_id))["text"]


@pytest.mark.asyncio
async def test_api_line_goes_away_after_the_first_set(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex_id, workout_id = await _setup()
    block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block, ex_id, 0)
    await db.add_set(block, ex_id, round_index=1, order_in_round=0, weight=40.0, reps=10)
    assert await _hint(client, workout_id, ex_id) is None


@pytest.mark.asyncio
async def test_api_with_history_is_as_before(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex_id, workout_id = await _setup()
    past = await db.create_finished_workout(
        111, started_at="2026-02-01T10:00:00", finished_at="2026-02-01T11:00:00"
    )
    block = await db.create_block(past, "single")
    await db.add_block_exercise(block, ex_id, 0)
    await db.add_set(block, ex_id, round_index=1, order_in_round=0, weight=40.0, reps=8)
    hint = await _hint(client, workout_id, ex_id)
    assert "no_history" not in hint and hint["target_reps"] == 9


@pytest.mark.asyncio
async def test_api_program_scheme_wins(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex_id, workout_id = await _setup(routine=True)
    assert await _hint(client, workout_id, ex_id) is None


@pytest.mark.asyncio
async def test_api_program_rule_wins(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex_id, workout_id = await _setup(routine=True)
    routine_id = (await db.get_workout(workout_id))["routine_id"]
    await db.set_routine_exercise_target(
        (await db.list_routine_exercises(routine_id))[0]["id"], None
    )
    entry = (await db.list_routine_exercises(routine_id))[0]
    await db.set_routine_exercise_progression(
        entry["id"], json.dumps({"rule": "double_progression", "reps_top": 8, "step": 2.5})
    )
    assert await _hint(client, workout_id, ex_id) is None


@pytest.mark.asyncio
async def test_api_no_line_for_a_plank(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex_id = await db.get_or_create_user_exercise_by_name(111, "Планка")
    workout_id = await db.create_workout(111, started_at="2026-03-02T10:00:00")
    assert await _hint(client, workout_id, ex_id) is None


@pytest.mark.asyncio
async def test_api_no_line_when_the_toggle_is_off(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    await db.conn().execute("UPDATE users SET progression_hint_enabled = 0 WHERE telegram_id = 111")
    await db.conn().commit()
    ex_id, workout_id = await _setup()
    assert await _hint(client, workout_id, ex_id) is None


def test_no_history_hint_unit():
    assert progression_data.no_history_hint(kind="timed") is None
    assert progression_data.no_history_hint(target="3×5") is None
    assert progression_data.no_history_hint(rule={"rule": "linear_load"}) is None
