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
    assert latest["weight"] == pytest.approx(220.462)


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
