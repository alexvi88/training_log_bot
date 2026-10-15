"""Одноразовые миграции v13 и v14.

v13 — копии программ, назначенные тренером до #847 (на языке тренера, без
ключа каталога), переводятся на язык атлета, ключ записывается. Своё имя
тренера остаётся как набрано.
v14 — автокомментарий AI выключается у всех один раз; кто включил снова —
тому рестарт его не выключит.
"""

from __future__ import annotations

import pytest

import db
import formatting
import i18n
import seed_data

pytestmark = pytest.mark.asyncio

ATHLETE = 902
BENCH = "Жим штанги лёжа"


async def _rerun(from_version: int) -> None:
    await db.conn().execute(f"PRAGMA user_version = {from_version}")
    await db.conn().commit()
    await db._run_one_shot_migrations()


async def _athlete(lang: str) -> None:
    await db.get_or_create_user(telegram_id=ATHLETE, username="athlete")
    await db.set_user_lang(ATHLETE, lang)


async def _old_coach_copy_of_catalog(key: str, lang: str) -> int:
    """Как ложилась копия до #847: снимок на языке тренера, source='coach', без ключа."""
    with i18n.use_lang(lang):
        program_id = await seed_data.instantiate_program(
            ATHLETE, key, seed_data.localized_program_name(key, lang)
        )
    await db.conn().execute(
        "UPDATE programs SET source = 'coach', source_ref = NULL WHERE id = ?", (program_id,)
    )
    await db.conn().commit()
    return program_id


def _key_with_seconds() -> str:
    return next(p["key"] for p in seed_data.WORKOUT_PROGRAMS if any(
        target.endswith(" сек") for _day, exercises in p["days"] for _name, target in exercises
    ))


async def test_old_ru_coach_copy_lands_in_english_with_key(fresh_db):
    await _athlete("en")
    key = _key_with_seconds()
    program_id = await _old_coach_copy_of_catalog(key, "ru")
    assert (await db.get_program(program_id))["name"] == seed_data.localized_program_name(key, "ru")

    await _rerun(12)

    program = await db.get_program(program_id)
    assert program["source"] == "coach" and program["source_ref"] == key
    assert program["name"] == seed_data.localized_program_name(key, "en")
    assert program["description"] == db.clean_program_description(
        seed_data.localized_program_description(key, "en")
    )
    days = await db.list_program_days_by_id(program_id)
    assert [d["name"] for d in days] == [
        seed_data.localized_program_day_name(key, i, "en") for i in range(len(days))
    ]
    rows = [r for d in days for r in await db.list_routine_exercises(d["id"])]
    targets = [r["target"] for r in rows if r["target"]]
    assert any(t.endswith(" sec") for t in targets), targets
    assert not any(t.endswith(" сек") for t in targets), targets
    for r in rows:
        ex = await db.get_exercise(r["exercise_id"])
        assert ex["name"] == seed_data.localized_exercise_name(ex["original_name"], "en")


async def test_renamed_program_is_recognised_by_its_days(fresh_db):
    """Тренер переименовал каталожную программу — узнаём её по дням; имя — как набрано."""
    await _athlete("en")
    key = _key_with_seconds()
    program_id = await _old_coach_copy_of_catalog(key, "ru")
    await db.rename_program_by_id(program_id, "Масса для Маши")

    await _rerun(12)

    program = await db.get_program(program_id)
    assert program["source_ref"] == key
    assert program["name"] == "Масса для Маши"
    days = await db.list_program_days_by_id(program_id)
    assert days[0]["name"] == seed_data.localized_program_day_name(key, 0, "en")


async def test_custom_names_stay_as_typed_but_schemes_translate(fresh_db):
    await _athlete("en")
    program_id = await db.create_program(ATHLETE, "Блок силы", source="coach")
    day = await db.create_routine(ATHLETE, "Тяжёлый день", program_id=program_id)
    with i18n.use_lang("ru"):
        bench = await db.get_or_create_user_exercise_by_name(ATHLETE, BENCH)
        await db.add_routine_exercise(day, bench, 0, formatting.build_routine_target(3, None, None))

    await _rerun(12)

    program = await db.get_program(program_id)
    assert program["name"] == "Блок силы" and program["source_ref"] is None
    [copy_day] = await db.list_program_days_by_id(program_id)
    assert copy_day["name"] == "Тяжёлый день"
    [row] = await db.list_routine_exercises(copy_day["id"])
    assert row["target"] == "3 sets"
    assert row["display_name"] == seed_data.localized_exercise_name(BENCH, "en")


async def test_athlete_edits_survive_and_rerun_is_idempotent(fresh_db):
    await _athlete("en")
    key = _key_with_seconds()
    program_id = await _old_coach_copy_of_catalog(key, "ru")
    days = await db.list_program_days_by_id(program_id)
    await db.rename_routine(days[0]["id"], "Мой день")

    await _rerun(12)
    snapshot = (
        dict(await db.get_program(program_id)),
        [dict(d) for d in await db.list_program_days_by_id(program_id)],
    )
    assert snapshot[1][0]["name"] == "Мой день"

    await _rerun(12)
    await db._run_one_shot_migrations()
    assert snapshot == (
        dict(await db.get_program(program_id)),
        [dict(d) for d in await db.list_program_days_by_id(program_id)],
    )


async def test_ai_comments_turned_off_once_and_reenable_survives_restart(fresh_db):
    await db.get_or_create_user(telegram_id=ATHLETE, username="athlete")
    await db.update_user(ATHLETE, ai_comments_enabled=1)

    await _rerun(13)
    assert (await db.get_user(ATHLETE))["ai_comments_enabled"] == 0

    await db.update_user(ATHLETE, ai_comments_enabled=1)
    await db._run_one_shot_migrations()  # рестарт: версия уже 14
    assert (await db.get_user(ATHLETE))["ai_comments_enabled"] == 1
