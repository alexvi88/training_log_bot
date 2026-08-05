"""End-to-end: build_daily_push wired against a real (in-memory) DB."""

import datetime as dt

import pytest

import engagement
import push_texts

pytestmark = pytest.mark.asyncio


async def test_build_daily_push_fires_the_matching_skip_milestone(fresh_db, user_id):
    db = fresh_db
    await db.create_finished_workout(
        user_id, started_at="2026-07-05T10:00:00", finished_at="2026-07-05T11:00:00"
    )

    today = dt.date(2026, 7, 12)  # exactly 7 days after the workout above
    decision = await engagement.build_daily_push(user_id, today)

    assert decision is not None
    assert decision.category == push_texts.SKIP_7
    assert "АТЛЕТ" in decision.text
    # a day-7 skip must never draw a day-14 line ("две недели"/"четырнадцать")
    assert "недел" in decision.text.lower()
    assert "четырнадцат" not in decision.text.lower()


async def test_build_daily_push_respects_one_per_day_dedup(fresh_db, user_id):
    db = fresh_db
    await db.create_finished_workout(
        user_id, started_at="2026-07-05T10:00:00", finished_at="2026-07-05T11:00:00"
    )
    today = dt.date(2026, 7, 12)
    await db.record_push(user_id, push_texts.SKIP_7, "already sent today", today.isoformat())

    decision = await engagement.build_daily_push(user_id, today)
    assert decision is None


async def test_build_daily_push_two_days_out_is_silent(fresh_db, user_id):
    db = fresh_db
    await db.create_finished_workout(
        user_id, started_at="2026-07-10T10:00:00", finished_at="2026-07-10T11:00:00"
    )
    today = dt.date(2026, 7, 12)  # 2 days later — below the first milestone (3)

    decision = await engagement.build_daily_push(user_id, today)
    assert decision is None


async def test_build_daily_push_none_for_user_without_workouts(fresh_db, user_id):
    decision = await engagement.build_daily_push(user_id, dt.date(2026, 7, 12))
    assert decision is None


async def test_build_newbie_push_fires_day_after_signup(fresh_db, user_id):
    decision = await engagement.build_newbie_push(user_id, "2026-07-11T09:00:00", dt.date(2026, 7, 12))
    assert decision is not None
    assert decision.category == push_texts.NEWBIE_NUDGE
    assert "АТЛЕТ" in decision.text


async def test_build_newbie_push_silent_same_day_as_signup(fresh_db, user_id):
    decision = await engagement.build_newbie_push(user_id, "2026-07-12T09:00:00", dt.date(2026, 7, 12))
    assert decision is None


async def test_build_newbie_push_respects_one_per_day_dedup(fresh_db, user_id):
    db = fresh_db
    today = dt.date(2026, 7, 12)
    await db.record_push(user_id, push_texts.NEWBIE_NUDGE, "already sent today", today.isoformat())

    decision = await engagement.build_newbie_push(user_id, "2026-07-11T09:00:00", today)
    assert decision is None


async def test_monday_streak_milestone_beats_the_skip_jab(fresh_db, user_id):
    """Four consecutive training weeks, checked on the following Monday: the
    celebration must win over the skip-5 jab that fires the same day."""
    db = fresh_db
    for day in ("2026-06-17", "2026-06-24", "2026-07-01", "2026-07-08"):  # 4 weeks in a row
        await db.create_finished_workout(
            user_id, started_at=f"{day}T10:00:00", finished_at=f"{day}T11:00:00"
        )

    decision = await engagement.build_daily_push(user_id, dt.date(2026, 7, 13))  # Monday, 5 days out

    assert decision is not None
    assert decision.category == push_texts.STREAK_MILESTONE
    assert "4 недели" in decision.text


async def _seed_heavy_workout(db, ex_id, user_id, day: str):
    workout_id = await db.create_finished_workout(
        user_id, started_at=f"{day}T10:00:00", finished_at=f"{day}T11:00:00"
    )
    block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block, ex_id, 0)
    await db.add_set(block, ex_id, 1, 0, 100.0, 8)
    await db.add_set(block, ex_id, 2, 0, 100.0, 8)
    await db.add_set(block, ex_id, 3, 0, 100.0, 8)


async def test_friday_rank_near_push_names_the_rank_and_the_gap(fresh_db, user_id):
    """13 workouts, ~31 т, 2+ раза в неделю: до «Работяги» (15 тренировок)
    ровно две — в пятницу приходит пуш с целью."""
    db = fresh_db
    gid = (await db.list_muscle_groups(None, global_only=True))[0]["id"]
    ex_id = await db.create_exercise(user_id, "Жим", gid)
    for k in range(13):
        day = (dt.date(2026, 7, 16) - dt.timedelta(days=2 * k)).isoformat()
        await _seed_heavy_workout(db, ex_id, user_id, day)

    decision = await engagement.build_daily_push(user_id, dt.date(2026, 7, 17))  # Friday, 1 day out

    assert decision is not None
    assert decision.category == push_texts.RANK_NEAR
    assert "Работяга" in decision.text
    assert "2 тренировки" in decision.text


async def _seed_recent_workout(db, user_id):
    # Sunday 2026-07-19; a workout the day before → tonnage>0, day-since-last not a milestone.
    await db.create_finished_workout(
        user_id, started_at="2026-07-18T10:00:00", finished_at="2026-07-18T11:00:00"
    )
    block = await db.create_block(
        (await db.list_workouts(user_id))[0]["id"], "single"
    )
    gid = (await db.list_muscle_groups(None, global_only=True))[0]["id"]
    ex_id = await db.create_exercise(user_id, "Жим", gid)
    await db.add_block_exercise(block, ex_id, 0)
    await db.add_set(block, ex_id, 1, 0, 100.0, 8)


async def test_sunday_digest_uses_ai_when_available(fresh_db, user_id, monkeypatch):
    await _seed_recent_workout(fresh_db, user_id)
    monkeypatch.setattr(engagement.ai_trainer, "is_configured", lambda: True)

    async def fake_digest(uid):
        return "ПРИВЕТ АТЛЕТ, неделя выдалась крепкой."

    monkeypatch.setattr(engagement.ai_trainer, "weekly_digest", fake_digest)

    decision = await engagement.build_daily_push(user_id, dt.date(2026, 7, 19))  # Sunday
    assert decision is not None
    assert decision.category == push_texts.AI_WEEKLY
    assert decision.text.startswith("ПРИВЕТ АТЛЕТ")


async def test_sunday_digest_falls_back_to_static_when_ai_returns_none(fresh_db, user_id, monkeypatch):
    await _seed_recent_workout(fresh_db, user_id)
    monkeypatch.setattr(engagement.ai_trainer, "is_configured", lambda: True)

    async def no_digest(uid):
        return None

    monkeypatch.setattr(engagement.ai_trainer, "weekly_digest", no_digest)

    decision = await engagement.build_daily_push(user_id, dt.date(2026, 7, 19))
    assert decision is not None
    assert decision.category == push_texts.WEEKLY_DIGEST
