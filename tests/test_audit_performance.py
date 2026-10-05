"""Находки аудита про производительность: индексы по exercise_id, зал славы без
всей истории в Python, чистка таблиц по индексу. Каждый тест падает без фикса."""

import dataclasses
import datetime as dt
import random
from unittest.mock import AsyncMock

import httpx
import pytest

import api_v1
import db
import hall_of_fame_data


async def _plan(sql: str, params=()) -> str:
    cur = await db.conn().execute("EXPLAIN QUERY PLAN " + sql, params)
    return " | ".join(row[3] for row in await cur.fetchall())


# ---------- 11. индексы ----------


async def test_usage_count_lookup_uses_the_exercise_index(fresh_db, user_id):
    plan = await _plan(
        "SELECT COUNT(DISTINCT wb.workout_id) FROM block_exercises be "
        "JOIN workout_blocks wb ON wb.id = be.block_id WHERE be.exercise_id = ?", (1,)
    )
    assert "idx_block_exercises_exercise" in plan
    assert "SCAN be" not in plan


async def test_routine_exercise_lookup_uses_the_exercise_index(fresh_db, user_id):
    plan = await _plan("SELECT 1 FROM routine_exercises re WHERE re.exercise_id = ?", (1,))
    assert "idx_routine_exercises_exercise" in plan


async def test_list_user_exercises_query_plan_has_no_scan_per_row(fresh_db, user_id):
    captured = []
    real = db.conn().execute

    def spy(sql, params=()):
        captured.append((sql, params))
        return real(sql, params)

    db.conn().execute = spy
    try:
        await db.list_user_exercises(user_id)
    finally:
        db.conn().execute = real
    sql, params = captured[-1]
    plan = await _plan(sql, params)
    assert "SCAN be" not in plan and "SCAN re" not in plan, plan


# ---------- 13. prune по индексу ----------


@pytest.mark.parametrize(
    "prune, table, index",
    [
        ("prune_old_cost_events", "cost_events", "idx_cost_events_created"),
        ("prune_old_funnel_events", "funnel_events", "idx_funnel_events_created"),
        ("prune_old_user_events", "user_events", "idx_user_events_created"),
        ("prune_old_diagnostics", "diagnostics", "idx_diagnostics_created"),
    ],
)
async def test_prune_deletes_use_the_created_at_index(fresh_db, prune, table, index):
    captured = []
    real = db.conn().execute

    def spy(sql, params=()):
        captured.append((sql, params))
        return real(sql, params)

    db.conn().execute = spy
    try:
        await getattr(db, prune)(30)
    finally:
        db.conn().execute = real
    delete = next((s, p) for s, p in captured if s.startswith("DELETE FROM " + table))
    assert "date(" not in delete[0]
    assert index in await _plan(*delete)


async def test_prune_boundary_semantics_unchanged(fresh_db, user_id):
    cutoff = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    day_before = (dt.date.today() - dt.timedelta(days=31)).isoformat()
    for stamp in (day_before + "T23:59:59", cutoff + "T00:00:00", cutoff + "T23:59:59"):
        await db.conn().execute(
            "INSERT INTO cost_events (user_id, event_type, created_at) VALUES (?, 'llm_call', ?)",
            (user_id, stamp),
        )
    await db.conn().commit()

    assert await db.prune_old_cost_events(30) == 1  # только вчерашнее от границы

    cur = await db.conn().execute("SELECT created_at FROM cost_events ORDER BY created_at")
    assert [r[0] for r in await cur.fetchall()] == [cutoff + "T00:00:00", cutoff + "T23:59:59"]


# ---------- 12. зал славы ----------


async def _seed_random(user_id: int, seed: int) -> None:
    rng = random.Random(seed)
    c = db.conn()
    cur = await c.execute("SELECT id FROM muscle_groups WHERE user_id IS NULL LIMIT 1")
    gid = (await cur.fetchone())["id"]
    ex_ids = []
    for k in range(7):
        cur = await c.execute(
            "INSERT INTO exercises (user_id, name, primary_group_id, display_name, original_name, "
            "created_at, is_archived) VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00', ?)",
            (user_id, f"e{k}", gid, f"Упр {k}", f"Упр {k}", 1 if k == 6 else 0),
        )
        ex_ids.append(cur.lastrowid)
    stamps = [f"2026-0{1 + i // 10}-{1 + i % 10:02d}T10:00:00" for i in range(25)]
    for i in range(40):
        # Одинаковый started_at у соседних тренировок — ветка равных по порядку.
        started = stamps[min(i, 24) if i % 5 else max(i - 1, 0) % 25]
        status = "finished" if i % 9 else "active"
        cur = await c.execute(
            "INSERT INTO workouts (user_id, started_at, finished_at, status) VALUES (?, ?, ?, ?)",
            (user_id, started, started, status),
        )
        wid = cur.lastrowid
        for b in range(rng.randrange(1, 4)):
            cur = await c.execute(
                "INSERT INTO workout_blocks (workout_id, order_index, type) VALUES (?, ?, 'single')",
                (wid, b),
            )
            bid = cur.lastrowid
            ex = rng.choice(ex_ids)
            await c.execute(
                "INSERT INTO block_exercises (block_id, exercise_id, order_in_block) VALUES (?, ?, 0)",
                (bid, ex),
            )
            for r in range(rng.randrange(1, 6)):
                weight = rng.choice([0.0, 0.0, 20.0, 40.0, 40.0, 60.0, 62.5, 100.0])
                reps = rng.choice([1, 3, 5, 5, 8, 8, 12, 20, 40])
                rpe = rng.choice([None, None, 7.0, 8.0, 9.5, 10.0, 2.0])
                load = rng.choice([None, None, weight, weight + 80.0])
                await c.execute(
                    "INSERT INTO sets (block_id, exercise_id, round_index, order_in_round, weight, reps, "
                    "rpe, load_weight, created_at) VALUES (?, ?, ?, 0, ?, ?, ?, ?, '2026-01-01T00:00:00')",
                    (bid, ex, r, weight, reps, rpe, load),
                )
    await c.commit()


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("formula", ["epley", "brzycki"])
async def test_top_lifts_identical_to_the_full_history_version(fresh_db, user_id, monkeypatch, seed, formula):
    await _seed_random(user_id, seed)

    new = await hall_of_fame_data._top_lifts(user_id, formula)
    monkeypatch.setattr(db, "list_all_sets_by_exercise_deduped", db.list_all_sets_by_exercise)
    old = await hall_of_fame_data._top_lifts(user_id, formula)

    assert new == old
    assert new[0], "сид должен давать рекорды"


async def test_dedup_actually_sends_fewer_rows(fresh_db, user_id):
    c = db.conn()
    wid = (await c.execute(
        "INSERT INTO workouts (user_id, started_at, finished_at, status) "
        "VALUES (?, '2026-01-01T10:00:00', '2026-01-01T11:00:00', 'finished')", (user_id,))).lastrowid
    gid = (await (await c.execute("SELECT id FROM muscle_groups LIMIT 1")).fetchone())["id"]
    ex = (await c.execute(
        "INSERT INTO exercises (user_id, name, primary_group_id, display_name, original_name, created_at) "
        "VALUES (?, 'x', ?, 'Жим', 'Жим', '2026-01-01T00:00:00')", (user_id, gid))).lastrowid
    bid = (await c.execute(
        "INSERT INTO workout_blocks (workout_id, order_index, type) VALUES (?, 0, 'single')", (wid,))).lastrowid
    for r in range(10):
        await c.execute(
            "INSERT INTO sets (block_id, exercise_id, round_index, order_in_round, weight, reps, created_at) "
            "VALUES (?, ?, ?, 0, 100, 5, '2026-01-01T00:00:00')", (bid, ex, r))
    await c.commit()

    assert len(await db.list_all_sets_by_exercise(user_id)) == 10
    assert len(await db.list_all_sets_by_exercise_deduped(user_id)) == 1


async def test_rank_ladder_does_not_collect_lifts_again(fresh_db, monkeypatch):
    monkeypatch.setattr(hall_of_fame_data, "_top_lifts", AsyncMock(side_effect=AssertionError("full collect")))
    await db.get_or_create_user(telegram_id=111, username="t")
    code = await db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"

    resp = await client.get("/hall-of-fame/rank-ladder")

    assert resp.status_code == 200, resp.text
    assert resp.json()["ranks"]


async def test_collect_without_lifts_keeps_rank_and_frequency(fresh_db, user_id):
    await db.create_finished_workout(user_id, "2026-01-01T10:00:00", "2026-01-01T11:00:00")
    full = await hall_of_fame_data.collect(user_id)
    light = await hall_of_fame_data.collect(user_id, with_lifts=False)
    assert light.rank == full.rank and light.per_week == full.per_week
    assert light.top_lifts == [] and light.total_workouts == full.total_workouts
    assert dataclasses.replace(
        light, top_lifts=full.top_lifts, top_lift_ids=full.top_lift_ids,
        tonnage_equivalent=full.tonnage_equivalent, longest_workout_seconds=full.longest_workout_seconds,
    ) == full
