"""Weekly volume: analytics classifier, db aggregation by group, screen text."""

import datetime as dt

import pytest

import analytics
import db as dbmod
import formatting
from handlers.volume import _week_bounds

# ---------- classifier ----------


@pytest.mark.parametrize(
    "count,expected",
    [(0, "none"), (1, "low"), (4, "low"), (5, "in_range"), (12, "in_range"), (13, "high"), (25, "high")],
)
def test_classify_weekly_volume(count, expected):
    assert analytics.classify_weekly_volume(count) == expected


# ---------- week bounds ----------


def test_week_bounds_current_and_offset():
    wednesday = dt.date(2026, 7, 15)  # a Wednesday
    start, end = _week_bounds(wednesday, 0)
    assert start == dt.date(2026, 7, 13)  # Monday
    assert end == dt.date(2026, 7, 19)  # Sunday
    start_prev, end_prev = _week_bounds(wednesday, 1)
    assert start_prev == dt.date(2026, 7, 6)
    assert end_prev == dt.date(2026, 7, 12)


# ---------- db aggregation ----------


async def _group_id(name: str) -> int:
    groups = await dbmod.list_muscle_groups(None, global_only=True)
    return next(g["id"] for g in groups if g["name"] == name)


async def _log_sets(user_id: int, group_name: str, ex_name: str, started_at: str, n_sets: int) -> None:
    gid = await _group_id(group_name)
    ex_id = await dbmod.create_exercise(user_id, ex_name, gid)
    wid = await dbmod.create_finished_workout(user_id, started_at, started_at)
    block_id = await dbmod.create_block(wid, "single")
    await dbmod.add_block_exercise(block_id, ex_id, 0)
    for i in range(n_sets):
        await dbmod.add_set(block_id, ex_id, i + 1, 0, 100.0, 8)


@pytest.mark.asyncio
async def test_weekly_volume_by_group_counts_sets(user_id):
    await _log_sets(user_id, "Грудь", "Жим", "2026-07-14T10:00:00", 4)
    await _log_sets(user_id, "Грудь", "Разведение", "2026-07-16T10:00:00", 3)
    await _log_sets(user_id, "Спина", "Тяга", "2026-07-15T10:00:00", 6)
    # Outside the target week — must not count.
    await _log_sets(user_id, "Грудь", "Отжимания", "2026-07-01T10:00:00", 5)

    counts = await dbmod.weekly_volume_by_group(user_id, "2026-07-13", "2026-07-19")
    chest = await _group_id("Грудь")
    back = await _group_id("Спина")
    assert counts[chest] == 7  # 4 + 3, the July-1 workout excluded
    assert counts[back] == 6


@pytest.mark.asyncio
async def test_weekly_volume_empty_week(user_id):
    counts = await dbmod.weekly_volume_by_group(user_id, "2026-07-13", "2026-07-19")
    assert counts == {}


# ---------- screen ----------


def test_weekly_volume_screen_shows_icons_and_total():
    rows = [("Грудь", 7, "in_range"), ("Спина", 3, "low"), ("Ноги", 0, "none")]
    text = formatting.build_weekly_volume_screen(dt.date(2026, 7, 13), rows, is_current_week=True)
    assert "🟢 Грудь: <b>7</b>" in text
    assert "🟡 Спина: <b>3</b>" in text
    assert "▫️ Ноги: <b>0</b>" in text
    assert "Всего 10 подходов." in text


def test_weekly_volume_screen_empty():
    rows = [("Грудь", 0, "none"), ("Спина", 0, "none")]
    text = formatting.build_weekly_volume_screen(dt.date(2026, 7, 13), rows, is_current_week=True)
    assert "ещё нет ни одного подхода" in text
