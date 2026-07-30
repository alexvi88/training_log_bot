"""The "➕ Суперсет" screen offers a couple of one-tap shortcuts for exercises
most often logged alongside the one already open — see db.list_superset_partners
and keyboards.groups_keyboard's partner_buttons.
"""
import pytest

import keyboards


async def _pair(db, user_id, workout_id, a_id, b_id):
    block_a = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_a, a_id, 0)
    block_b = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_b, b_id, 0)


@pytest.mark.asyncio
async def test_partners_ranked_by_shared_workout_count(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    pulldown = await db.create_exercise(user_id, "Pull down", group_id)
    row = await db.create_exercise(user_id, "Seated row", group_id)
    curl = await db.create_exercise(user_id, "Curl", group_id)

    for _ in range(3):
        w = await db.create_workout(user_id)
        await _pair(db, user_id, w, pulldown, row)
    w = await db.create_workout(user_id)
    await _pair(db, user_id, w, pulldown, curl)

    partners = await db.list_superset_partners(user_id, pulldown, limit=2)
    assert [p["id"] for p in partners] == [row, curl]


@pytest.mark.asyncio
async def test_partners_exclude_given_ids(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    pulldown = await db.create_exercise(user_id, "Pull down", group_id)
    row = await db.create_exercise(user_id, "Seated row", group_id)
    w = await db.create_workout(user_id)
    await _pair(db, user_id, w, pulldown, row)

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
