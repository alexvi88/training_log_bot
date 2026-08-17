"""Каталоги упражнений, групп мышц и программ — самый опасный кусок
локализации: в отличие от экранов, он оставляет русский след в БАЗЕ
пользователя необратимо (см. CLAUDE.md). Проверяем и данные (английские
названия существуют, без кириллицы), и инвариант идентичности (форк у
англоязычного пользователя всё равно хранит русский original_name — иначе
рвётся связь с exercise_media/exercise_descriptions).
"""
import exercise_descriptions
import exercise_media
import i18n
import i18n_coverage
import seed_data

# ---------- каталог: у всех есть английские названия, без кириллицы --------


def test_every_exercise_has_an_english_name():
    for _group, name in seed_data.EXERCISE_TEMPLATES:
        en_name = seed_data.localized_exercise_name(name, "en")
        assert en_name, name
        assert en_name != name, f"{name!r}: английское имя совпадает с русским"
        assert not i18n_coverage.has_cyrillic(en_name), (name, en_name)


def test_every_muscle_group_has_an_english_name():
    for name, _emoji, _order in seed_data.MUSCLE_GROUP_PRESETS:
        en_name = seed_data.localized_muscle_group_name(name, "en")
        assert en_name, name
        assert en_name != name, f"{name!r}: английское имя совпадает с русским"
        assert not i18n_coverage.has_cyrillic(en_name), (name, en_name)


def test_every_program_has_english_text():
    for program in seed_data.WORKOUT_PROGRAMS:
        key = program["key"]
        en_name = seed_data.localized_program_name(key, "en")
        en_meta = seed_data.localized_program_meta(key, "en")
        en_description = seed_data.localized_program_description(key, "en")
        assert en_name and en_meta and en_description, key
        assert not i18n_coverage.has_cyrillic(en_name), (key, en_name)
        assert not i18n_coverage.has_cyrillic(en_meta), (key, en_meta)
        assert not i18n_coverage.has_cyrillic(en_description), (key, en_description)
        for i, (_day_name, _exercises) in enumerate(program["days"]):
            en_day = seed_data.localized_program_day_name(key, i, "en")
            assert en_day, (key, i)
            assert not i18n_coverage.has_cyrillic(en_day), (key, i, en_day)


def test_russian_names_are_returned_unchanged():
    """Русский — канонический источник, а не перевод: не должен уходить в
    каталог и рисковать разойтись сам с собой."""
    for _group, name in seed_data.EXERCISE_TEMPLATES:
        assert seed_data.localized_exercise_name(name, "ru") == name
    for name, _emoji, _order in seed_data.MUSCLE_GROUP_PRESETS:
        assert seed_data.localized_muscle_group_name(name, "ru") == name
    program = seed_data.WORKOUT_PROGRAMS[0]
    assert seed_data.localized_program_name(program["key"], "ru") == program["name"]


# ---------- инвариант идентичности: original_name форка -------------------


async def _fork_a_well_known_template(db, user_id):
    """Жим штанги лёжа — гарантированно есть слаг (фото) и описание техники."""
    template = next(
        t for t in await db.list_all_exercise_templates()
        if t["name"] == "Жим штанги лёжа"
    )
    ex_id = await db.fork_exercise_from_template(user_id, template["id"])
    return await db.get_exercise(ex_id)


async def test_english_user_fork_keeps_russian_original_name(fresh_db, user_id):
    db = fresh_db
    await db.set_user_lang(user_id, "en")

    ex = await _fork_a_well_known_template(db, user_id)

    assert ex["original_name"] == "Жим штанги лёжа"
    assert ex["name"] == "Barbell Bench Press"
    assert ex["display_name"] == "Barbell Bench Press"


async def test_english_users_fork_still_finds_photo_and_description(fresh_db, user_id):
    """Сквозная проверка инварианта: exercise_media/exercise_descriptions
    ключуются по original_name (exercise_media.catalog_key), а не по
    показанному имени — форк англоязычного не должен терять ни то, ни другое.
    """
    db = fresh_db
    await db.set_user_lang(user_id, "en")

    ex = await _fork_a_well_known_template(db, user_id)

    assert len(exercise_media.get_images_for(ex)) == 2
    assert exercise_descriptions.effective_description(ex)


async def test_russian_user_fork_is_unchanged(fresh_db, user_id):
    """Уже заведённым (и всем ru-пользователям) ничего не сломали."""
    db = fresh_db

    ex = await _fork_a_well_known_template(db, user_id)

    assert ex["original_name"] == "Жим штанги лёжа"
    assert ex["name"] == "Жим штанги лёжа"
    assert ex["display_name"] == "Жим штанги лёжа"
    assert len(exercise_media.get_images_for(ex)) == 2
    assert exercise_descriptions.effective_description(ex)


async def test_fork_for_a_user_with_no_row_defaults_to_russian(fresh_db):
    """fork_exercise_from_template(user_id) для пользователя, которого ещё нет
    в users (например, служебный вызов), не должен падать — тихо считает язык
    русским, как и явное значение по умолчанию в users.lang."""
    db = fresh_db
    template = next(
        t for t in await db.list_all_exercise_templates()
        if t["name"] == "Жим штанги лёжа"
    )

    ex_id = await db.fork_exercise_from_template(999999, template["id"])
    ex = await db.get_exercise(ex_id)

    assert ex["original_name"] == "Жим штанги лёжа"
    assert ex["name"] == "Жим штанги лёжа"


# ---------- локальный каталог не расходится с i18n-хранилищем -------------


def test_locale_lookup_matches_the_seed_data_helper():
    """seed_data.localized_exercise_name — не собственная копия текста, а
    обёртка над каталогом i18n; проверяем, что она реально читает en.json, а
    не какой-то захардкоженный словарь внутри модуля."""
    i18n.reload()
    slug = exercise_media.EXERCISE_IMAGE_SLUGS["Жим штанги лёжа"]
    assert i18n.t_in("en", f"exercise.{slug}.name") == "Barbell Bench Press"
    assert seed_data.localized_exercise_name("Жим штанги лёжа", "en") == "Barbell Bench Press"
