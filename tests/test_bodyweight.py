"""Bodyweight log: db round-trip, ordering, undo, rescale, and the screen text."""

import pytest

import db as dbmod
import formatting
from parser import ParseError, parse_bodyweight

# ---------- parser ----------


@pytest.mark.parametrize("text,expected", [("80", 80.0), ("80.5", 80.5), ("80,5", 80.5), (" 72 ", 72.0)])
def test_parse_bodyweight_ok(text, expected):
    assert parse_bodyweight(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "0", "-5", "1200", "80 kg"])
def test_parse_bodyweight_rejects(bad):
    with pytest.raises(ParseError):
        parse_bodyweight(bad)


# ---------- db ----------


@pytest.mark.asyncio
async def test_bodyweight_log_roundtrip_and_order(user_id):
    await dbmod.add_bodyweight_log(user_id, 80.0, logged_at="2026-01-01T10:00:00")
    await dbmod.add_bodyweight_log(user_id, 79.0, logged_at="2026-02-01T10:00:00")
    await dbmod.add_bodyweight_log(user_id, 78.0, logged_at="2026-03-01T10:00:00")

    logs = await dbmod.list_bodyweight_logs(user_id)
    assert [r["weight"] for r in logs] == [80.0, 79.0, 78.0]  # ascending by date

    latest = await dbmod.get_latest_bodyweight(user_id)
    assert latest["weight"] == 78.0


@pytest.mark.asyncio
async def test_bodyweight_limit_returns_recent_ascending(user_id):
    for i, w in enumerate([80, 79, 78, 77]):
        await dbmod.add_bodyweight_log(user_id, w, logged_at=f"2026-0{i + 1}-01T10:00:00")
    recent = await dbmod.list_bodyweight_logs(user_id, limit=2)
    assert [r["weight"] for r in recent] == [78.0, 77.0]


@pytest.mark.asyncio
async def test_delete_last_bodyweight(user_id):
    await dbmod.add_bodyweight_log(user_id, 80.0, logged_at="2026-01-01T10:00:00")
    await dbmod.add_bodyweight_log(user_id, 79.0, logged_at="2026-02-01T10:00:00")
    removed = await dbmod.delete_last_bodyweight(user_id)
    assert removed["weight"] == 79.0
    assert [r["weight"] for r in await dbmod.list_bodyweight_logs(user_id)] == [80.0]


@pytest.mark.asyncio
async def test_delete_last_bodyweight_empty(user_id):
    assert await dbmod.delete_last_bodyweight(user_id) is None


@pytest.mark.asyncio
async def test_scale_bodyweight_logs(user_id):
    await dbmod.add_bodyweight_log(user_id, 100.0, logged_at="2026-01-01T10:00:00")
    await dbmod.scale_bodyweight_logs(user_id, 2.20462)
    latest = await dbmod.get_latest_bodyweight(user_id)
    assert latest["weight"] == pytest.approx(220.5)  # rounded to 1 decimal


# ---------- screen text ----------


def test_bodyweight_screen_empty():
    text = formatting.build_bodyweight_screen([], "kg")
    assert "Пока нет ни одной записи" in text


def test_bodyweight_screen_with_deltas():
    logs = [
        {"weight": 82.0, "logged_at": "2026-01-01T10:00:00"},
        {"weight": 80.0, "logged_at": "2026-02-01T10:00:00"},
    ]
    text = formatting.build_bodyweight_screen(logs, "kg")
    assert "Сейчас: <b>80 кг</b>" in text
    assert "С прошлой записи: ↓ 2 кг" in text
    assert "За всё время: ↓ 2 кг" in text


# ---------- unit conversion: set weights ----------


@pytest.mark.asyncio
async def test_scale_user_set_weights_converts_nonzero_only(user_id):
    groups = await dbmod.list_muscle_groups(None, global_only=True)
    gid = groups[0]["id"]
    ex_id = await dbmod.create_exercise(user_id, "Жим", gid)
    wid = await dbmod.create_finished_workout(user_id, "2026-01-01T10:00:00", "2026-01-01T10:30:00")
    block_id = await dbmod.create_block(wid, "single")
    await dbmod.add_block_exercise(block_id, ex_id, 0)
    await dbmod.add_set(block_id, ex_id, 1, 0, 100.0, 8)
    await dbmod.add_set(block_id, ex_id, 2, 0, 0.0, 12)  # bodyweight set

    await dbmod.scale_user_set_weights(user_id, dbmod.config.LB_PER_KG)

    weights = sorted(s["weight"] for s in await dbmod.list_sets_for_block(block_id))
    assert weights == [0.0, pytest.approx(220.5)]  # zero untouched, 100 -> 220.5
