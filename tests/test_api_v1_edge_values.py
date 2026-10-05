"""Пограничные значения REST `/v1`: прогрессия, еда, подходы, гонки."""
import json

import httpx
import pytest

import api_v1

pytestmark = pytest.mark.asyncio


@pytest.fixture
def client_factory():
    def _make():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test"
        )

    return _make


async def _client(fresh_db, client_factory, telegram_id=111, lang=None):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    if lang:
        await fresh_db.set_user_lang(telegram_id, lang)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _routine_item(fresh_db, client):
    routine_id = (await client.post("/routines", json={"name": "Push"})).json()["id"]
    ex_id = await fresh_db.create_exercise(111, "Жим", None)
    item = (await client.post(f"/routines/{routine_id}/exercises", json={"exercise_id": ex_id})).json()
    return routine_id, item["id"]


# ---------- 5: progression ----------

@pytest.mark.parametrize("rule", [
    {"rule": "linear_load", "step": float("nan")},
    {"rule": "linear_load", "step": float("inf")},
    {"rule": "linear_load", "step": 0},
    {"rule": "linear_load", "step": -2.5},
    {"rule": "linear_load", "step": 1e308},
    {"rule": "linear_load", "step": 1000},
    {"rule": "linear_load", "step": True},
    {"rule": "linear_load", "step": "2.5"},
    {"rule": "made_up", "step": 2.5},
    {"rule": "linear_load", "step": 2.5, "x": {"nested": 1}},
    {"rule": "linear_load", "step": 2.5, "note": "я" * 5000},
    {f"k{i}": i for i in range(40)},
])
async def test_progression_schema_rejected(fresh_db, client_factory, rule):
    client = await _client(fresh_db, client_factory)
    _, item_id = await _routine_item(fresh_db, client)
    resp = await client.patch(
        f"/routine-exercises/{item_id}",
        content=json.dumps({"progression": rule}),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "bad_request"


async def test_progression_error_is_localized(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory, lang="en")
    _, item_id = await _routine_item(fresh_db, client)
    resp = await client.patch(
        f"/routine-exercises/{item_id}", json={"progression": {"rule": "linear_load", "step": 0}}
    )
    assert resp.status_code == 400
    assert not any("а" <= ch <= "я" for ch in resp.json()["message"].lower())


async def test_valid_progression_still_saved(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    _, item_id = await _routine_item(fresh_db, client)
    ok = await client.patch(
        f"/routine-exercises/{item_id}",
        json={"progression": {"rule": "double_progression", "step": 2.5, "reps_top": 12}},
    )
    assert ok.status_code == 200
    assert ok.json()["progression"]["step"] == 2.5


async def test_reading_stored_nan_progression_does_not_500(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    routine_id, item_id = await _routine_item(fresh_db, client)
    await fresh_db.set_routine_exercise_progression(item_id, '{"rule": "linear_load", "step": NaN}')
    resp = await client.get(f"/routines/{routine_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["exercises"][0]["progression"] is None


# ---------- 6: food caps ----------

@pytest.mark.parametrize("field,value", [
    ("kcal", 1e308), ("kcal", 20001), ("protein", 1e308), ("fat", 2001), ("carbs", 1e9),
])
async def test_food_entry_caps(fresh_db, client_factory, field, value):
    client = await _client(fresh_db, client_factory)
    resp = await client.post("/food", json={"name": "Плов", field: value})
    assert resp.status_code == 400, resp.text


async def test_food_entry_at_the_cap_is_fine(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    resp = await client.post("/food", json={"name": "Плов", "kcal": 20000, "protein": 2000})
    assert resp.status_code == 201, resp.text


# ---------- 7: гонка записи подхода и удаления/закрытия ----------

async def _open_workout_with_exercise(fresh_db, client):
    ex_id = await fresh_db.create_exercise(111, "Жим", None)
    wid = (await client.post("/workouts/active")).json()["id"]
    return wid, ex_id


@pytest.mark.parametrize("path,body", [
    ("sets", {"weight": 50, "reps": 5}),
    ("sets/text", {"text": "50 5"}),
])
async def test_set_racing_workout_delete_is_404(fresh_db, client_factory, monkeypatch, path, body):
    client = await _client(fresh_db, client_factory)
    wid, ex_id = await _open_workout_with_exercise(fresh_db, client)
    real = api_v1._block_for_exercise

    async def delete_then_create(workout_id, exercise_id):
        await fresh_db.discard_workout(workout_id)
        return await real(workout_id, exercise_id)

    monkeypatch.setattr(api_v1, "_block_for_exercise", delete_then_create)
    resp = await client.post(f"/workouts/{wid}/{path}", json={"exercise_id": ex_id, **body})
    assert resp.status_code == 404, resp.text


async def test_set_racing_finish_is_409(fresh_db, client_factory, monkeypatch):
    import sqlite3

    client = await _client(fresh_db, client_factory)
    wid, ex_id = await _open_workout_with_exercise(fresh_db, client)

    async def finish_then_fail(*a, **k):
        await fresh_db.finish_workout(wid)
        raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")

    monkeypatch.setattr(fresh_db, "append_set", finish_then_fail)
    resp = await client.post(
        f"/workouts/{wid}/sets", json={"exercise_id": ex_id, "weight": 50, "reps": 5}
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == "workout_finished"


# ---------- 8: PATCH подхода, пропавшего между update и чтением ----------

async def test_patch_set_vanished_after_update_is_404(fresh_db, client_factory, monkeypatch):
    client = await _client(fresh_db, client_factory)
    wid, ex_id = await _open_workout_with_exercise(fresh_db, client)
    created = (await client.post(
        f"/workouts/{wid}/sets", json={"exercise_id": ex_id, "weight": 50, "reps": 5}
    )).json()
    real_update = fresh_db.update_set

    async def update_then_delete(set_id, *a, **k):
        await real_update(set_id, *a, **k)
        await fresh_db.conn().execute("DELETE FROM sets WHERE id = ?", (set_id,))
        await fresh_db.conn().commit()

    monkeypatch.setattr(fresh_db, "update_set", update_then_delete)
    resp = await client.patch(f"/workouts/{wid}/sets/{created['id']}", json={"reps": 6})
    assert resp.status_code == 404, resp.text


# ---------- 9: рекорды упражнения с подходами длиннее 12 повторов ----------

async def test_progress_best_set_for_high_rep_sets_is_not_zero(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex_id = await fresh_db.create_exercise(111, "Жим", None)
    wid = await fresh_db.create_finished_workout(
        111, started_at="2026-03-01T10:00:00", finished_at="2026-03-01T11:00:00"
    )
    block_id = await fresh_db.create_block(wid, "single")
    await fresh_db.add_block_exercise(block_id, ex_id, 0)
    await fresh_db.add_set(block_id, ex_id, 1, 0, 30.0, 15, None)
    await fresh_db.add_set(block_id, ex_id, 2, 0, 30.0, 20, None)
    body = (await client.get(f"/exercises/{ex_id}/progress/sessions")).json()
    best = body["records"]["best_set"]
    assert "0×0" not in best and best.strip() != "0"
    assert "20" in best and "30" in best


# ---------- 10: формулы в CSV-экспорте, управляющие символы в именах ----------

@pytest.mark.parametrize("name", ["=HYPERLINK(1)", "+1", "-2 кг", "@SUM(A1)"])
async def test_csv_export_escapes_formula_cells(fresh_db, user_id, name):
    import csv_export

    ex_id = await fresh_db.create_exercise(user_id, name, None)
    wid = await fresh_db.create_finished_workout(
        user_id, started_at="2026-03-01T10:00:00", finished_at="2026-03-01T11:00:00"
    )
    block_id = await fresh_db.create_block(wid, "single")
    await fresh_db.add_block_exercise(block_id, ex_id, 0)
    await fresh_db.add_set(block_id, ex_id, 1, 0, 50.0, 5, None)
    import csv as csvmod
    import io

    raw = (await csv_export.build_csv(user_id)).decode("utf-8-sig")
    rows = list(csvmod.reader(io.StringIO(raw)))
    assert rows[1][1] == "'" + name


async def test_escape_formula_cell_covers_tab_and_cr():
    import csv_export

    assert csv_export.escape_formula_cell("\tx") == "'\tx"
    assert csv_export.escape_formula_cell("\rx") == "'\rx"
    assert csv_export.escape_formula_cell("Присед") == "Присед"


@pytest.mark.parametrize("name", [
    "'-5 drop", "=SUM", "-5", "\tfoo", "\rbar", "@x", "+1", "'Тяга", "''=x", "'=x", "'\tx", "Присед", "'",
])
async def test_csv_formula_escape_roundtrips_exactly(name):
    import csv_export
    from handlers import csv_import

    cell = csv_export.escape_formula_cell(name)
    assert csv_import._unescape_formula_cell(cell) == name
    if csv_export.needs_formula_escape(name):
        assert cell == "'" + name
    else:
        assert cell == name


async def test_csv_export_keeps_leading_apostrophe_name(fresh_db, user_id):
    import csv as csvmod
    import io

    import csv_export
    from handlers import csv_import

    ex_id = await fresh_db.create_exercise(user_id, "'-5 drop", None)
    wid = await fresh_db.create_finished_workout(
        user_id, started_at="2026-03-01T10:00:00", finished_at="2026-03-01T11:00:00"
    )
    block_id = await fresh_db.create_block(wid, "single")
    await fresh_db.add_block_exercise(block_id, ex_id, 0)
    await fresh_db.add_set(block_id, ex_id, 1, 0, 50.0, 5, None)
    raw = (await csv_export.build_csv(user_id)).decode("utf-8-sig")
    cell = list(csvmod.reader(io.StringIO(raw)))[1][1]
    assert cell == "''-5 drop"
    assert csv_import._unescape_formula_cell(cell) == "'-5 drop"


async def test_exercise_name_control_chars_stripped(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    resp = await client.post("/exercises", json={"name": "Жим\x00 лёжа\x1b\n"})
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["display_name"] == "Жим лёжа"


async def test_name_of_only_control_chars_is_rejected(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    resp = await client.post("/exercises", json={"name": "\x00\x01"})
    assert resp.status_code == 400


async def test_program_and_day_names_control_chars_stripped(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    day = await client.post("/routines", json={"name": "Push\x00 day"})
    assert day.json()["name"] == "Push day"
    prog = await client.post("/programs", json={"name": "Сила\x00"})
    assert prog.status_code in (200, 201), prog.text
    assert prog.json()["name"] == "Сила"


async def test_note_keeps_newline_drops_nul(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    wid = (await client.post("/workouts/active")).json()["id"]
    await fresh_db.finish_workout(wid)
    resp = await client.patch(f"/workouts/{wid}/note", json={"note": "строка1\x00\nстрока2\x1b"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["note"] == "строка1\nстрока2"


# ---------- 5: реальные формы правил (AI-тренер, каталог, iOS AIProgression) ----------

REAL_RULES = [
    {"rule": "double_progression", "step": 2.5, "reps_top": 12},
    {"rule": "double_progression", "reps_top": 12},
    {"rule": "linear_load", "step": 2.5},
    {"rule": "linear_load", "step": 0.25},
    {"rule": "top_set_backoff", "step": 2.5, "top_reps_min": 3, "top_reps_max": 5,
     "backoff_sets": 3, "backoff_pct": 85},
    {"rule": "top_set_backoff", "top_reps_min": 1, "top_reps_max": 5, "backoff_sets": 3, "backoff_pct": 85},
    # упражнения на время: шаг в секундах, потолок — тот же общий (15 сек ≪ 25)
    {"rule": "double_progression", "reps_top": 60, "step": 5, "step_unit": "sec"},
    {"rule": "double_progression", "reps_top": 60, "step": 15, "step_unit": "sec"},
    # iOS при копировании дня шлёт всё, что пришло, возможно с null
    {"rule": "linear_load", "step": None, "reps_top": None, "step_unit": None},
    {"rule": None, "step": 2.5},
]


@pytest.mark.parametrize("rule", REAL_RULES)
async def test_real_progression_shapes_are_accepted(fresh_db, client_factory, rule):
    client = await _client(fresh_db, client_factory)
    _, item_id = await _routine_item(fresh_db, client)
    resp = await client.patch(f"/routine-exercises/{item_id}", json={"progression": rule})
    assert resp.status_code == 200, resp.text


async def test_lb_athlete_stored_step_roundtrips_through_patch(fresh_db, client_factory):
    """Шаг 2.5 кг после перехода на lb = 5.5116 — его же приложение шлёт назад
    при копировании дня; потолок для lb выше кг-шного."""
    import analytics
    import db as dbmod

    client = await _client(fresh_db, client_factory)
    await fresh_db.update_user(111, unit="lb")
    _, item_id = await _routine_item(fresh_db, client)
    top = analytics.progression_max_step("lb")
    for step in (dbmod.convert_weight(2.5, 2.20462), top):
        resp = await client.patch(
            f"/routine-exercises/{item_id}", json={"progression": {"rule": "linear_load", "step": step}}
        )
        assert resp.status_code == 200, resp.text


async def test_ai_clean_progression_output_is_always_accepted(fresh_db, client_factory):
    import ai_trainer

    client = await _client(fresh_db, client_factory)
    _, item_id = await _routine_item(fresh_db, client)
    raws = [
        {"rule": "double_progression", "reps_top": 10, "step": 99},
        {"rule": "linear_load", "step": 1000},
        {"rule": "top_set_backoff", "top_reps_min": 9, "top_reps_max": 1, "backoff_sets": 50, "backoff_pct": 5},
    ]
    for raw in raws:
        cleaned = ai_trainer._clean_progression(raw, "kg")
        resp = await client.patch(f"/routine-exercises/{item_id}", json={"progression": cleaned})
        assert resp.status_code == 200, (cleaned, resp.text)


# ---------- округлённое число вернулось от клиента: хранимое точное не затираем ----------

async def _unit_switch(client, unit):
    resp = await client.patch("/settings", json={"unit": unit})
    assert resp.status_code == 200, resp.text


async def test_set_edit_echoing_rounded_weight_keeps_exact_value(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    await _unit_switch(client, "lb")
    wid, ex_id = await _open_workout_with_exercise(fresh_db, client)
    created = (await client.post(
        f"/workouts/{wid}/sets", json={"exercise_id": ex_id, "weight": 135, "reps": 5}
    )).json()
    await fresh_db.finish_workout(wid)
    await _unit_switch(client, "kg")
    shown = (await client.get(f"/exercises/{ex_id}/progress")).json()
    kg_shown = shown[0]["weight"]
    assert kg_shown == round(135 / 2.20462, 2)  # клиент видит округлённое
    # правка только повторов: приложение шлёт вес как показали
    resp = await client.patch(
        f"/workouts/{wid}/sets/{created['id']}", json={"weight": kg_shown, "reps": 6}
    )
    assert resp.status_code == 200, resp.text
    await _unit_switch(client, "lb")
    cur = await fresh_db.conn().execute("SELECT weight FROM sets WHERE id = ?", (created["id"],))
    assert (await cur.fetchone())["weight"] == 135


async def test_set_edit_with_a_real_new_weight_is_stored_as_sent(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    wid, ex_id = await _open_workout_with_exercise(fresh_db, client)
    created = (await client.post(
        f"/workouts/{wid}/sets", json={"exercise_id": ex_id, "weight": 60, "reps": 5}
    )).json()
    resp = await client.patch(f"/workouts/{wid}/sets/{created['id']}", json={"weight": 62.5})
    assert resp.status_code == 200
    cur = await fresh_db.conn().execute("SELECT weight FROM sets WHERE id = ?", (created["id"],))
    assert (await cur.fetchone())["weight"] == 62.5


async def test_progression_step_echoed_back_keeps_exact(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    _, item_id = await _routine_item(fresh_db, client)
    await client.patch(
        f"/routine-exercises/{item_id}", json={"progression": {"rule": "linear_load", "step": 2.5}}
    )
    await _unit_switch(client, "lb")
    cur = await fresh_db.conn().execute("SELECT progression FROM routine_exercises WHERE id = ?", (item_id,))
    stored_lb = json.loads((await cur.fetchone())["progression"])["step"]
    assert stored_lb == pytest.approx(5.51155, abs=1e-5)
    # приложение вернуло показанное 5.51 тому же пункту
    resp = await client.patch(
        f"/routine-exercises/{item_id}", json={"progression": {"rule": "linear_load", "step": 5.51}}
    )
    assert resp.status_code == 200, resp.text
    await _unit_switch(client, "kg")
    cur = await fresh_db.conn().execute("SELECT progression FROM routine_exercises WHERE id = ?", (item_id,))
    assert json.loads((await cur.fetchone())["progression"])["step"] == 2.5


@pytest.mark.parametrize("step", [2.27, 4.54, 1.13])
async def test_manual_kg_step_without_stored_progression_is_kept_as_sent(fresh_db, client_factory, step):
    client = await _client(fresh_db, client_factory)
    _, item_id = await _routine_item(fresh_db, client)
    await client.patch(
        f"/routine-exercises/{item_id}", json={"progression": {"rule": "linear_load", "step": step}}
    )
    cur = await fresh_db.conn().execute("SELECT progression FROM routine_exercises WHERE id = ?", (item_id,))
    assert json.loads((await cur.fetchone())["progression"])["step"] == step


async def test_step_of_other_unit_is_not_compared_with_stored(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    _, item_id = await _routine_item(fresh_db, client)
    await fresh_db.set_routine_exercise_progression(
        item_id, json.dumps({"rule": "double_progression", "step": 5.0000001, "step_unit": "sec"})
    )
    await client.patch(
        f"/routine-exercises/{item_id}", json={"progression": {"rule": "linear_load", "step": 5.0}}
    )
    cur = await fresh_db.conn().execute("SELECT progression FROM routine_exercises WHERE id = ?", (item_id,))
    assert json.loads((await cur.fetchone())["progression"])["step"] == 5.0


@pytest.mark.parametrize("step", [2.5, 5, 1.25, 5.5, 7.37])
async def test_round_native_or_odd_steps_are_not_touched(fresh_db, client_factory, step):
    client = await _client(fresh_db, client_factory)
    await _unit_switch(client, "lb")
    _, item_id = await _routine_item(fresh_db, client)
    await client.patch(
        f"/routine-exercises/{item_id}", json={"progression": {"rule": "linear_load", "step": step}}
    )
    cur = await fresh_db.conn().execute("SELECT progression FROM routine_exercises WHERE id = ?", (item_id,))
    assert json.loads((await cur.fetchone())["progression"])["step"] == step


async def test_bodyweight_edit_echoing_rounded_keeps_exact(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    await fresh_db.conn().execute(
        "INSERT INTO bodyweight_logs (telegram_id, weight, logged_at) VALUES (111, 61.23496133, '2026-01-01T10:00:00')"
    )
    await fresh_db.conn().commit()
    log_id = (await client.get("/bodyweight")).json()[0]["id"]
    resp = await client.patch(f"/bodyweight/{log_id}", json={"weight": 61.23})
    assert resp.status_code == 200, resp.text
    cur = await fresh_db.conn().execute("SELECT weight FROM bodyweight_logs WHERE id = ?", (log_id,))
    assert (await cur.fetchone())["weight"] == 61.23496133


# ---------- _round_floats / JSONResponse ----------

async def test_round_floats_walks_nested_structures():
    import api_v1_common as common

    out = common._round_floats({"a": [1.23456, (2.0049, {"b": 3.999})], "c": {"d": 61.23496133}})
    assert out == {"a": [1.23, [2.0, {"b": 4.0}]], "c": {"d": 61.23}}


async def test_round_floats_leaves_bool_int_str_none_and_nonfinite():
    import math

    import api_v1_common as common

    out = common._round_floats({"t": True, "f": False, "i": 7, "s": "1.23456", "n": None})
    assert out == {"t": True, "f": False, "i": 7, "s": "1.23456", "n": None}
    assert out["t"] is True and out["f"] is False
    nan, inf = common._round_floats(float("nan")), common._round_floats(float("-inf"))
    assert math.isnan(nan) and inf == float("-inf")


async def test_round_floats_normalizes_negative_zero():
    import math

    import api_v1_common as common

    for raw in (-0.0, -0.001, -0.0049):
        out = common._round_floats(raw)
        assert out == 0.0 and math.copysign(1.0, out) == 1.0
    assert common.JSONResponse({"x": -0.001}).body == b'{"x":0.0}'


async def test_json_response_rounds_in_body():
    import api_v1_common as common

    body = json.loads(common.JSONResponse({"w": [61.23496133], "ok": True}).body)
    assert body == {"w": [61.23], "ok": True}
    with pytest.raises(ValueError):  # NaN по-прежнему не JSON (starlette: allow_nan=False)
        common.JSONResponse({"w": float("nan")})


async def test_bodyweight_patch_of_foreign_entry_is_404(fresh_db, client_factory):
    mine = await _client(fresh_db, client_factory)
    await fresh_db.get_or_create_user(telegram_id=222, username="other")
    await fresh_db.conn().execute(
        "INSERT INTO bodyweight_logs (telegram_id, weight, logged_at) VALUES (222, 61.23496133, '2026-01-01T10:00:00')"
    )
    await fresh_db.conn().commit()
    cur = await fresh_db.conn().execute("SELECT id FROM bodyweight_logs WHERE telegram_id = 222")
    log_id = (await cur.fetchone())["id"]
    resp = await mine.patch(f"/bodyweight/{log_id}", json={"weight": 61.23})
    assert resp.status_code == 404
    cur = await fresh_db.conn().execute("SELECT weight FROM bodyweight_logs WHERE id = ?", (log_id,))
    assert (await cur.fetchone())["weight"] == 61.23496133
