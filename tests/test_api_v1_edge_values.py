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


async def test_csv_export_import_roundtrip_unescapes():
    from handlers import csv_import

    assert csv_import._unescape_formula_cell("'=cmd") == "=cmd"
    assert csv_import._unescape_formula_cell("'Тяга") == "'Тяга"


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
