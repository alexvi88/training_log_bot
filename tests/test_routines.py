"""Routines: create/list/rename/delete and snapshotting a workout into a routine."""

import pytest

import db as dbmod


async def _group_id(name: str) -> int:
    groups = await dbmod.list_muscle_groups(None, global_only=True)
    return next(g["id"] for g in groups if g["name"] == name)


async def _finished_workout_with(user_id: int, ex_names: list[str]) -> tuple[int, list[int]]:
    gid = await _group_id("Грудь")
    wid = await dbmod.create_finished_workout(user_id, "2026-07-15T10:00:00", "2026-07-15T11:00:00")
    ex_ids = []
    for name in ex_names:
        ex_id = await dbmod.create_exercise(user_id, name, gid)
        ex_ids.append(ex_id)
        block_id = await dbmod.create_block(wid, "single")
        await dbmod.add_block_exercise(block_id, ex_id, 0)
        await dbmod.add_set(block_id, ex_id, 1, 0, 100.0, 8)
    return wid, ex_ids


@pytest.mark.asyncio
async def test_create_and_list_routine(user_id):
    rid = await dbmod.create_routine(user_id, "День груди")
    ex_gid = await _group_id("Грудь")
    ex_id = await dbmod.create_exercise(user_id, "Жим", ex_gid)
    await dbmod.add_routine_exercise(rid, ex_id, 0)

    routines = await dbmod.list_routines(user_id)
    assert len(routines) == 1
    assert routines[0]["name"] == "День груди"
    assert routines[0]["exercise_count"] == 1


@pytest.mark.asyncio
async def test_create_routine_from_workout_dedups_and_orders(user_id):
    wid, ex_ids = await _finished_workout_with(user_id, ["Жим", "Разведение", "Жим"])
    rid = await dbmod.create_routine_from_workout(user_id, wid, "Грудь")
    rexs = await dbmod.list_routine_exercises(rid)
    # "Жим" created once (unique display name), so 2 distinct exercises in order.
    names = [r["display_name"] for r in rexs]
    assert names == ["Жим", "Разведение"]


@pytest.mark.asyncio
async def test_list_routine_exercises_skips_archived(user_id):
    wid, ex_ids = await _finished_workout_with(user_id, ["Жим", "Разведение"])
    rid = await dbmod.create_routine_from_workout(user_id, wid, "Грудь")
    await dbmod.archive_exercise(ex_ids[0])
    rexs = await dbmod.list_routine_exercises(rid)
    assert [r["display_name"] for r in rexs] == ["Разведение"]


@pytest.mark.asyncio
async def test_rename_and_delete_routine(user_id):
    rid = await dbmod.create_routine(user_id, "Old")
    await dbmod.rename_routine(rid, "New")
    assert (await dbmod.get_routine(rid))["name"] == "New"

    await dbmod.delete_routine(rid)
    assert await dbmod.get_routine(rid) is None
    assert await dbmod.list_routines(user_id) == []
