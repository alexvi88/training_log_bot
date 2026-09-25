"""Импорт CSV пишет вес в единице аккаунта, а не всегда в килограммах.

Вес подхода хранится в `users.unit` (db.scale_user_set_weights пересчитывает
историю при смене кг↔lb). Раньше импорт всегда переводил в кг: у атлета в
фунтах «weight_lbs = 225» ложилось 102.1 (на экране — «102 lb»), а явные
килограммы — как фунты без пересчёта. Бот и REST, превью и запись — один и
тот же разбор (handlers.csv_import._build_workout_groups).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import ai_trainer
import api_v1
import config
import db
import handlers.csv_import as csv_import
from fsm import ImportFlow
from handlers.csv_import import _build_workout_groups, _weight_factor
from parser import ParseError

MAPPING = {"date": 0, "exercise": 1, "weight": 2, "reps": 3}

HEVY_HDR = (
    "title,start_time,end_time,description,exercise_title,superset_id,"
    "exercise_notes,set_index,set_type,weight_lbs,reps,distance_miles,duration_seconds,rpe"
)
HEVY_LBS = (
    HEVY_HDR + "\n"
    '"Push","7 Aug 2026, 00:26","7 Aug 2026, 00:50","","Bench Press (Barbell)",,"",0,"warmup",95,10,,,\n'
    '"Push","7 Aug 2026, 00:26","7 Aug 2026, 00:50","","Bench Press (Barbell)",,"",1,"normal",225,5,,,\n'
)


# ---------- множитель: единица файла → единица аккаунта ----------


@pytest.mark.parametrize(
    "header, account_unit, factor",
    [
        ("weight_lbs", "lb", 1.0),
        ("Weight (lbs)", "lb", 1.0),
        ("weight_lbs", "kg", 0.45359237),
        ("weight_kg", "lb", config.LB_PER_KG),
        ("Weight (kg)", "lb", config.LB_PER_KG),
        ("Weight kg", "lb", config.LB_PER_KG),
        ("Вес (кг)", "lb", config.LB_PER_KG),
        ("weight_kg", "kg", 1.0),
        # Без единицы — единица аккаунта: так пишет наш экспорт.
        ("weight", "lb", 1.0),
        ("Вес", "lb", 1.0),
        ("weight", "kg", 1.0),
        ("bulbs", "kg", 1.0),
        ("backgrounds", "lb", 1.0),
    ],
)
def test_weight_factor_targets_account_unit(header, account_unit, factor):
    assert _weight_factor(["date", "exercise", header, "reps"], MAPPING, account_unit) == pytest.approx(factor)


def test_kg_file_into_lb_account_is_converted_and_rounded():
    rows = [["2025-01-02", "Жим лёжа", "100", "5"]]
    factor = _weight_factor(["date", "exercise", "weight_kg", "reps"], MAPPING, "lb")
    (workout,) = _build_workout_groups(rows, MAPPING, weight_factor=factor, account_unit="lb")
    assert workout["entries"][0]["sets"] == [[220.5, 5, None]]


def test_max_weight_is_checked_in_kg_equivalent():
    """MAX_WEIGHT задан в кг: 1600 lb (≈726 кг) — не повод валить файл, а
    1600 кг у атлета в фунтах (≈3527 lb) — по-прежнему ошибка."""
    lb_rows = [["2025-01-02", "Становая", "1600", "1"]]
    (workout,) = _build_workout_groups(lb_rows, MAPPING, account_unit="lb")
    assert workout["entries"][0]["sets"] == [[1600.0, 1, None]]
    with pytest.raises(ParseError):
        _build_workout_groups(lb_rows, MAPPING, account_unit="kg")
    kg_factor = _weight_factor(["date", "exercise", "weight_kg", "reps"], MAPPING, "lb")
    with pytest.raises(ParseError):
        _build_workout_groups(lb_rows, MAPPING, weight_factor=kg_factor, account_unit="lb")


# ---------- имя упражнения: тот же потолок, что в /v1 ----------


def test_too_long_exercise_name_is_a_line_error():
    ok = "Ж" * config.MAX_EXERCISE_NAME_LENGTH
    _build_workout_groups([["2025-01-02", ok, "100", "5"]], MAPPING)
    rows = [
        ["2025-01-02", "Жим лёжа", "100", "5"],
        ["2025-01-02", ok + "ж", "100", "5"],
    ]
    with pytest.raises(ParseError) as err:
        _build_workout_groups(rows, MAPPING)
    assert "3" in err.value.message
    assert str(config.MAX_EXERCISE_NAME_LENGTH) in err.value.message


# ---------- бот ----------


async def _bot_upload(uid, text, monkeypatch):
    monkeypatch.setattr(ai_trainer, "match_exercise_names_to_catalog", AsyncMock(return_value={}))
    msg = MagicMock()
    msg.document = SimpleNamespace(file_name="workout_data.csv")
    msg.from_user = SimpleNamespace(id=uid, username="t", language_code=None)
    msg.reply = AsyncMock()
    msg.answer = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.download = AsyncMock(return_value=SimpleNamespace(read=lambda: text.encode()))
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))
    await csv_import.import_file_received(msg, state)
    return msg, state


async def _bot_save(uid, state):
    message = MagicMock()
    message.chat = SimpleNamespace(id=uid)
    message.message_id = 1
    message.text = "экран"
    message.photo = None
    message.edit_text = AsyncMock(return_value=True)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=999, chat=SimpleNamespace(id=uid)))
    message.answer_photo = AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=uid), message_id=1000))
    message.delete = AsyncMock()
    cb = MagicMock()
    cb.from_user = SimpleNamespace(id=uid, username="t", language_code=None)
    cb.data = "imp:save"
    cb.answer = AsyncMock()
    cb.message = message
    await csv_import.import_save(cb, state)


async def _weights(uid):
    cur = await db.conn().execute(
        "SELECT s.weight FROM sets s JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id WHERE w.user_id = ? ORDER BY s.id",
        (uid,),
    )
    return [round(row[0], 2) for row in await cur.fetchall()]


@pytest.mark.parametrize("unit, stored", [("lb", 225.0), ("kg", 102.1)])
async def test_bot_import_lbs_file_lands_in_account_unit(fresh_db, monkeypatch, unit, stored):
    uid = 333
    await fresh_db.get_or_create_user(uid, "t")
    await fresh_db.update_user(uid, unit=unit)
    gid = await fresh_db.create_muscle_group(uid, "Грудь")
    await fresh_db.create_exercise(uid, "Bench Press (Barbell)", gid)
    msg, state = await _bot_upload(uid, HEVY_LBS, monkeypatch)
    assert msg.reply.await_count == 0
    assert await state.get_state() == ImportFlow.confirming
    await _bot_save(uid, state)
    assert await _weights(uid) == [stored]


async def test_bot_import_rejects_too_long_exercise_name(fresh_db, monkeypatch):
    uid = 334
    await fresh_db.get_or_create_user(uid, "t")
    name = "Жим" + "ж" * 5000
    msg, state = await _bot_upload(uid, f"date,exercise,weight,reps\n2026-03-01,{name},80,5\n", monkeypatch)
    assert await state.get_state() == ImportFlow.awaiting_file
    cur = await db.conn().execute("SELECT COUNT(*) FROM exercises WHERE user_id = ?", (uid,))
    assert (await cur.fetchone())[0] == 0


# ---------- REST ----------


async def _rest_client(fresh_db, uid, unit):
    await fresh_db.get_or_create_user(uid, "t")
    await fresh_db.update_user(uid, unit=unit)
    code = await fresh_db.issue_oauth_link_code(uid, ttl_seconds=600, digits=8)
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    tok = (await c.post("/auth/link", json={"code": code})).json()["token"]
    c.headers["Authorization"] = f"Bearer {tok}"
    return c


@pytest.fixture(autouse=True)
def _no_ai_matching(monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)


async def test_rest_import_into_lb_account(fresh_db):
    uid = 444
    c = await _rest_client(fresh_db, uid, "lb")
    r = await c.post("/import/csv/preview", json={"csv": HEVY_LBS})
    assert r.status_code == 200, r.text
    r = await c.post("/import/csv", json={"csv": HEVY_LBS})
    assert r.status_code == 200, r.text
    assert r.json()["rows_skipped_warmup"] == 1
    r = await c.post("/import/csv", json={"csv": "date,exercise,weight_kg,reps\n2026-03-01,Присед,100,5\n"})
    assert r.status_code == 200, r.text
    r = await c.post("/import/csv", json={"csv": "date,exercise,weight,reps\n2026-03-02,Присед,135,5\n"})
    assert r.status_code == 200, r.text
    assert await _weights(uid) == [225.0, 220.5, 135.0]


async def test_rest_lb_account_heavy_lbs_pass_the_kg_ceiling(fresh_db):
    c = await _rest_client(fresh_db, 445, "lb")
    csv = "date,exercise,weight,reps\n2026-03-01,Становая,1600,1\n"
    r = await c.post("/import/csv/preview", json={"csv": csv})
    assert r.status_code == 200, r.text


async def test_rest_import_rejects_too_long_exercise_name(fresh_db):
    uid = 446
    c = await _rest_client(fresh_db, uid, "kg")
    name = "Жим" + "ж" * 5000
    csv = f"date,exercise,weight,reps\n2026-03-01,{name},80,5\n"
    for path in ("/import/csv/preview", "/import/csv"):
        r = await c.post(path, json={"csv": csv})
        assert r.status_code == 400, r.text
        assert r.json()["error"] == "invalid_csv"
        assert r.json()["message"]
    cur = await db.conn().execute("SELECT COUNT(*) FROM exercises WHERE user_id = ?", (uid,))
    assert (await cur.fetchone())[0] == 0


async def test_rest_muscle_group_name_length(fresh_db):
    c = await _rest_client(fresh_db, 447, "kg")
    r = await c.post("/muscle-groups", json={"name": "Г" * 5000})
    assert r.status_code == 400
    assert r.json()["error"] == "name_too_long" and r.json()["message"]
    r = await c.post("/muscle-groups", json={"name": "Г" * config.MAX_EXERCISE_NAME_LENGTH})
    assert r.status_code == 201, r.text
    r = await c.post("/muscle-groups", json={"name": "  "})
    assert r.status_code == 400
