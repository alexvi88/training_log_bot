"""«Что это за тренажёр»: POST /v1/exercises/identify и /identify/accept.

Модель подменена на уровне клиента vision_backend(): ответ — JSON строгой
схемы, как его вернул бы OpenAI. Всё остальное — настоящее: квота вопроса,
cost-событие через paid_call, разбор по базе атлета, события аналитики.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

import ai_limits
import ai_trainer
import api_v1
import api_v1_ai
import config
import exercise_identify

PHOTO = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0fake-jpeg").decode()


def _verdict(**overrides):
    verdict = {
        "unclear": False, "catalog_name": None, "own_name": None, "alternatives": [],
        "new_name": None, "new_group": None, "equipment": None,
        "title": "That's a hack squat machine.", "tip": "Keep your back flat on the pad.",
    }
    verdict.update(overrides)
    return verdict


def _install_model(monkeypatch, verdict) -> AsyncMock:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(verdict, ensure_ascii=False)))],
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=50),
    )
    create = AsyncMock(return_value=response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    backend = ai_trainer.ChatBackend(client=client, model=config.OPENAI_VISION_MODEL, effort="none", quick_effort="none")
    monkeypatch.setattr(ai_trainer, "vision_backend", lambda: backend)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    return create


async def _client(fresh_db, lang="en", telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    await fresh_db.set_user_lang(telegram_id, lang)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _events(fresh_db, name):
    cur = await fresh_db.conn().execute(
        "SELECT props, platform FROM analytics_events WHERE event = ? ORDER BY id", (name,)
    )
    return [(json.loads(r["props"] or "{}"), r["platform"]) for r in await cur.fetchall()]


async def _fork(fresh_db, identity, telegram_id=111):
    found = await fresh_db.find_global_templates_by_names([identity])
    return await fresh_db.fork_exercise_from_template(telegram_id, found[identity]["id"])


# ---------- identify: четыре итога ----------


async def test_catalog_result_for_exercise_the_athlete_does_not_have(fresh_db, monkeypatch):
    create = _install_model(monkeypatch, _verdict(
        catalog_name="Гак-присед", equipment="machine", alternatives=["Жим ногами", "Гак-присед", "nope"],
    ))
    client = await _client(fresh_db)
    resp = await client.post("/exercises/identify", json={"image_data_url": PHOTO})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "catalog"
    assert body["title"] == "That's a hack squat machine."
    assert body["tip"] == "Keep your back flat on the pad."
    ex = body["exercise"]
    assert ex["exercise_id"] is None
    assert ex["catalog_name"] == "Гак-присед"
    assert ex["name"] == "Hack Squat"
    assert ex["muscle_group"] == "Legs"
    assert ex["equipment"] == "machine"
    assert ex["image_url"] is None or ex["image_url"].startswith("/media/exercises/")
    # Главное не повторяется в запасных, мусор из запасных выкинут.
    assert [a["catalog_name"] for a in body["alternatives"]] == ["Жим ногами"]
    assert body["alternatives"][0]["exercise_id"] is None
    assert body["limits"]["question"]["used"] == 1
    # Один платный вызов: строгая схема, картинка в сообщении, свой слот кэша.
    kwargs = create.await_args.kwargs
    assert kwargs["response_format"]["json_schema"]["strict"] is True
    assert kwargs["extra_body"]["prompt_cache_key"] == "identify-111"
    assert kwargs["messages"][1]["content"][1]["image_url"]["url"] == PHOTO
    cur = await fresh_db.conn().execute("SELECT COUNT(*) FROM cost_events WHERE user_id = 111")
    assert (await cur.fetchone())[0] == 1
    assert await _events(fresh_db, "exercise_identified") == [({"result": "catalog"}, "ios")]
    assert await _events(fresh_db, "ai_question") == [({"kind": "identify"}, "ios")]
    # Ничего не завелось: до «Записать сюда» база не меняется.
    assert await fresh_db.find_exercise_by_original_name(111, "Гак-присед") is None


async def test_mine_result_with_last_time_from_real_history(fresh_db, monkeypatch):
    _install_model(monkeypatch, _verdict(
        catalog_name="Жим штанги лёжа", equipment="barbell", tip="Feet flat. Start with 60 kg for 10.",
    ))
    client = await _client(fresh_db)
    ex_id = await _fork(fresh_db, "Жим штанги лёжа")
    wid = (await client.post("/workouts/active", json={})).json()["id"]
    for weight, reps in ((70, 10), (80, 8), (80, 6)):
        r = await client.post(f"/workouts/{wid}/sets", json={"exercise_id": ex_id, "weight": weight, "reps": reps})
        assert r.status_code == 201, r.text
    assert (await client.post(f"/workouts/{wid}/finish", json={})).status_code == 200

    body = (await client.post("/exercises/identify", json={"image_data_url": PHOTO})).json()
    assert body["result"] == "mine"
    assert body["exercise"]["exercise_id"] == ex_id
    assert body["exercise"]["name"] == "Barbell Bench Press"
    # Выдуманные моделью числа выброшены, «в прошлый раз» — из дневника.
    assert body["tip"] == "Feet flat. Last time: 80×8."


async def test_mine_result_for_own_custom_exercise(fresh_db, monkeypatch):
    _install_model(monkeypatch, _verdict(own_name="belt squat", equipment="machine", tip=""))
    client = await _client(fresh_db)
    own = (await client.post("/exercises", json={"name": "Belt Squat"})).json()
    body = (await client.post("/exercises/identify", json={"image_data_url": PHOTO})).json()
    assert body["result"] == "mine"
    assert body["exercise"]["exercise_id"] == own["id"]
    assert body["exercise"]["catalog_name"] is None
    assert body["tip"] == ""


async def test_new_result_proposes_exercise_in_athlete_language(fresh_db, monkeypatch):
    _install_model(monkeypatch, _verdict(
        new_name="Pendulum squat", new_group="legs", equipment="machine", title="",
    ))
    client = await _client(fresh_db)
    body = (await client.post("/exercises/identify", json={"image_data_url": PHOTO})).json()
    assert body["result"] == "new"
    assert body["exercise"] == {
        "exercise_id": None, "name": "Pendulum squat", "catalog_name": None,
        "muscle_group": "Legs", "equipment": "machine", "image_url": None,
    }
    assert body["title"] == "Looks like Pendulum squat."
    assert body["alternatives"] == []


async def test_new_name_that_is_in_the_catalog_is_not_new(fresh_db, monkeypatch):
    _install_model(monkeypatch, _verdict(new_name="Hack Squat", new_group="legs", equipment="machine"))
    client = await _client(fresh_db)
    body = (await client.post("/exercises/identify", json={"image_data_url": PHOTO})).json()
    assert body["result"] == "catalog"
    assert body["exercise"]["catalog_name"] == "Гак-присед"


async def test_none_result_says_what_to_do(fresh_db, monkeypatch):
    _install_model(monkeypatch, _verdict(unclear=True, title="A cat.", tip="Cute."))
    client = await _client(fresh_db, lang="ru")
    body = (await client.post("/exercises/identify", json={"image_data_url": PHOTO})).json()
    assert body["result"] == "none"
    assert body["exercise"] is None and body["alternatives"] == [] and body["tip"] == ""
    assert body["title"] == "Не разобрал тренажёр. Сними его целиком, сбоку."
    assert await _events(fresh_db, "exercise_identified") == [({"result": "none"}, "ios")]


# ---------- identify: отказы ----------


async def test_quota_exhausted_is_429_and_model_not_called(fresh_db, monkeypatch):
    create = _install_model(monkeypatch, _verdict(catalog_name="Гак-присед"))
    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT", 1)
    ai_limits.reset_cache()
    client = await _client(fresh_db)
    await fresh_db.try_increment_ai_question_count(111, 1)
    resp = await client.post("/exercises/identify", json={"image_data_url": PHOTO})
    assert resp.status_code == 429
    assert resp.json()["error"] == "question_limit_exceeded"
    assert resp.json()["message"]
    create.assert_not_awaited()


async def test_bad_images(fresh_db, monkeypatch):
    create = _install_model(monkeypatch, _verdict(catalog_name="Гак-присед"))
    client = await _client(fresh_db)
    gif = "data:image/gif;base64," + base64.b64encode(b"GIF89a").decode()
    assert (await client.post("/exercises/identify", json={"image_data_url": gif})).status_code == 415
    resp = await client.post("/exercises/identify", json={})
    assert resp.status_code == 400 and resp.json()["message"] == "No photo came through. Snap the machine and send it."
    monkeypatch.setattr(api_v1_ai, "MAX_IMAGE_BYTES", 4)
    resp = await client.post("/exercises/identify", json={"image_data_url": PHOTO})
    assert resp.status_code == 400 and resp.json()["error"] == "photo_too_big"
    create.assert_not_awaited()
    assert (await client.get("/ai/limits")).json()["question"]["used"] == 0


async def test_unparsable_answer_is_502_and_costs_no_question(fresh_db, monkeypatch):
    create = _install_model(monkeypatch, _verdict())
    create.return_value.choices[0].message.content = "not json"
    client = await _client(fresh_db)
    resp = await client.post("/exercises/identify", json={"image_data_url": PHOTO})
    assert resp.status_code == 502
    assert (await client.get("/ai/limits")).json()["question"]["used"] == 0


def test_tip_never_carries_numbers():
    assert exercise_identify.clean_tip("Set the seat to 4. Keep knees out! Go for 3x10.") == "Keep knees out!"
    assert exercise_identify.clean_tip("One. Two. Three.") == "One. Two."
    assert exercise_identify.clean_tip(None) == ""


def test_schema_enum_fits_openai_limits():
    """Строгая схема: каталог целиком в enum — в пределах OpenAI (до 1000
    значений на схему, у одного enum больше 250 значений — до 15000 символов)."""
    schema = exercise_identify.response_format()["json_schema"]["schema"]
    catalog = schema["properties"]["catalog_name"]["enum"]
    alternatives = schema["properties"]["alternatives"]["items"]["enum"]
    assert len(catalog) + len(alternatives) + 20 < 1000
    assert sum(len(x) for x in alternatives) < 15000
    assert set(schema["required"]) == set(schema["properties"])


# ---------- accept ----------


async def test_accept_catalog_creates_copy_once(fresh_db, monkeypatch):
    client = await _client(fresh_db)
    first = await client.post("/exercises/identify/accept", json={"catalog_name": "Гак-присед"})
    assert first.status_code == 200, first.text
    exercise = first.json()["exercise"]
    assert exercise["display_name"] == "Hack Squat"
    assert exercise["original_name"] == "Гак-присед"
    listed = {e["id"]: e for e in (await client.get("/exercises")).json()}
    assert set(exercise) <= set(listed[exercise["id"]])
    assert {k: listed[exercise["id"]][k] for k in exercise} == exercise
    second = await client.post("/exercises/identify/accept", json={"catalog_name": "Гак-присед"})
    assert second.json()["exercise"]["id"] == exercise["id"]
    assert await _events(fresh_db, "exercise_identify_accept") == [
        ({"result": "catalog"}, "ios"), ({"result": "mine"}, "ios"),
    ]


async def test_accept_reuses_archived_copy(fresh_db):
    client = await _client(fresh_db)
    ex_id = await _fork(fresh_db, "Гак-присед")
    await fresh_db.archive_exercise(ex_id)
    body = (await client.post("/exercises/identify/accept", json={"catalog_name": "Гак-присед"})).json()
    assert body["exercise"]["id"] == ex_id
    assert body["exercise"]["is_archived"] is False


async def test_accept_custom_new_and_no_duplicate(fresh_db):
    client = await _client(fresh_db)
    payload = {"name": "Pendulum squat", "muscle_group": "Legs", "equipment": "machine"}
    first = (await client.post("/exercises/identify/accept", json=payload)).json()["exercise"]
    assert first["display_name"] == "Pendulum squat"
    groups = {g["name"]: g["id"] for g in (await client.get("/muscle-groups")).json()}
    assert first["primary_group_id"] == groups["Legs"]
    again = (await client.post("/exercises/identify/accept", json={**payload, "name": "pendulum SQUAT"})).json()
    assert again["exercise"]["id"] == first["id"]
    # Слаг группы и «своим весом» тоже понимаются.
    bw = (await client.post("/exercises/identify/accept", json={
        "name": "Sissy squat", "muscle_group": "legs", "equipment": "bodyweight",
    })).json()["exercise"]
    assert bw["reps_only"] is True
    assert await _events(fresh_db, "exercise_identify_accept") == [
        ({"result": "new"}, "ios"), ({"result": "mine"}, "ios"), ({"result": "new"}, "ios"),
    ]


async def test_accept_custom_name_from_catalog_goes_to_catalog_copy(fresh_db):
    client = await _client(fresh_db)
    body = (await client.post("/exercises/identify/accept", json={
        "name": "Hack Squat", "muscle_group": "Legs", "equipment": "machine",
    })).json()
    assert body["exercise"]["original_name"] == "Гак-присед"


async def test_accept_validation_errors_speak_coach(fresh_db):
    client = await _client(fresh_db)
    cases = [
        ({"catalog_name": "Нет такого"}, "unknown_catalog_exercise"),
        ({"name": "X", "muscle_group": "Legs", "equipment": "rocket"}, "invalid_equipment"),
        ({"name": "X", "muscle_group": "Wings", "equipment": "machine"}, "invalid_muscle_group"),
        ({"name": "  ", "muscle_group": "Legs", "equipment": "machine"}, "bad_request"),
        ({"name": "x" * 500, "muscle_group": "Legs", "equipment": "machine"}, "name_too_long"),
    ]
    for payload, code in cases:
        resp = await client.post("/exercises/identify/accept", json=payload)
        assert resp.status_code == 400, (payload, resp.text)
        assert resp.json()["error"] == code
        assert resp.json()["message"] and "error" not in resp.json()["message"].lower()
    assert (await client.post("/exercises/identify/accept", json={
        "name": "X", "muscle_group": "Wings", "equipment": "machine",
    })).json()["message"] == "Can't find that muscle group. Pick one from the list, like Chest."
