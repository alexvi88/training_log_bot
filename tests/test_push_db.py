"""DB-layer plumbing for pushes: logging, dedup, tonnage."""

import pytest

pytestmark = pytest.mark.asyncio


async def test_record_and_list_recent_pushes_most_recent_first(fresh_db, user_id):
    db = fresh_db
    await db.record_push(user_id, "skip_3", "первое")
    await db.record_push(user_id, "win_back", "второе")

    assert await db.count_pushes() == 2
    rows = await db.list_recent_pushes(limit=10, offset=0)
    assert [r["text"] for r in rows] == ["второе", "первое"]
    assert rows[0]["username"] == "tester"


async def test_has_push_today_true_only_after_a_push_is_recorded(fresh_db, user_id):
    db = fresh_db
    today = db.now_iso()[:10]
    assert await db.has_push_today(user_id, today) is False

    await db.record_push(user_id, "skip_3", "третий день")
    assert await db.has_push_today(user_id, today) is True


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

    assert set(await db.list_engagement_eligible_user_ids()) == {user_id, other_id}

    await db.update_user(other_id, pushes_enabled=0)
    assert set(await db.list_engagement_eligible_user_ids()) == {user_id}


async def test_list_newbie_user_ids_only_users_without_finished_workouts(fresh_db, user_id):
    db = fresh_db
    trained_id = 222
    await db.get_or_create_user(telegram_id=trained_id, username="trained")
    await db.create_finished_workout(
        trained_id, started_at="2026-07-01T10:00:00", finished_at="2026-07-01T11:00:00"
    )

    newbies = await db.list_newbie_user_ids()
    assert [uid for uid, _ in newbies] == [user_id]

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
