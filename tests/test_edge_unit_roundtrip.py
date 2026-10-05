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


def _grid(step, top=500):
    return [round(i * step, 2) for i in range(1, int(top / step) + 1)]


@pytest.mark.parametrize("step", [0.25, 0.05])
def test_every_grid_value_roundtrips_exactly_both_ways(step):
    import db

    bad = []
    for w in _grid(step):
        for there, back in ((1 / config.LB_PER_KG, config.LB_PER_KG), (config.LB_PER_KG, 1 / config.LB_PER_KG)):
            if db.convert_weight(db.convert_weight(w, there), back) != w:
                bad.append((w, there))
    assert bad == []


async def test_sql_path_matches_python_on_every_quarter(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Ноги")
    ex_id = await db.create_exercise(user_id, "Присед", gid)
    wid = await db.create_finished_workout(user_id, "2026-01-01T10:00:00", "2026-01-01T10:30:00")
    block_id = await db.create_block(wid, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    weights = _grid(0.25)
    for w in weights:
        await db.append_set(block_id, ex_id, 0, w, 5)
    for factor in (1 / config.LB_PER_KG, config.LB_PER_KG):
        await db.scale_user_set_weights(user_id, factor)
    got = sorted(s["weight"] for s in await db.list_sets_for_block(block_id))
    assert got == weights


async def test_bodyweight_roundtrip_and_undo_match(fresh_db, user_id):
    db = fresh_db
    import fsm_unit_rescale

    await db.conn().execute(
        "INSERT INTO bodyweight_logs (telegram_id, weight, logged_at) VALUES (?, ?, ?)",
        (user_id, 80.0, "2026-01-02"),
    )
    await db.conn().commit()
    undo = {"kind": "bodyweight_restore", "weight": 80.0}
    pending = {"bw_pending_weight": 80.0}
    for factor in (config.LB_PER_KG, 1 / config.LB_PER_KG):
        await db.scale_bodyweight_logs(user_id, factor)
        db.scale_ai_undo_weights(undo, factor)
        pending.update(fsm_unit_rescale.weight_cache_updates(pending, factor))
        if factor > 1:
            cur = await db.conn().execute(
                "SELECT weight FROM bodyweight_logs WHERE telegram_id = ?", (user_id,)
            )
            lb = [r["weight"] for r in await cur.fetchall()]
            assert set(lb) == {undo["weight"]} == {pending["bw_pending_weight"]}
    cur = await db.conn().execute("SELECT weight FROM bodyweight_logs WHERE telegram_id = ?", (user_id,))
    assert {r["weight"] for r in await cur.fetchall()} == {80.0}
    assert undo["weight"] == 80.0 and pending["bw_pending_weight"] == 80.0
