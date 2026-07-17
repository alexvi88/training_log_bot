"""Ready-made workout programs: catalog integrity and instantiation into routines."""

import db as dbmod
from seed_data import EXERCISE_TEMPLATES, PROGRAM_BY_KEY, WORKOUT_PROGRAMS

# asyncio_mode=auto (pytest.ini) runs async tests without a marker; the pure-data
# checks below have no async def, so they run as plain sync tests.

_TEMPLATE_NAMES = {ex_name.strip().lower() for _group, ex_name in EXERCISE_TEMPLATES}


# ---------- pure data-integrity checks on the catalog ----------

def test_program_keys_are_unique():
    keys = [p["key"] for p in WORKOUT_PROGRAMS]
    assert len(keys) == len(set(keys))
    assert set(PROGRAM_BY_KEY) == set(keys)


def test_programs_have_days_and_exercises():
    for p in WORKOUT_PROGRAMS:
        assert p["days"], f"program {p['key']} has no days"
        for day_name, exercises in p["days"]:
            assert day_name.strip(), f"empty day name in {p['key']}"
            assert exercises, f"day {day_name!r} in {p['key']} has no exercises"


def test_every_program_exercise_exists_in_catalog():
    """Every referenced exercise must be a known global template so it resolves."""
    for p in WORKOUT_PROGRAMS:
        for _day_name, exercises in p["days"]:
            for ex in exercises:
                assert ex.strip().lower() in _TEMPLATE_NAMES, (
                    f"{ex!r} in program {p['key']} is not in EXERCISE_TEMPLATES"
                )


def test_callback_data_fits_telegram_limit():
    """rt:progadd:<key> is the longest callback and must stay under 64 bytes."""
    for p in WORKOUT_PROGRAMS:
        cb = f"rt:progadd:{p['key']}"
        assert len(cb.encode("utf-8")) <= 64, cb


# ---------- DB-level resolution & instantiation ----------

async def test_get_or_create_forks_global_template(user_id):
    before = await dbmod.count_user_exercises(user_id)
    ex_id = await dbmod.get_or_create_user_exercise_by_name(user_id, "Жим штанги лёжа")
    assert ex_id is not None
    ex = await dbmod.get_exercise(ex_id)
    assert ex["user_id"] == user_id
    assert await dbmod.count_user_exercises(user_id) == before + 1


async def test_get_or_create_reuses_existing_exercise(user_id):
    first = await dbmod.get_or_create_user_exercise_by_name(user_id, "Присед со штангой")
    again = await dbmod.get_or_create_user_exercise_by_name(user_id, "присед со штангой")  # different case
    assert first == again


async def test_get_or_create_unknown_name_returns_none(user_id):
    assert await dbmod.get_or_create_user_exercise_by_name(user_id, "Полёт на Марс") is None


async def test_create_routine_from_program_orders_and_forks(user_id):
    exercises = ["Присед со штангой", "Жим штанги лёжа", "Тяга штанги в наклоне"]
    rid = await dbmod.create_routine_from_program(user_id, "Всё тело A", exercises)
    rexs = await dbmod.list_routine_exercises(rid)
    assert [r["display_name"] for r in rexs] == exercises


async def test_create_routine_from_program_dedupes_and_skips_unknown(user_id):
    exercises = ["Присед со штангой", "Присед со штангой", "Полёт на Марс", "Жим штанги лёжа"]
    rid = await dbmod.create_routine_from_program(user_id, "Смесь", exercises)
    rexs = await dbmod.list_routine_exercises(rid)
    assert [r["display_name"] for r in rexs] == ["Присед со штангой", "Жим штанги лёжа"]


async def test_instantiating_a_full_program_creates_all_days(user_id):
    program = PROGRAM_BY_KEY["ppl"]
    for day_name, exercises in program["days"]:
        await dbmod.create_routine_from_program(user_id, day_name, exercises)

    routines = await dbmod.list_routines(user_id)
    assert len(routines) == len(program["days"])
    by_name = {r["name"]: r for r in routines}
    for day_name, exercises in program["days"]:
        assert by_name[day_name]["exercise_count"] == len(exercises)
        rexs = await dbmod.list_routine_exercises(by_name[day_name]["id"])
        assert [r["display_name"] for r in rexs] == exercises
