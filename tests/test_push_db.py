"""DB-layer plumbing for pushes: logging, dedup, tonnage."""

import datetime as dt

import pytest

pytestmark = pytest.mark.asyncio


async def test_record_and_list_recent_pushes_most_recent_first(fresh_db, user_id):
    db = fresh_db
    await db.record_push(user_id, "skip_3", "первое", "2026-05-04")
    await db.record_push(user_id, "win_back", "второе", "2026-05-04")

    assert await db.count_pushes() == 2
    rows = await db.list_recent_pushes(limit=10, offset=0)
    assert [r["text"] for r in rows] == ["второе", "первое"]
    assert rows[0]["username"] == "tester"


async def test_has_push_today_true_only_after_a_push_is_recorded(fresh_db, user_id):
    db = fresh_db
    today = "2026-05-04"
    assert await db.has_push_today(user_id, today) is False

    await db.record_push(user_id, "skip_3", "третий день", today)
    assert await db.has_push_today(user_id, today) is True


async def test_has_push_today_excludes_named_categories(fresh_db, user_id):
    """A one-off admin report reuses `pushes` for its own dedup bookkeeping —
    it must not eat the recipient's daily-rotation push slot for the day."""
    db = fresh_db
    today = "2026-05-04"
    await db.record_push(user_id, "admin_funnel_digest", "сводка", today)

    assert await db.has_push_today(user_id, today) is True
    assert await db.has_push_today(user_id, today, exclude_categories=("admin_funnel_digest",)) is False

    # A real daily-rotation push still counts, even alongside an excluded one.
    await db.record_push(user_id, "skip_3", "третий день", today)
    assert await db.has_push_today(user_id, today, exclude_categories=("admin_funnel_digest",)) is True


async def test_prune_old_pushes_keeps_recent_and_protected_categories(fresh_db, user_id):
    db = fresh_db
    # Relative to "now", not a fixed calendar date — prune_old_pushes's cutoff
    # is `now() - retention_days`, so a hardcoded "recent" date eventually
    # ages past that cutoff and starts getting pruned too.
    old_date = "2020-01-01T00:00:00"
    recent_date = (dt.datetime.now() - dt.timedelta(days=5)).isoformat(timespec="seconds")
    await db.conn().execute(
        "INSERT INTO pushes (telegram_id, category, text, sent_at, sent_on) VALUES (?, ?, ?, ?, ?)",
        (user_id, "skip_3", "старый", old_date, "2020-01-01"),
    )
    await db.conn().execute(
        "INSERT INTO pushes (telegram_id, category, text, sent_at, sent_on) VALUES (?, ?, ?, ?, ?)",
        (user_id, "release_x", "старый анонс", old_date, "2020-01-01"),
    )
    await db.conn().execute(
        "INSERT INTO pushes (telegram_id, category, text, sent_at, sent_on) VALUES (?, ?, ?, ?, ?)",
        (user_id, "skip_3", "свежий", recent_date, "2026-08-01"),
    )
    await db.conn().commit()

    deleted = await db.prune_old_pushes(30, keep_categories=("release_x",))

    assert deleted == 1  # only the old, non-protected row
    remaining = {(r["category"], r["text"]) for r in await db.list_recent_pushes(limit=10)}
    assert remaining == {("release_x", "старый анонс"), ("skip_3", "свежий")}


async def test_pushes_table_has_a_category_index(fresh_db):
    """count_announcement_recipients/list_announcement_recipients/has_announcement_push
    all filter `pushes` by category alone — see _migrate_schema."""
    cur = await fresh_db.conn().execute("PRAGMA index_list(pushes)")
    names = {row["name"] for row in await cur.fetchall()}
    assert "idx_pushes_category" in names


async def test_rotation_bag_round_trips(fresh_db, user_id):
    db = fresh_db
    assert await db.get_rotation_bag(user_id, "skip_3") == []
    await db.save_rotation_bag(user_id, "skip_3", [2, 0, 1])
    assert await db.get_rotation_bag(user_id, "skip_3") == [2, 0, 1]
    await db.save_rotation_bag(user_id, "skip_3", [1])
    assert await db.get_rotation_bag(user_id, "skip_3") == [1]


async def test_pushes_enabled_defaults_on_and_is_toggleable(fresh_db, user_id):
    db = fresh_db
    user = await db.get_user(user_id)
    assert user["pushes_enabled"] == 1

    await db.update_user(user_id, pushes_enabled=0)
    user = await db.get_user(user_id)
    assert user["pushes_enabled"] == 0


async def test_list_engagement_eligible_user_ids_excludes_opted_out(fresh_db, user_id):
    db = fresh_db
    other_id = 333
    await db.get_or_create_user(telegram_id=other_id, username="other")

    for uid in (user_id, other_id):
        await db.create_finished_workout(
            uid, started_at="2026-07-01T10:00:00", finished_at="2026-07-01T11:00:00"
        )

    assert {uid for uid, _tz in await db.list_engagement_eligible_user_ids()} == {user_id, other_id}

    await db.update_user(other_id, pushes_enabled=0)
    assert {uid for uid, _tz in await db.list_engagement_eligible_user_ids()} == {user_id}


async def test_engagement_pool_carries_the_timezone_offset(fresh_db, user_id):
    """The job runs hourly and sends at each user's own ENGAGEMENT_HOUR, so the
    pool has to say which zone they're in."""
    db = fresh_db
    await db.create_finished_workout(
        user_id, started_at="2026-07-01T10:00:00", finished_at="2026-07-01T11:00:00"
    )
    await db.update_user(user_id, tz_offset=5)

    assert await db.list_engagement_eligible_user_ids() == [(user_id, 5)]


async def test_list_newbie_user_ids_only_users_without_finished_workouts(fresh_db, user_id):
    db = fresh_db
    trained_id = 222
    await db.get_or_create_user(telegram_id=trained_id, username="trained")
    await db.create_finished_workout(
        trained_id, started_at="2026-07-01T10:00:00", finished_at="2026-07-01T11:00:00"
    )

    newbies = await db.list_newbie_user_ids()
    assert [uid for uid, _created, _tz in newbies] == [user_id]

    await db.update_user(user_id, pushes_enabled=0)
    assert await db.list_newbie_user_ids() == []


async def test_tonnage_since_sums_weight_times_reps(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    exercise_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, exercise_id, 0)
    await db.add_set(block_id, exercise_id, round_index=1, order_in_round=0, weight=100, reps=5)
    await db.add_set(block_id, exercise_id, round_index=2, order_in_round=0, weight=100, reps=5)
    await db.finish_workout(workout_id)

    since = (await db.get_workout(workout_id))["started_at"][:10]
    assert await db.tonnage_since(user_id, since) == 1000

    future = "2099-01-01"
    assert await db.tonnage_since(user_id, future) == 0


async def test_dedup_uses_the_users_own_date_not_the_servers(fresh_db, user_id, monkeypatch):
    """A user at UTC-7 gets their 19:00 push at 02:00 the *next* server day, so
    the row lands with tomorrow's server date. Deduping on date(sent_at) then
    answered "already pushed" on the user's next local day and silently ate
    every other day's push."""
    db = fresh_db
    local_day = "2026-05-04"
    # Server clock is already past midnight into the 5th when this goes out.
    monkeypatch.setattr(db, "now_iso", lambda: "2026-05-05T02:00:00")

    await db.record_push(user_id, "skip_3", "третий день", local_day)

    assert await db.has_push_today(user_id, local_day) is True
    assert await db.has_push_today(user_id, "2026-05-05") is False
