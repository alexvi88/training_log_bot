"""Muscle-group recovery estimate on the exercise picker.

Answers "что сегодня логичнее" from what's already logged — no extra questions
to the user, and nothing is blocked or hidden. Fitbod's headline feature, minus
the model: how much work a group took and how long ago is enough for a nudge.
"""
import datetime as dt

import pytest

import analytics

pytestmark = pytest.mark.asyncio


async def test_recovery_is_zero_right_after_training_and_full_after_the_window():
    today = dt.date(2026, 5, 10)
    assert analytics.recovery_percent(today, 10, today) == 0
    assert analytics.recovery_percent(today - dt.timedelta(days=5), 10, today) == 100


async def test_a_heavier_session_recovers_more_slowly():
    today = dt.date(2026, 5, 10)
    two_days_ago = today - dt.timedelta(days=2)
    light = analytics.recovery_percent(two_days_ago, 4, today)
    heavy = analytics.recovery_percent(two_days_ago, 15, today)
    assert light > heavy
    assert 0 < heavy < 100


async def test_recovery_never_leaves_the_0_100_range():
    today = dt.date(2026, 5, 10)
    # A workout dated in the future (backfill typo, timezone edge) mustn't
    # produce a negative percentage on screen.
    assert analytics.recovery_percent(today + dt.timedelta(days=3), 10, today) == 0
    assert analytics.recovery_percent(today - dt.timedelta(days=365), 0, today) == 100


async def test_line_names_only_the_groups_still_short_of_recovered(fresh_db, user_id):
    from handlers import workout

    db = fresh_db
    legs = await db.create_muscle_group(user_id, "Ноги")
    back = await db.create_muscle_group(user_id, "Спина")
    squat = await db.create_exercise(user_id, "Присед", legs)
    row_ex = await db.create_exercise(user_id, "Тяга", back)

    today = dt.date.today()
    # Legs: hammered yesterday. Back: a light session two weeks ago.
    for ex_id, when, sets in ((squat, today, 12), (row_ex, today - dt.timedelta(days=14), 3)):
        workout_id = await db.create_workout(user_id, started_at=f"{when.isoformat()}T10:00:00")
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        for _ in range(sets):
            await db.append_set(block_id, ex_id, 0, 100.0, 5)
        await db.finish_workout(workout_id, finished_at=f"{when.isoformat()}T11:00:00")

    groups = await db.list_muscle_groups(user_id)
    line = await workout._recovery_line(user_id, groups)

    assert "ноги" in line
    assert "спина" not in line  # fully recovered — naming it would be noise


async def test_other_is_never_named_even_when_it_looks_spent(fresh_db, user_id):
    """«Другое» — мешок (пресс, предплечья, трапеции), а не мышца, которую можно
    поберечь сегодня: его процент в подсказке ничего не значит."""
    from handlers import workout

    db = fresh_db
    other = await db.create_muscle_group(user_id, "Другое")
    crunch = await db.create_exercise(user_id, "Скручивания", other)

    today = dt.date.today()
    workout_id = await db.create_workout(user_id, started_at=f"{today.isoformat()}T10:00:00")
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, crunch, 0)
    for _ in range(12):
        await db.append_set(block_id, crunch, 0, 0.0, 20)
    await db.finish_workout(workout_id, finished_at=f"{today.isoformat()}T11:00:00")

    groups = await db.list_muscle_groups(user_id)
    assert await workout._recovery_line(user_id, groups) == ""


async def test_no_line_at_all_when_everything_is_fresh(fresh_db, user_id):
    from handlers import workout

    db = fresh_db
    groups = await db.list_muscle_groups(user_id)
    assert await workout._recovery_line(user_id, groups) == ""
