"""Экспорт CSV и обратный импорт того же файла должны быть обратимы: выгрузил
свою историю, загрузил её же обратно — история не должна измениться и не
должна удвоиться.

`csv_export.build_csv` писал в колонку `started_at` сырое значение из БД, а
там оно хранится в UTC (`db.export_rows_for_user`). Календарная дата
тренировки, которую видит пользователь (дашборд, история, стрик), — местная,
`date(started_at, '<tz_offset> hours')` (`db._local_day`). Импорт же
(`handlers/csv_import.py:_parse_row_date`) читает календарную дату прямо из
колонки `started_at`, без поправки на часовой пояс.

На положительном сдвиге (например, UTC+12) тренировка, начатая под вечер по
местному времени, в UTC приходится уже на предыдущие сутки. Экспорт такой
тренировки и обратный импорт того же файла давал дату на день раньше:
`_duplicate_dates` не находила совпадения (сравнивает по дате) и заводила
вторую, лишнюю тренировку на несуществующий для человека день вместо того,
чтобы распознать файл как повтор.
"""

import datetime as dt

import csv_export
import db
import handlers.csv_import as csv_import


async def test_export_then_reimport_same_file_does_not_duplicate_or_shift_date(fresh_db, user_id):
    await fresh_db.update_user(user_id, tz_offset=12)
    gid = await fresh_db.create_muscle_group(user_id, "Ноги")
    ex_id = await fresh_db.create_exercise(user_id, "Присед", gid)

    # UTC 2026-03-14 14:00 = местное (UTC+12) 2026-03-15 02:00 — сутки в
    # UTC и по местному времени разные, ровно тот случай, что ломался.
    started_at = "2026-03-14T14:00:00"
    workout_id = await db.create_finished_workout(user_id, started_at, started_at, source="manual")
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 1, 0, 100.0, 5, None)

    local_dates_before = await db.list_finished_workout_dates(user_id, tz_offset=12)
    assert local_dates_before == ["2026-03-15"]

    csv_bytes = await csv_export.build_csv(user_id)
    text = csv_bytes.decode("utf-8-sig")

    headers, rows, has_header = csv_import._read_table(text)
    mapping = csv_import._auto_detect(headers)
    workouts = csv_import._build_workout_groups(
        rows, mapping, first_line=2 if has_header else 1,
        weight_factor=csv_import._weight_factor(headers, mapping),
    )
    # Дата из экспортированного файла должна совпасть с местной календарной
    # датой тренировки, а не с UTC-датой.
    assert [w["date"] for w in workouts] == ["2026-03-15"]

    resolved, unresolved = await csv_import.resolve_exercise_names_exact(
        user_id, [e["name"] for w in workouts for e in w["entries"]]
    )
    assert unresolved == []

    dup = await csv_import._duplicate_dates(user_id, workouts, resolved)
    assert dup == {"2026-03-15"}
    to_import = [w for w in workouts if w["date"] not in dup]

    imported, failed = await csv_import.apply_import(user_id, to_import, resolved)
    assert (imported, failed) == (0, 0)

    assert await db.count_workouts(user_id) == 1
    assert await db.list_finished_workout_dates(user_id, tz_offset=12) == ["2026-03-15"]


async def test_exported_started_at_is_local_wall_clock(fresh_db, user_id):
    """Экспортированная колонка `started_at` — местное время пользователя, а
    не сырой UTC: иначе не только импорт, но и любой человек, открывший файл
    в Excel, видит тренировку в другой день."""
    await fresh_db.update_user(user_id, tz_offset=-5)
    gid = await fresh_db.create_muscle_group(user_id, "Спина")
    ex_id = await fresh_db.create_exercise(user_id, "Тяга", gid)

    started_at = "2026-06-01T02:00:00"  # UTC 02:00 = местные (UTC-5) 2026-05-31 21:00
    workout_id = await db.create_finished_workout(user_id, started_at, started_at, source="manual")
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 1, 0, 60.0, 8, None)

    csv_bytes = await csv_export.build_csv(user_id)
    text = csv_bytes.decode("utf-8-sig")
    data_line = text.splitlines()[1]
    exported_started_at = data_line.split(",")[0]
    assert exported_started_at == dt.datetime(2026, 5, 31, 21, 0, 0).isoformat()
