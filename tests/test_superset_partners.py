"""The "➕ Суперсет" screen offers a couple of one-tap shortcuts for exercises
actually worked as a superset with the one already open — see
db.list_superset_partners and keyboards.groups_keyboard's partner_buttons.

"Worked as a superset" is inferred from each exercise's set timestamps
overlapping within a workout (switching back and forth), not just both
having been logged somewhere in the same workout — two exercises done one
after the other, start to finish, are not a superset even though they
share a workout_id.
"""
import datetime as dt

import pytest

import keyboards


async def _log_set_at(db, block_id, exercise_id, when: dt.datetime):
    await db.conn().execute(
        "INSERT INTO sets (block_id, exercise_id, round_index, order_in_round, weight, reps, created_at) "
        "VALUES (?, ?, 0, 0, 100, 5, ?)",
        (block_id, exercise_id, when.isoformat()),
    )
    await db.conn().commit()


async def _superset(db, workout_id, a_id, b_id, start: dt.datetime):
    """Two exercises whose sets interleave in time — an actual superset."""
    block_a = await db.create_block(workout_id, "single")
    block_b = await db.create_block(workout_id, "single")
    await _log_set_at(db, block_a, a_id, start)
    await _log_set_at(db, block_b, b_id, start + dt.timedelta(seconds=30))
    await _log_set_at(db, block_a, a_id, start + dt.timedelta(seconds=60))
    await _log_set_at(db, block_b, b_id, start + dt.timedelta(seconds=90))


async def _sequential(db, workout_id, a_id, b_id, start: dt.datetime):
    """Two exercises done one after the other, start to finish — not a superset."""
    block_a = await db.create_block(workout_id, "single")
    await _log_set_at(db, block_a, a_id, start)
    await _log_set_at(db, block_a, a_id, start + dt.timedelta(seconds=30))
    block_b = await db.create_block(workout_id, "single")
    await _log_set_at(db, block_b, b_id, start + dt.timedelta(minutes=5))
    await _log_set_at(db, block_b, b_id, start + dt.timedelta(minutes=5, seconds=30))


@pytest.mark.asyncio
async def test_partners_ranked_by_shared_workout_count(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    pulldown = await db.create_exercise(user_id, "Pull down", group_id)
    row = await db.create_exercise(user_id, "Seated row", group_id)
    curl = await db.create_exercise(user_id, "Curl", group_id)

    for i in range(3):
        w = await db.create_workout(user_id)
        await _superset(db, w, pulldown, row, dt.datetime(2026, 1, 1 + i, 10, 0))
    w = await db.create_workout(user_id)
    await _superset(db, w, pulldown, curl, dt.datetime(2026, 2, 1, 10, 0))

    partners = await db.list_superset_partners(user_id, pulldown, limit=2)
    assert [p["id"] for p in partners] == [row, curl]


@pytest.mark.asyncio
async def test_sequential_same_workout_exercises_are_not_partners(fresh_db, user_id):
    """Two exercises just done back to back in the same workout (not
    switched between) should not be offered as a superset shortcut."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    pulldown = await db.create_exercise(user_id, "Pull down", group_id)
    deadlift = await db.create_exercise(user_id, "Conventional deadlift", group_id)
    w = await db.create_workout(user_id)
    await _sequential(db, w, deadlift, pulldown, dt.datetime(2026, 1, 1, 10, 0))

    partners = await db.list_superset_partners(user_id, pulldown, limit=2)
    assert partners == []


@pytest.mark.asyncio
async def test_partners_exclude_given_ids(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    pulldown = await db.create_exercise(user_id, "Pull down", group_id)
    row = await db.create_exercise(user_id, "Seated row", group_id)
    w = await db.create_workout(user_id)
    await _superset(db, w, pulldown, row, dt.datetime(2026, 1, 1, 10, 0))

    partners = await db.list_superset_partners(user_id, pulldown, limit=2, exclude_ids=(row,))
    assert partners == []


@pytest.mark.asyncio
async def test_partners_empty_with_no_history(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    pulldown = await db.create_exercise(user_id, "Pull down", group_id)

    partners = await db.list_superset_partners(user_id, pulldown, limit=2)
    assert partners == []


def test_groups_keyboard_omits_partner_row_when_none():
    kb = keyboards.groups_keyboard([], prefix="pick", show_all=True, partner_buttons=[])
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert not any(cb.startswith("pick:partner:") for cb in callback_datas)


def test_groups_keyboard_shows_partner_shortcuts():
    kb = keyboards.groups_keyboard(
        [], prefix="pick", show_all=True, partner_buttons=[(5, "Seated row")]
    )
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "pick:partner:5" in callback_datas


def test_groups_keyboard_gives_each_partner_shortcut_a_full_row():
    """adjust(2) reflows the whole builder, so partner rows added before it used
    to end up sharing a half-width column with a group — long enough to clip
    every real exercise name."""
    kb = keyboards.groups_keyboard(
        [{"id": 1, "name": "Спина"}, {"id": 2, "name": "Грудь"}],
        prefix="pick",
        show_all=True,
        partner_buttons=[(5, "triceps block - single arm - cuff"), (6, "Seated row")],
    )
    partner_rows = [
        row for row in kb.inline_keyboard
        if any(b.callback_data.startswith("pick:partner:") for b in row)
    ]
    assert [len(r) for r in partner_rows] == [1, 1]
    assert partner_rows[0][0].text == "⚡ triceps block - single arm - cuff"
    # Groups themselves still pair up two to a row, uppercase.
    group_row = next(
        row for row in kb.inline_keyboard
        if any(b.callback_data == "pick:grp:1" for b in row)
    )
    assert [b.text for b in group_row] == ["СПИНА", "ГРУДЬ"]
