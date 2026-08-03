"""Собственный вес как нагрузка.

До этого подтягивания лежали в базе как «0×12»: нулевой тоннаж, нулевой e1RM и
плоский график у человека, который за полгода дошёл с пяти повторов до
двенадцати. Вес тела бот всё это время знал — «⚖️ Дневник веса» пишет
`bodyweight_logs`, — и ни во что его не включал.

Считается не на лету, а снимком на подходе (`sets.load_weight`): вес тела
меняется, а уже сделанный подход — нет.
"""
import analytics
import db as db_module

# asyncio_mode=auto (pytest.ini); часть проверок ниже чисто арифметические.

PULLUPS = "Подтягивания"
PUSHUPS = "Отжимания от пола"
BENCH = "Жим штанги лёжа"


async def _own(db, user_id, template_name):
    template = next(
        t for t in await db.list_all_exercise_templates() if t["name"] == template_name
    )
    return await db.fork_exercise_from_template(user_id, template["id"])


async def _log(db, user_id, ex_id, weight, reps, finished=True):
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.append_set(block_id, ex_id, 0, weight, reps)
    if finished:
        await db.finish_workout(workout_id)
    return workout_id


# ---------- формула ----------


def test_effective_load_by_mode():
    assert db_module.effective_load(60, 80, "none") == 60
    assert db_module.effective_load(0, 80, "full") == 80
    assert db_module.effective_load(10, 80, "full") == 90          # подтягивания с поясом
    assert db_module.effective_load(0, 80, "full", 0.65) == 52.0    # отжимания от пола
    assert db_module.effective_load(30, 80, "assisted") == 50       # гравитрон
    assert db_module.effective_load(200, 80, "assisted") == 0       # не уходим в минус


def test_without_a_weigh_in_nothing_is_invented():
    """Ноль честнее выдуманной массы."""
    assert db_module.effective_load(0, None, "full") == 0
    assert db_module.effective_load(10, None, "full") == 10


# ---------- каталог ----------


async def test_catalog_marks_bodyweight_movements(fresh_db):
    db = fresh_db
    by_name = {t["name"]: t for t in await db.list_all_exercise_templates()}

    assert by_name[PULLUPS]["bodyweight_load"] == "full"
    assert by_name[PULLUPS]["bodyweight_factor"] == 1.0
    assert by_name[PUSHUPS]["bodyweight_load"] == "full"
    assert by_name[PUSHUPS]["bodyweight_factor"] == 0.65
    assert by_name[BENCH]["bodyweight_load"] == "none"
    # Планка — время, а не подъём массы; приписывать ей тоннаж было бы
    # раздуванием цифры.
    assert by_name["Планка"]["bodyweight_load"] == "none"


async def test_forking_carries_the_load_mode(fresh_db, user_id):
    db = fresh_db
    ex_id = await _own(db, user_id, PULLUPS)
    ex = await db.get_exercise(ex_id)
    assert ex["bodyweight_load"] == "full"


# ---------- снимок при записи ----------


async def test_a_pullup_set_records_what_was_actually_lifted(fresh_db, user_id):
    db = fresh_db
    await db.add_bodyweight_log(user_id, 80.0)
    ex_id = await _own(db, user_id, PULLUPS)

    await _log(db, user_id, ex_id, 0, 12)

    sets = await db.list_sets_for_exercise(ex_id)
    assert sets[0]["weight"] == 0        # показываем то, что записал человек
    assert db_module.load_of(sets[0]) == 80.0


async def test_a_barbell_set_is_untouched(fresh_db, user_id):
    db = fresh_db
    await db.add_bodyweight_log(user_id, 80.0)
    ex_id = await _own(db, user_id, BENCH)

    await _log(db, user_id, ex_id, 100, 5)

    sets = await db.list_sets_for_exercise(ex_id)
    assert sets[0]["load_weight"] is None
    assert db_module.load_of(sets[0]) == 100


async def test_the_snapshot_does_not_drift_when_the_athlete_changes(fresh_db, user_id):
    """Подход, сделанный при 80 кг, таким и остаётся."""
    db = fresh_db
    await db.add_bodyweight_log(user_id, 80.0)
    ex_id = await _own(db, user_id, PULLUPS)
    await _log(db, user_id, ex_id, 0, 10)

    await db.add_bodyweight_log(user_id, 90.0)

    assert db_module.load_of((await db.list_sets_for_exercise(ex_id))[0]) == 80.0


async def test_editing_a_set_recomputes_the_snapshot(fresh_db, user_id):
    db = fresh_db
    await db.add_bodyweight_log(user_id, 80.0)
    ex_id = await _own(db, user_id, PULLUPS)
    await _log(db, user_id, ex_id, 0, 10)
    set_row = (await db.list_sets_for_exercise(ex_id))[0]

    await db.update_set(set_row["id"], weight=10, reps=8)

    assert db_module.load_of((await db.list_sets_for_exercise(ex_id))[0]) == 90.0


# ---------- арифметика, которая раньше показывала нули ----------


async def test_pullups_now_have_tonnage_and_an_e1rm(fresh_db, user_id):
    db = fresh_db
    await db.add_bodyweight_log(user_id, 80.0)
    ex_id = await _own(db, user_id, PULLUPS)
    workout_id = await _log(db, user_id, ex_id, 0, 10)

    rows = await db.list_sets_for_exercise(ex_id)
    sets = [analytics.SetRow(db_module.load_of(r), r["reps"], workout_id, r["started_at"]) for r in rows]
    session = analytics.SessionStats(workout_id, rows[0]["started_at"], sets)

    assert session.tonnage == 800.0
    assert session.top_e1rm > 100  # 80 кг на 10 повторов
    assert await db.max_e1rm_before_workout(user_id, ex_id, workout_id + 1) > 100


async def test_adding_reps_at_the_same_bodyweight_reads_as_progress(fresh_db, user_id):
    """Ровно тот график, который раньше был плоской нулевой линией."""
    db = fresh_db
    await db.add_bodyweight_log(user_id, 80.0)
    ex_id = await _own(db, user_id, PULLUPS)
    await _log(db, user_id, ex_id, 0, 5)
    await _log(db, user_id, ex_id, 0, 12)

    rows = await db.list_sets_for_exercise(ex_id)
    e1rms = [analytics.e1rm(db_module.load_of(r), r["reps"]) for r in rows]

    assert e1rms[1] > e1rms[0]


async def test_the_sql_tonnage_path_counts_bodyweight_too(fresh_db, user_id):
    """Тоннаж считается и на Python (SessionStats), и в SQL — проверяем второй."""
    db = fresh_db
    await db.add_bodyweight_log(user_id, 80.0)
    ex_id = await _own(db, user_id, PULLUPS)
    await _log(db, user_id, ex_id, 0, 10)

    assert (await db.hall_of_fame_aggregates(user_id))["tonnage"] == 800.0


# ---------- миграция ----------


async def test_historical_sets_are_backfilled_from_the_nearest_weigh_in(fresh_db, user_id):
    db = fresh_db
    ex_id = await _own(db, user_id, PULLUPS)
    await db.add_bodyweight_log(user_id, 75.0, logged_at="2026-01-10T10:00:00")
    await db.add_bodyweight_log(user_id, 85.0, logged_at="2026-06-10T10:00:00")
    await _log(db, user_id, ex_id, 0, 10)
    # Симулируем строку, записанную до появления колонки, в марте.
    await db.conn().execute(
        "UPDATE sets SET load_weight = NULL, created_at = '2026-03-01T10:00:00' "
        "WHERE exercise_id = ?",
        (ex_id,),
    )
    await db.conn().commit()

    await db._backfill_bodyweight_load()

    # Мартовский подход считается по январскому взвешиванию, а не по июньскому.
    assert db_module.load_of((await db.list_sets_for_exercise(ex_id))[0]) == 75.0


async def test_a_user_who_never_weighed_in_keeps_the_old_numbers(fresh_db, user_id):
    db = fresh_db
    ex_id = await _own(db, user_id, PULLUPS)
    await _log(db, user_id, ex_id, 0, 10)
    await db.conn().execute("UPDATE sets SET load_weight = NULL WHERE exercise_id = ?", (ex_id,))
    await db.conn().commit()

    await db._backfill_bodyweight_load()

    row = (await db.list_sets_for_exercise(ex_id))[0]
    assert row["load_weight"] is None
    assert db_module.load_of(row) == 0


async def test_the_backfill_leaves_barbell_sets_alone(fresh_db, user_id):
    db = fresh_db
    await db.add_bodyweight_log(user_id, 80.0)
    ex_id = await _own(db, user_id, BENCH)
    await _log(db, user_id, ex_id, 100, 5)

    await db._backfill_bodyweight_load()

    assert (await db.list_sets_for_exercise(ex_id))[0]["load_weight"] is None
