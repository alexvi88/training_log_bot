"""Круговая смена кг↔lb не портит историю (db.scale_user_set_weights)."""

import pytest

import config

TYPICAL = [2.5, 5, 10, 25, 45, 135, 225, 1.25, 102.5]


async def _one_set_workout(db, user_id, weight, load_weight=None):
    gid = await db.create_muscle_group(user_id, "Ноги")
    ex_id = await db.create_exercise(user_id, "Присед", gid)
    wid = await db.create_finished_workout(user_id, "2026-01-01T10:00:00", "2026-01-01T10:30:00")
    block_id = await db.create_block(wid, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.append_set(block_id, ex_id, 0, weight, 5)
    return block_id


@pytest.mark.parametrize("weight", TYPICAL)
async def test_lb_kg_lb_roundtrip_is_exact(fresh_db, user_id, weight):
    db = fresh_db
    block_id = await _one_set_workout(db, user_id, weight)
    to_kg = 1 / config.LB_PER_KG
    await db.scale_user_set_weights(user_id, to_kg)
    await db.scale_user_set_weights(user_id, config.LB_PER_KG)
    (s,) = await db.list_sets_for_block(block_id)
    assert s["weight"] == weight


@pytest.mark.parametrize("weight", TYPICAL)
async def test_kg_lb_kg_roundtrip_is_exact_many_times(fresh_db, user_id, weight):
    db = fresh_db
    block_id = await _one_set_workout(db, user_id, weight)
    for _ in range(5):
        await db.scale_user_set_weights(user_id, config.LB_PER_KG)
        await db.scale_user_set_weights(user_id, 1 / config.LB_PER_KG)
    (s,) = await db.list_sets_for_block(block_id)
    assert s["weight"] == weight


def test_convert_weight_matches_sql_and_roundtrips():
    import db

    for w in TYPICAL:
        there = db.convert_weight(w, config.LB_PER_KG)
        assert db.convert_weight(there, 1 / config.LB_PER_KG) == w


async def test_progression_step_roundtrip(fresh_db, user_id):
    import db

    for step in (1.25, 2.5, 5):
        there = db.convert_weight(step, config.LB_PER_KG)
        assert db.convert_weight(there, 1 / config.LB_PER_KG) == step
