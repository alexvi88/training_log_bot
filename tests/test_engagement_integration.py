"""End-to-end: build_daily_push wired against a real (in-memory) DB."""

import datetime as dt

import pytest

import engagement
import push_texts

pytestmark = pytest.mark.asyncio


async def test_build_daily_push_fires_skip_milestone(fresh_db, user_id):
    db = fresh_db
    workout_id = await db.create_finished_workout(
        user_id, started_at="2026-07-05T10:00:00", finished_at="2026-07-05T11:00:00"
    )
    assert workout_id

    today = dt.date(2026, 7, 12)  # 7 days after the workout above
    decision = await engagement.build_daily_push(user_id, today)

    assert decision is not None
    assert decision.category == push_texts.SKIP
    assert "АТЛЕТ" in decision.text


async def test_build_daily_push_respects_one_per_day_dedup(fresh_db, user_id):
    db = fresh_db
    await db.create_finished_workout(
        user_id, started_at="2026-07-05T10:00:00", finished_at="2026-07-05T11:00:00"
    )
    today = dt.date(2026, 7, 12)
    await db.record_push(user_id, push_texts.SKIP, "already sent today")

    decision = await engagement.build_daily_push(user_id, today)
    assert decision is None


async def test_build_daily_push_none_for_user_without_workouts(fresh_db, user_id):
    decision = await engagement.build_daily_push(user_id, dt.date(2026, 7, 12))
    assert decision is None


async def test_build_daily_push_fires_challenge_progress_nudge_on_thursday(fresh_db, user_id):
    db = fresh_db
    # 40 days out: not a skip milestone (3/5/7/10/14) and not a win-back day
    # (40 - 21) % 10 != 0 -- isolates the Thursday challenge-progress branch.
    await db.create_finished_workout(
        user_id, started_at="2026-05-30T10:00:00", finished_at="2026-05-30T11:00:00"
    )
    thursday = dt.date(2026, 7, 9)
    assert thursday.weekday() == 3

    decision = await engagement.build_daily_push(user_id, thursday)

    assert decision is not None
    assert decision.category == push_texts.CHALLENGE
