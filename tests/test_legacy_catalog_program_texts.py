"""Каталожные копии программ под прежним текстом каталога (до f3c805c).

Копия — снимок имени и описания на момент добавления. После переименования
«Верх / Низ — 4 дня» → «Верх / Низ» старая копия переставала считаться
нетронутой: смена языка её не переводила, а «уже есть такая программа»
по новому имени её не находила и заводила дубль.
"""
import i18n
import seed_data


async def _legacy_copy(db, user_id, key, lang):
    """Копия, снятая до правки каталога: старые имя и описание в базе."""
    fields = seed_data.LEGACY_PROGRAM_TEXTS[(key, lang)]
    name = fields.get("name", (seed_data.localized_program_name(key, lang),))[0]
    with i18n.use_lang(lang):
        program_id = await seed_data.instantiate_program(user_id, key, name)
    if "description" in fields:
        await db.set_program_description(
            program_id, db.clean_program_description(fields["description"][0])
        )
    return program_id


async def _rerun_migration(db):
    await db.conn().execute("PRAGMA user_version = 6")
    await db.conn().commit()
    await db._run_one_shot_migrations()


async def test_migration_moves_legacy_copy_to_current_text(fresh_db, user_id):
    db = fresh_db
    program_id = await _legacy_copy(db, user_id, "upperlower", "ru")
    assert (await db.get_program(program_id))["name"] == "Верх / Низ — 4 дня"

    await _rerun_migration(db)

    program = await db.get_program(program_id)
    assert program["name"] == "Верх / Низ"
    assert program["description"] == db._catalog_program_description("upperlower", "ru")
    # Проверка «уже есть такая программа» теперь её находит — дубля не будет.
    found = await db.find_program_by_name(user_id, seed_data.localized_program_name("upperlower", "ru"))
    assert found is not None and found["id"] == program_id


async def test_migration_fixes_descriptions_of_other_programs(fresh_db, user_id):
    db = fresh_db
    ids = {
        key: await _legacy_copy(db, user_id, key, "ru")
        for key in ("fullbody3", "strength5x5", "ppl")
    }
    await _rerun_migration(db)
    for key, program_id in ids.items():
        assert (await db.get_program(program_id))["description"] == db._catalog_program_description(key, "ru")


async def test_migration_leaves_own_names_alone(fresh_db, user_id):
    db = fresh_db
    manual_id = await db.create_program(user_id, "Верх / Низ — 4 дня")
    await _rerun_migration(db)
    assert (await db.get_program(manual_id))["name"] == "Верх / Низ — 4 дня"


async def test_taken_new_name_is_skipped_but_relocalize_still_translates(fresh_db, user_id):
    db = fresh_db
    await db.create_program(user_id, "Верх / Низ")
    program_id = await _legacy_copy(db, user_id, "upperlower", "ru")

    await _rerun_migration(db)
    assert (await db.get_program(program_id))["name"] == "Верх / Низ — 4 дня"

    # Прежнее каталожное имя — всё ещё «нетронутое»: смена языка его переводит.
    await db.set_user_lang(user_id, "en")
    program = await db.get_program(program_id)
    assert program["name"] == "Upper / Lower"
    assert program["description"] == db._catalog_program_description("upperlower", "en")


async def test_relocalize_recognizes_legacy_english_description(fresh_db, user_id):
    db = fresh_db
    await db.set_user_lang(user_id, "en")
    program_id = await _legacy_copy(db, user_id, "strength5x5", "en")
    await db.set_user_lang(user_id, "ru")
    assert (await db.get_program(program_id))["description"] == db._catalog_program_description(
        "strength5x5", "ru"
    )
