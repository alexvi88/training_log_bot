"""«Чем заменить» — альтернативы упражнению из каталога (exercise_alternatives):
данные сверены с каталогом, связь симметрична, ручка /v1 отдаёт свою копию
там, где она уже есть."""
import exercise_alternatives
import i18n
import seed_data

_CATALOG = {name for _group, name in seed_data.EXERCISE_TEMPLATES}


def test_every_family_name_is_a_catalog_template():
    for family in exercise_alternatives.FAMILIES:
        assert len(family) >= 2, family
        for name in family:
            assert name in _CATALOG, f"{name!r} нет в EXERCISE_TEMPLATES"


def test_alternatives_are_symmetric_and_exclude_self():
    for name in _CATALOG:
        alts = exercise_alternatives.alternatives_for(name)
        assert name not in alts
        assert len(alts) == len(set(alts))
        assert len(alts) <= exercise_alternatives.MAX_ALTERNATIVES
        for alt in alts:
            # Обрезка по MAX_ALTERNATIVES может съесть обратную связь у
            # большого семейства — симметрию проверяем на полном списке.
            full = {n for f in exercise_alternatives.FAMILIES if alt in f for n in f}
            assert name in full, (name, alt)


def test_staple_lifts_have_alternatives():
    for name in ("Жим штанги лёжа", "Присед со штангой", "Становая тяга",
                 "Тяга верхнего блока", "Разгибание на трицепс на блоке"):
        assert exercise_alternatives.alternatives_for(name), name


def test_own_exercise_has_no_alternatives():
    assert exercise_alternatives.alternatives_for("Моё странное упражнение") == []


async def _fork(db, user_id, canonical):
    template = (await db.find_global_templates_by_names([canonical]))[canonical]
    return await db.fork_exercise_from_template(user_id, template["id"])


async def test_for_exercise_points_to_own_copy_when_present(fresh_db, user_id):
    db = fresh_db
    bench_id = await _fork(db, user_id, "Жим штанги лёжа")
    db_bench_id = await _fork(db, user_id, "Жим гантелей лёжа")
    bench = await db.get_exercise(bench_id)
    with i18n.use_lang("ru"):
        alts = await exercise_alternatives.for_exercise(user_id, bench, "ru")
    by_name = {a["name"]: a for a in alts}
    assert by_name["Жим гантелей лёжа"]["exercise_id"] == db_bench_id
    assert by_name["Жим в тренажёре"]["exercise_id"] is None
    assert by_name["Жим в тренажёре"]["template_id"]


async def test_archived_copy_is_offered_as_template(fresh_db, user_id):
    db = fresh_db
    bench_id = await _fork(db, user_id, "Жим штанги лёжа")
    db_bench_id = await _fork(db, user_id, "Жим гантелей лёжа")
    await db.archive_exercise(db_bench_id)
    alts = await exercise_alternatives.for_exercise(user_id, await db.get_exercise(bench_id), "ru")
    assert all(a["exercise_id"] != db_bench_id for a in alts)


def test_every_catalog_template_has_alternatives():
    """Новое упражнение каталога без семейства остаётся без замен молча —
    экран «Альтернативные упражнения» пустой. Дописывай в FAMILIES."""
    missing = [n for _g, n in seed_data.EXERCISE_TEMPLATES if not exercise_alternatives.alternatives_for(n)]
    assert not missing, f"нет ни в одном семействе FAMILIES: {missing}"
