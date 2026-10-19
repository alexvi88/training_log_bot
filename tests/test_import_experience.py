"""Перенесённая импортом история — данные, а не опыт с нашим вводом.

Атлет, который принёс сорок тренировок из Hevy, у нас ещё ничего не
записывал: подсказки новичку ему нужны, а свежий переезд со старыми датами —
не пауза, о которой пишут пуши (владелец)."""

import datetime as dt

import pytest

import api_v1
import db as db_module
import engagement
import push_texts


async def _import(db, user_id, started, *, batch_created_at):
    batch = await db.create_import_batch(user_id, "hevy")
    await db.create_finished_workout(
        user_id, started_at=started, finished_at=started, source="import", import_batch_id=batch,
    )
    await db.finish_import_batch(batch, workouts=1, sets=3)
    await db.conn().execute("UPDATE import_batches SET created_at = ? WHERE id = ?", (batch_created_at, batch))
    await db.conn().commit()
    return batch


@pytest.mark.asyncio
async def test_count_workouts_in_app_only_skips_imported(fresh_db, user_id):
    db = fresh_db
    await _import(db, user_id, "2026-03-01T10:00:00", batch_created_at="2026-07-10T09:00:00")
    await db.create_finished_workout(user_id, started_at="2026-07-11T10:00:00", finished_at="2026-07-11T11:00:00")
    assert await db.count_workouts(user_id) == 2
    assert await db.count_workouts(user_id, in_app_only=True) == 1


@pytest.mark.asyncio
async def test_workout_json_marks_imported(fresh_db, user_id):
    db = fresh_db
    await _import(db, user_id, "2026-03-01T10:00:00", batch_created_at="2026-07-10T09:00:00")
    await db.create_finished_workout(user_id, started_at="2026-07-11T10:00:00", finished_at="2026-07-11T11:00:00")
    rows = await db.list_workouts(user_id, limit=10)
    flags = {row["started_at"][:10]: api_v1._workout_json(row)["imported"] for row in rows}
    assert flags == {"2026-03-01": True, "2026-07-11": False}


@pytest.mark.asyncio
async def test_no_pause_push_the_week_after_importing_old_history(fresh_db, user_id):
    db = fresh_db
    # Последняя импортнутая тренировка — ровно 7 дней назад: раньше это был
    # «неделю не тренировался» на следующий день после переезда.
    await _import(db, user_id, "2026-07-05T10:00:00", batch_created_at="2026-07-11T20:00:00")
    decision = await engagement.build_daily_push(user_id, dt.date(2026, 7, 12))
    assert decision is None or decision.category not in {push_texts.SKIP_7, push_texts.WIN_BACK}


@pytest.mark.asyncio
async def test_pause_counts_from_the_import_once_a_week_passes(fresh_db, user_id):
    db = fresh_db
    await _import(db, user_id, "2026-03-01T10:00:00", batch_created_at="2026-07-05T20:00:00")
    decision = await engagement.build_daily_push(user_id, dt.date(2026, 7, 12))
    assert decision is not None
    assert decision.category == push_texts.SKIP_7


@pytest.mark.asyncio
async def test_undone_import_is_not_activity(fresh_db, user_id):
    db = fresh_db
    await db.create_finished_workout(user_id, started_at="2026-07-05T10:00:00", finished_at="2026-07-05T11:00:00")
    batch = await db.create_import_batch(user_id, "hevy")
    await db.finish_import_batch(batch, workouts=1, sets=3)
    await db.conn().execute(
        "UPDATE import_batches SET created_at = ?, undone_at = ? WHERE id = ?",
        ("2026-07-11T09:00:00", "2026-07-11T09:05:00", batch),
    )
    await db.conn().commit()
    assert await db.last_import_at(user_id) is None
    decision = await engagement.build_daily_push(user_id, dt.date(2026, 7, 12))
    assert decision is not None and decision.category == push_texts.SKIP_7


def test_days_since_activity_takes_the_fresher_of_the_two():
    today = dt.date(2026, 7, 12)
    assert engagement.days_since_activity(40, "2026-07-11T09:00:00", today) == 1
    assert engagement.days_since_activity(2, "2026-07-01T09:00:00", today) == 2
    assert engagement.days_since_activity(None, "2026-07-05T09:00:00", today) == 7
    assert engagement.days_since_activity(9, None, today) == 9
    assert engagement.days_since_activity(9, "garbage", today) == 9


def test_is_imported_workout_reads_either_marker():
    class Row(dict):
        def keys(self):
            return super().keys()
    assert db_module.is_imported_workout(Row(import_batch_id="b", source="manual"))
    assert db_module.is_imported_workout(Row(import_batch_id=None, source="import"))
    assert not db_module.is_imported_workout(Row(import_batch_id=None, source="manual"))
    assert not db_module.is_imported_workout(Row(source="manual"))
