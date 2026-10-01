"""Каталожные копии программ под прежним текстом каталога (до f3c805c).

Копия — снимок имени и описания на момент добавления. После переименования
«Верх / Низ — 4 дня» → «Верх / Низ» старая копия переставала считаться
нетронутой: смена языка её не переводила, а «уже есть такая программа»
по новому имени её не находила и заводила дубль. Второй заход — эмодзи перед
«Всё тело — 2 дня» и «Верх / Низ» (единственные в каталоге без него): копии,
добавленные до этого, должны доехать до нового имени тем же путём.
"""
import re

import httpx

import api_v1
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
    assert program["name"] == seed_data.localized_program_name("upperlower", "ru")
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
    await db.create_program(user_id, seed_data.localized_program_name("upperlower", "ru"))
    program_id = await _legacy_copy(db, user_id, "upperlower", "ru")

    await _rerun_migration(db)
    assert (await db.get_program(program_id))["name"] == "Верх / Низ — 4 дня"

    # Прежнее каталожное имя — всё ещё «нетронутое»: смена языка его переводит.
    await db.set_user_lang(user_id, "en")
    program = await db.get_program(program_id)
    assert program["name"] == seed_data.localized_program_name("upperlower", "en")
    assert program["description"] == db._catalog_program_description("upperlower", "en")


async def test_relocalize_recognizes_legacy_english_description(fresh_db, user_id):
    db = fresh_db
    await db.set_user_lang(user_id, "en")
    program_id = await _legacy_copy(db, user_id, "strength5x5", "en")
    await db.set_user_lang(user_id, "ru")
    assert (await db.get_program(program_id))["description"] == db._catalog_program_description(
        "strength5x5", "ru"
    )


# ---------- эмодзи перед «Всё тело — 2 дня» и «Верх / Низ» ----------

_PRE_EMOJI_NAMES = {
    ("fullbody2", "ru"): "Всё тело — 2 дня",
    ("fullbody2", "en"): "Full Body — 2 Days",
    ("upperlower", "ru"): "Верх / Низ",
    ("upperlower", "en"): "Upper / Lower",
}


def _lead(name: str) -> str:
    return name.split(" ", 1)[0]


def test_every_catalog_program_leads_with_its_own_emoji():
    """Заголовки в ленте каталога выстраиваются в колонку только если эмодзи
    есть у всех; один и тот же у двух программ читался бы как одна."""
    for lang in ("ru", "en"):
        leads = [
            _lead(seed_data.localized_program_name(p["key"], lang)) for p in seed_data.WORKOUT_PROGRAMS
        ]
        assert all(not re.match(r"\w", lead) for lead in leads), leads
        assert len(set(leads)) == len(leads), leads
    for p in seed_data.WORKOUT_PROGRAMS:
        assert _lead(seed_data.localized_program_name(p["key"], "ru")) == _lead(
            seed_data.localized_program_name(p["key"], "en")
        )


def test_pre_emoji_names_are_listed_as_legacy():
    for (key, lang), old in _PRE_EMOJI_NAMES.items():
        assert old in seed_data.LEGACY_PROGRAM_TEXTS[(key, lang)]["name"]
        assert seed_data.localized_program_name(key, lang).endswith(" " + old)


async def _pre_emoji_copy(db, user_id, key, lang):
    with i18n.use_lang(lang):
        return await seed_data.instantiate_program(user_id, key, _PRE_EMOJI_NAMES[(key, lang)])


async def test_migration_adds_emoji_to_pre_emoji_copies(fresh_db, user_id):
    db = fresh_db
    ids = {k: await _pre_emoji_copy(db, user_id, *k) for k in _PRE_EMOJI_NAMES}
    # База, уже прошедшая первый заход (v7), тоже должна получить второй.
    await db.conn().execute("PRAGMA user_version = 7")
    await db.conn().commit()
    await db._run_one_shot_migrations()
    for (key, lang), program_id in ids.items():
        assert (await db.get_program(program_id))["name"] == seed_data.localized_program_name(key, lang)
    # Повторный прогон ничего не ломает.
    await _rerun_migration(db)
    for (key, lang), program_id in ids.items():
        assert (await db.get_program(program_id))["name"] == seed_data.localized_program_name(key, lang)


async def test_migration_leaves_own_program_with_old_name_alone(fresh_db, user_id):
    db = fresh_db
    manual_id = await db.create_program(user_id, "Верх / Низ")
    await _rerun_migration(db)
    assert (await db.get_program(manual_id))["name"] == "Верх / Низ"


async def test_relocalize_translates_pre_emoji_copy(fresh_db, user_id):
    """Копия, миграцией не тронутая (новое имя было занято), всё равно
    считается нетронутой — смена языка её переводит."""
    db = fresh_db
    program_id = await _pre_emoji_copy(db, user_id, "fullbody2", "ru")
    await db.set_user_lang(user_id, "en")
    assert (await db.get_program(program_id))["name"] == seed_data.localized_program_name("fullbody2", "en")


async def test_readding_after_migration_is_name_taken_not_duplicate(fresh_db, user_id):
    """Сквозь /v1: копия до эмодзи → миграция → «➕ Добавить себе» отвечает
    409, а не заводит вторую программу под новым именем."""
    db = fresh_db
    await _pre_emoji_copy(db, user_id, "upperlower", "ru")
    await _rerun_migration(db)

    code = await db.issue_oauth_link_code(user_id, ttl_seconds=600, digits=8)
    transport = httpx.ASGITransport(app=api_v1.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/auth/link", json={"code": code})
        assert resp.status_code == 200, resp.text
        client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
        again = await client.post("/programs/catalog/upperlower")
        assert again.status_code == 409, again.text
        assert again.json()["error"] == "name_taken"
    cur = await db.conn().execute(
        "SELECT COUNT(*) FROM programs WHERE user_id = ? AND source_ref = 'upperlower'", (user_id,)
    )
    assert (await cur.fetchone())[0] == 1


# ---------- «Толкай / Тяни / Ноги» → «Push/Pull/Legs (жим, тяга, ноги)» (B-12) ----------

_OLD_PPL = "🔁 Толкай / Тяни / Ноги"
_OLD_PPL_DAYS = ("Толкай", "Тяни", "Ноги")


async def _old_ppl_copy(db, user_id):
    """Копия PPL, снятая до B-12: старое имя программы и старые имена дней."""
    with i18n.use_lang("ru"):
        program_id = await seed_data.instantiate_program(user_id, "ppl", _OLD_PPL)
    for day, old in zip(await db.list_program_days_by_id(program_id), _OLD_PPL_DAYS, strict=True):
        await db.rename_routine(day["id"], old)
    return program_id


async def _rerun_from_v9(db):
    await db.conn().execute("PRAGMA user_version = 9")
    await db.conn().commit()
    await db._run_one_shot_migrations()


def test_ppl_catalog_text_is_not_a_calque():
    assert seed_data.localized_program_name("ppl", "ru") == "🔁 Push/Pull/Legs (жим, тяга, ноги)"
    assert [seed_data.localized_program_day_name("ppl", i, "ru") for i in range(3)] == [
        "Жим", "Тяга", "Ноги",
    ]
    assert [seed_data.localized_program_day_name("ppl", i, "en") for i in range(3)] == [
        "Push", "Pull", "Legs",
    ]
    # Русский текст каталога и ru.json не разъехались.
    assert i18n.t_in("ru", "program.ppl.name") == seed_data.localized_program_name("ppl", "ru")
    for i in range(3):
        assert i18n.t_in("ru", f"program.ppl.day.{i}.name") == seed_data.PROGRAM_BY_KEY["ppl"]["days"][i][0]


async def test_migration_renames_old_ppl_copy_and_keeps_history(fresh_db, user_id):
    db = fresh_db
    program_id = await _old_ppl_copy(db, user_id)
    days_before = await db.list_program_days_by_id(program_id)
    push_id = days_before[0]["id"]
    workout_id = await db.create_workout(user_id, routine_id=push_id)
    await db.finish_workout(workout_id)

    await _rerun_from_v9(db)

    assert (await db.get_program(program_id))["name"] == "🔁 Push/Pull/Legs (жим, тяга, ноги)"
    days_after = await db.list_program_days_by_id(program_id)
    assert [d["id"] for d in days_after] == [d["id"] for d in days_before]
    assert [d["name"] for d in days_after] == ["Жим", "Тяга", "Ноги"]
    # История держится за тот же день: следующим идёт «Тяга», а не «Жим».
    cur = await db.conn().execute("SELECT routine_id FROM workouts WHERE id = ?", (workout_id,))
    assert (await cur.fetchone())["routine_id"] == push_id
    assert (await db.next_program_day(program_id))["name"] == "Тяга"
    # «Уже есть у тебя» находит её по новому имени.
    found = await db.find_program_by_name(user_id, seed_data.localized_program_name("ppl", "ru"))
    assert found is not None and found["id"] == program_id

    # Повторный прогон ничего не меняет.
    await _rerun_from_v9(db)
    assert [d["name"] for d in await db.list_program_days_by_id(program_id)] == ["Жим", "Тяга", "Ноги"]


async def test_migration_leaves_own_ppl_names_alone(fresh_db, user_id):
    db = fresh_db
    program_id = await _old_ppl_copy(db, user_id)
    await db.rename_program_by_id(program_id, "Мой сплит")
    days = await db.list_program_days_by_id(program_id)
    await db.rename_routine(days[1]["id"], "Спина и бицепс")
    # Своя программа с такими же именами — не каталожная копия.
    own_id = await db.create_program(user_id, _OLD_PPL)
    own_day = await db.create_routine(user_id, "Толкай", program_id=own_id)

    await _rerun_from_v9(db)

    assert (await db.get_program(program_id))["name"] == "Мой сплит"
    # Нетронутые дни переименованной программы всё равно доезжают до нового имени.
    assert [d["name"] for d in await db.list_program_days_by_id(program_id)] == [
        "Жим", "Спина и бицепс", "Ноги",
    ]
    assert (await db.get_program(own_id))["name"] == _OLD_PPL
    assert (await db.get_routine(own_day))["name"] == "Толкай"


async def test_relocalize_translates_old_ppl_day_names(fresh_db, user_id):
    """Копия, которую миграция не застала (или новое имя было занято), всё
    равно считается нетронутой: смена языка переводит и имя, и дни."""
    db = fresh_db
    program_id = await _old_ppl_copy(db, user_id)
    await db.set_user_lang(user_id, "en")
    assert (await db.get_program(program_id))["name"] == seed_data.localized_program_name("ppl", "en")
    assert [d["name"] for d in await db.list_program_days_by_id(program_id)] == ["Push", "Pull", "Legs"]


async def test_readding_ppl_after_migration_is_name_taken(fresh_db, user_id):
    db = fresh_db
    await _old_ppl_copy(db, user_id)
    await _rerun_from_v9(db)

    code = await db.issue_oauth_link_code(user_id, ttl_seconds=600, digits=8)
    transport = httpx.ASGITransport(app=api_v1.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/auth/link", json={"code": code})
        assert resp.status_code == 200, resp.text
        client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
        again = await client.post("/programs/catalog/ppl")
        assert again.status_code == 409, again.text
    cur = await db.conn().execute(
        "SELECT COUNT(*) FROM programs WHERE user_id = ? AND source_ref = 'ppl'", (user_id,)
    )
    assert (await cur.fetchone())[0] == 1
