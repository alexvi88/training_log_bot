"""db.get_or_create_backfill_workout — the same check-then-insert race that
get_or_create_active_workout was written to close for the active workout,
now closed for backfill too.

api_v1.start_backfill_workout used to call db.get_backfill_workout, then
(after an await) db.create_workout separately. Two concurrent starts — bot
and app, or a double tap — could both see "no backfill workout" before either
inserted, and both would create one; get_backfill_workout's
`ORDER BY id LIMIT 1` means the loser's row becomes an invisible permanent
ghost. Check and insert must happen under the same _write_lock acquisition,
exactly like get_or_create_active_workout.
"""
import asyncio

import pytest

pytestmark = pytest.mark.asyncio


async def test_concurrent_backfill_starts_create_only_one_workout(fresh_db, user_id):
    db = fresh_db
    started_at = "2026-09-10T12:00:00"

    results = await asyncio.gather(
        db.get_or_create_backfill_workout(user_id, started_at),
        db.get_or_create_backfill_workout(user_id, started_at),
    )

    ids = {results[0][0], results[1][0]}
    assert len(ids) == 1, "two concurrent starts must resolve to the same backfill workout"
    assert sorted(r[1] for r in results) == [False, True], (
        "exactly one caller should see created=True (the other gets the existing row)"
    )

    cur = await db.conn().execute(
        "SELECT COUNT(*) FROM workouts WHERE user_id = ? AND status = 'backfill'",
        (user_id,),
    )
    (count,) = await cur.fetchone()
    assert count == 1


async def test_concurrent_backfill_starts_over_http_create_only_one_workout(fresh_db):
    """Same race, exercised through the actual HTTP handler two concurrent
    clients would hit."""
    import httpx

    import api_v1

    telegram_id = 222
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    transport = httpx.ASGITransport(app=api_v1.build_app())
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"

    responses = await asyncio.gather(
        client.post("/workouts/backfill", json={"date": "2026-09-10"}),
        client.post("/workouts/backfill", json={"date": "2026-09-10"}),
    )

    ids = {r.json()["id"] for r in responses}
    assert len(ids) == 1
    cur = await fresh_db.conn().execute(
        "SELECT COUNT(*) FROM workouts WHERE user_id = ? AND status = 'backfill'",
        (telegram_id,),
    )
    (count,) = await cur.fetchone()
    assert count == 1
