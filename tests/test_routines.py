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
async def test_workout_exercise_summary_lists_names_in_order(user_id):
    from handlers.routines import _workout_exercise_summary

    wid, _ex_ids = await _finished_workout_with(user_id, ["Жим", "Разведение", "Жим"])
    assert await _workout_exercise_summary(wid) == "Жим, Разведение"


@pytest.mark.asyncio
async def test_workout_exercise_summary_truncates_long_lists(user_id):
    from handlers.routines import _SOURCE_PICKER_SUMMARY_MAX, _workout_exercise_summary

    wid, _ex_ids = await _finished_workout_with(
        user_id, ["Приседания со штангой на плечах", "Жим штанги лёжа широким хватом"]
    )
    summary = await _workout_exercise_summary(wid)
    assert summary.endswith("…")
    assert len(summary) <= _SOURCE_PICKER_SUMMARY_MAX + 1


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


# ---------- multi-day programs are one row in the list, days behind it ----------


@pytest.mark.asyncio
async def test_program_days_group_under_one_row_and_standalone_routines_stay_separate(user_id):
    for day in ("День 1 — Жим", "День 2 — Тяга", "День 3 — Ноги"):
        await dbmod.create_routine(user_id, day, program_name="PPL гипертрофия")
    await dbmod.create_routine(user_id, "Своя тренировка")

    programs = await dbmod.list_programs(user_id)
    standalone = await dbmod.list_standalone_routines(user_id)

    assert [(p["program_name"], p["day_count"]) for p in programs] == [("PPL гипертрофия", 3)]
    assert [r["name"] for r in standalone] == ["Своя тренировка"]


@pytest.mark.asyncio
async def test_program_days_are_listed_in_day_order(user_id):
    for day in ("День 1", "День 2", "День 3"):
        await dbmod.create_routine(user_id, day, program_name="Сплит")

    days = await dbmod.list_program_days(user_id, "Сплит")

    assert [d["name"] for d in days] == ["День 1", "День 2", "День 3"]


@pytest.mark.asyncio
async def test_the_anchor_of_a_program_belongs_to_it(user_id):
    """Программа адресуется id одного из своих дней — по нему хендлер и находит
    остальные, так что якорь обязан быть её же днём."""
    for day in ("День 1", "День 2"):
        await dbmod.create_routine(user_id, day, program_name="Верх/низ")

    program = (await dbmod.list_programs(user_id))[0]
    anchor = await dbmod.get_routine(program["anchor_id"])

    assert anchor["program_name"] == "Верх/низ"


# ---------- recovering the grouping of days named before program_name existed ----------


async def _force_legacy_shape(user_id: int, names: list[str]) -> None:
    """Routines as they looked before `program_name`: the program, if any, lived
    in the name itself."""
    for name in names:
        rid = await dbmod.create_routine(user_id, name)
        await dbmod._conn.execute("UPDATE routines SET program_name = NULL WHERE id = ?", (rid,))
    await dbmod._conn.execute("PRAGMA user_version = 1")
    await dbmod._conn.commit()


@pytest.mark.asyncio
async def test_migration_splits_a_shared_prefix_back_into_program_and_day(user_id):
    await _force_legacy_shape(user_id, [
        "PPL гипертрофия 3 дня — День 1 — Жим",
        "PPL гипертрофия 3 дня — День 2 — Тяга",
        "PPL гипертрофия 3 дня — День 3 — Ноги",
    ])

    await dbmod._run_one_shot_migrations()

    programs = await dbmod.list_programs(user_id)
    assert [(p["program_name"], p["day_count"]) for p in programs] == [("PPL гипертрофия 3 дня", 3)]
    days = await dbmod.list_program_days(user_id, "PPL гипертрофия 3 дня")
    assert [d["name"] for d in days] == ["День 1 — Жим", "День 2 — Тяга", "День 3 — Ноги"]
    assert await dbmod.list_standalone_routines(user_id) == []


@pytest.mark.asyncio
async def test_migration_leaves_a_lone_routine_with_a_dash_alone(user_id):
    """Один роутин с тире в названии — не программа, и переименовывать его
    («Грудь — тяжёлая» → «тяжёлая» под программой «Грудь») было бы порчей данных."""
    await _force_legacy_shape(user_id, ["Грудь — тяжёлая", "Спина"])

    await dbmod._run_one_shot_migrations()

    assert await dbmod.list_programs(user_id) == []
    assert sorted(r["name"] for r in await dbmod.list_standalone_routines(user_id)) == [
        "Грудь — тяжёлая", "Спина",
    ]


@pytest.mark.asyncio
async def test_migration_leaves_days_that_never_carried_a_program_name(user_id):
    """Дни, сохранённые ещё до префикса, не несут в себе никакой информации о
    программе — придумывать ей имя хуже, чем оставить их одиночными."""
    await _force_legacy_shape(user_id, ["День 1 — Жим", "День 2 — Тяга", "День 3 — Ноги"])

    await dbmod._run_one_shot_migrations()

    assert await dbmod.list_programs(user_id) == []
    assert len(await dbmod.list_standalone_routines(user_id)) == 3


@pytest.mark.asyncio
async def test_migration_does_not_regroup_routines_named_after_it_ran(user_id):
    await dbmod._run_one_shot_migrations()  # once-per-DB pass, nothing to do yet

    await dbmod.create_routine(user_id, "Тяга — с пола")
    await dbmod.create_routine(user_id, "Тяга — с плинтов")
    await dbmod._run_one_shot_migrations()  # every later startup

    assert await dbmod.list_programs(user_id) == []
    assert len(await dbmod.list_standalone_routines(user_id)) == 2
