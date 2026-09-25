"""Встроенная группа «Пресс» и переезд в неё упражнений на пресс из «Другое».

Группа новая только в списке пресетов: в старых базах уже лежит глобальная
«Пресс», заархивированная когда-то `_migrate_muscle_groups`, а у атлетов —
свои копии шаблонов с группой «Другое», унаследованной при форке. Тесты ниже
воспроизводят такую базу руками (старая раскладка поверх свежей) и прогоняют
стартовую цепочку init_db ещё раз.
"""
import formatting
import i18n
import seed_data
import timeutil
from seed_data import ABS_GROUP_NAME, EXERCISE_TEMPLATES, MUSCLE_GROUP_PRESETS

ABS_TEMPLATES = [name for group, name in EXERCISE_TEMPLATES if group == ABS_GROUP_NAME]


# ---------- сам пресет ----------


def test_abs_preset_sits_between_legs_and_other():
    order = [name for name, _emoji, _sort in sorted(MUSCLE_GROUP_PRESETS, key=lambda p: p[2])]
    assert order.index("Пресс") == order.index("Ноги") + 1
    assert order[-1] == "Другое"
    sorts = [sort for _n, _e, sort in MUSCLE_GROUP_PRESETS]
    assert len(set(sorts)) == len(sorts)


def test_abs_group_names_in_both_languages():
    assert seed_data.localized_muscle_group_name("Пресс", "ru") == "Пресс"
    assert seed_data.localized_muscle_group_name("Пресс", "en") == "Abs"
    assert seed_data.canonical_muscle_group_name("Abs") == "Пресс"
    assert seed_data.canonical_muscle_group_name("пресс") == "Пресс"


def test_core_exercises_are_labelled_abs_and_forearms_traps_stay_other():
    by_name = {name: group for group, name in EXERCISE_TEMPLATES}
    for name in (
        "Скручивания", "Планка", "Боковая планка", "Подъём ног в висе",
        "Колесо для пресса", "Пресс в тренажёре", "Жим Палоффа", "Мёртвый жук",
    ):
        assert by_name[name] == "Пресс", name
    for name in (
        "Шраги со штангой", "Шраги с гантелями",
        "Сгибание запястий со штангой", "Разгибание запястий со штангой",
    ):
        assert by_name[name] == "Другое", name


# ---------- свежая база ----------


async def _global_groups(db, name):
    cur = await db.conn().execute(
        "SELECT * FROM muscle_groups WHERE user_id IS NULL AND name = ?", (name,)
    )
    return await cur.fetchall()


async def _template(db, name):
    cur = await db.conn().execute(
        "SELECT * FROM exercises WHERE is_template = 1 AND user_id IS NULL AND name = ?", (name,)
    )
    return await cur.fetchone()


async def test_fresh_db_has_one_active_abs_group_with_its_templates(fresh_db):
    db = fresh_db
    rows = await _global_groups(db, "Пресс")
    assert len(rows) == 1
    assert rows[0]["is_archived"] == 0
    assert rows[0]["emoji"] == "🍫"
    names = [g["name"] for g in await db.list_muscle_groups(None, global_only=True)]
    assert names == [n for n, _e, _s in sorted(MUSCLE_GROUP_PRESETS, key=lambda p: p[2])]
    assert (await _template(db, "Планка"))["primary_group_id"] == rows[0]["id"]


async def test_seed_is_idempotent(fresh_db):
    db = fresh_db
    await db._seed_globals()
    await db._seed_globals()
    await db._migrate_muscle_groups()
    await db._sync_exercise_templates()
    for name, _emoji, _sort in MUSCLE_GROUP_PRESETS:
        assert len(await _global_groups(db, name)) == 1, name


async def test_group_missing_entirely_is_added(fresh_db):
    """База, заведённая уже после слияния групп, «Пресс» не видела вовсе."""
    db = fresh_db
    abs_id = (await _global_groups(db, "Пресс"))[0]["id"]
    other_id = (await _global_groups(db, "Другое"))[0]["id"]
    await db.conn().execute(
        "UPDATE exercises SET primary_group_id = ? WHERE primary_group_id = ?", (other_id, abs_id)
    )
    await db.conn().execute("DELETE FROM muscle_groups WHERE id = ?", (abs_id,))
    await db.conn().commit()

    await db._seed_globals()
    await db._sync_exercise_templates()

    rows = await _global_groups(db, "Пресс")
    assert len(rows) == 1 and rows[0]["is_archived"] == 0
    assert (await _template(db, "Планка"))["primary_group_id"] == rows[0]["id"]


# ---------- живая база: старая раскладка ----------


async def _make_legacy(db):
    """Состояние прода до этой ветки: «Пресс» заархивирован, пресс в «Другое»,
    «Другое» седьмая, миграции прогнаны до пятой версии."""
    abs_id = (await _global_groups(db, "Пресс"))[0]["id"]
    other_id = (await _global_groups(db, "Другое"))[0]["id"]
    await db.conn().execute(
        "UPDATE exercises SET primary_group_id = ? WHERE primary_group_id = ?", (other_id, abs_id)
    )
    await db.conn().execute("UPDATE muscle_groups SET is_archived = 1 WHERE id = ?", (abs_id,))
    await db.conn().execute("UPDATE muscle_groups SET sort_order = 7 WHERE id = ?", (other_id,))
    await db.conn().execute("PRAGMA user_version = 5")
    await db.conn().commit()
    return abs_id, other_id


async def _restart(db):
    await db._seed_globals()
    await db._migrate_muscle_groups()
    await db._sync_exercise_templates()
    await db._run_one_shot_migrations()


async def _group_of(db, ex_id):
    return (await db.get_exercise(ex_id))["primary_group_id"]


async def test_legacy_archived_group_is_revived_not_duplicated(fresh_db):
    db = fresh_db
    abs_id, other_id = await _make_legacy(db)
    plank_template_id = (await _template(db, "Планка"))["id"]

    await _restart(db)

    rows = await _global_groups(db, "Пресс")
    assert [(r["id"], r["is_archived"]) for r in rows] == [(abs_id, 0)]
    assert (await _global_groups(db, "Другое"))[0]["sort_order"] == 8
    # Шаблон переехал на месте: его id живёт в уже отправленных кнопках.
    plank = await _template(db, "Планка")
    assert plank["id"] == plank_template_id
    assert plank["primary_group_id"] == abs_id
    # И следующий рестарт ничего не откатывает назад в «Другое».
    await _restart(db)
    assert (await _template(db, "Планка"))["primary_group_id"] == abs_id
    assert (await _global_groups(db, "Пресс"))[0]["is_archived"] == 0


async def test_migration_moves_untouched_default_abs_and_leaves_the_rest(fresh_db, user_id):
    db = fresh_db
    abs_id, other_id = await _make_legacy(db)
    legs_id = (await _global_groups(db, "Ноги"))[0]["id"]

    async def fork(name):
        return await db.fork_exercise_from_template(user_id, (await _template(db, name))["id"])

    plank = await fork("Планка")                 # нетронутый форк
    renamed = await fork("Скручивания")          # переименован — идентичность та же
    await db.update_exercise_name(renamed, "Мои скрутки")
    moved_by_user = await fork("Подъём ног в висе")  # атлет сам унёс в «Ноги»
    await db.update_exercise_group(moved_by_user, legs_id)
    shrugs = await fork("Шраги со штангой")      # не пресс
    own = await db.create_exercise(user_id, "Вакуум", other_id)  # своё, не из каталога
    assert await _group_of(db, plank) == other_id

    await _restart(db)

    assert await _group_of(db, plank) == abs_id
    assert await _group_of(db, renamed) == abs_id
    assert await _group_of(db, moved_by_user) == legs_id
    assert await _group_of(db, shrugs) == other_id
    assert await _group_of(db, own) == other_id

    # Разовая: вернул планку в «Другое» после миграции — там она и останется.
    await db.update_exercise_group(plank, other_id)
    await _restart(db)
    assert await _group_of(db, plank) == other_id


async def test_own_abs_group_is_merged_into_the_builtin(fresh_db, user_id):
    db = fresh_db
    abs_id, _other_id = await _make_legacy(db)
    other_user = (await db.get_or_create_user(telegram_id=222, username="en"))["telegram_id"]
    mine = await db.create_muscle_group(user_id, "Пресс")
    theirs = await db.create_muscle_group(other_user, "Abs")
    unrelated = await db.create_muscle_group(user_id, "Кардио")
    in_mine = await db.create_exercise(user_id, "Вакуум", mine)
    in_theirs = await db.create_exercise(other_user, "Hollow hold", theirs)
    in_unrelated = await db.create_exercise(user_id, "Бег", unrelated)

    await _restart(db)

    assert await _group_of(db, in_mine) == abs_id
    assert await _group_of(db, in_theirs) == abs_id
    assert await _group_of(db, in_unrelated) == unrelated
    assert (await db.get_muscle_group(mine))["is_archived"] == 1
    assert (await db.get_muscle_group(theirs))["is_archived"] == 1
    assert (await db.get_muscle_group(unrelated))["is_archived"] == 0
    names = [g["name"] for g in await db.list_muscle_groups(user_id)]
    assert names.count("Пресс") == 1


async def test_new_fork_lands_in_abs(fresh_db, user_id):
    db = fresh_db
    abs_id = (await _global_groups(db, "Пресс"))[0]["id"]
    for name in ABS_TEMPLATES:
        ex_id = await db.fork_exercise_from_template(user_id, (await _template(db, name))["id"])
        assert await _group_of(db, ex_id) == abs_id, name


# ---------- недельный объём ----------


async def test_weekly_volume_panel_shows_abs(fresh_db, user_id):
    """«Другое» панель прячет — пресс, пока жил там, был невидим. Теперь у него
    своя строка, и на английском она подписана по-английски."""
    db = fresh_db
    groups = await db.list_muscle_groups(user_id)
    abs_id = next(g["id"] for g in groups if g["name"] == "Пресс")
    other_id = next(g["id"] for g in groups if g["name"] == "Другое")

    _title, rows = formatting.weekly_volume_panel({abs_id: 9, other_id: 4}, groups)
    by_label = {label: (sets, status) for label, sets, status in rows}
    assert by_label["ПРЕСС"] == (9, "in_range")
    assert "ДРУГОЕ" not in by_label

    with i18n.use_lang("en"):
        _title, rows = formatting.weekly_volume_panel({abs_id: 9}, groups)
    assert ("ABS", 9, "in_range") in rows


async def test_weekly_volume_by_group_counts_abs_sets(fresh_db, user_id):
    db = fresh_db
    abs_id = (await _global_groups(db, "Пресс"))[0]["id"]
    plank = await db.fork_exercise_from_template(user_id, (await _template(db, "Планка"))["id"])
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, plank, 0)
    for i in range(3):
        await db.add_set(block_id, plank, i, 0, 0.0, 60)
    await db.finish_workout(workout_id)
    today = timeutil.user_today(await db.get_user(user_id))
    counts = await db.weekly_volume_by_group(user_id, "2000-01-01", today.isoformat())
    assert counts.get(abs_id) == 3
