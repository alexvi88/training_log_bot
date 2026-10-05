"""Находки аудита про тихие поломки: чистка ретеншна, бэкап, рассылка пушей,
реплика Litestream. Каждый тест падает без своего фикса."""

import asyncio
import datetime as dt
import os
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

import admin_tasks
import config
import db
import engagement
import ops_alerts
import push_texts

# ---------- 8. чистка ретеншна ----------


async def test_one_failing_prune_does_not_stop_the_others(fresh_db, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "training_log.db"))
    called = []

    def recorder(name, fail=False):
        async def _fn(*args, **kwargs):
            called.append(name)
            if fail:
                raise RuntimeError(f"{name} broke")
            return 0
        return _fn

    names = [
        "prune_old_cost_events", "prune_old_user_events", "prune_old_funnel_events",
        "prune_old_analytics_events", "prune_old_diagnostics", "prune_old_behaviour_digests",
        "prune_old_weekly_digests", "prune_old_import_batches", "prune_old_exercise_merges",
        "prune_old_limit_acks", "prune_old_ai_usage", "prune_old_ai_conversations", "prune_old_pushes",
        "delete_shared_items_older_than",
    ]
    for name in names:
        monkeypatch.setattr(db, name, recorder(name, fail=name == "prune_old_cost_events"))

    ok = await admin_tasks._run_retention_cleanup()

    assert ok is False
    assert called == names  # упавшая первой не оборвала остальные
    assert "prune_old_cost_events" not in "".join(r.message for r in caplog.records if r.levelname != "ERROR")
    assert any("cost_events" in r.message for r in caplog.records if r.levelname == "ERROR")


async def test_retention_catches_up_on_start_by_last_success_marker(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "training_log.db"))
    run = AsyncMock(return_value=True)
    monkeypatch.setattr(admin_tasks, "_run_retention_cleanup", run)

    await admin_tasks._catch_up_missed_retention()  # меток нет — чистим сразу
    assert run.await_count == 1
    assert admin_tasks._retention_age_hours() < 1

    await admin_tasks._catch_up_missed_retention()  # свежая метка — не трогаем
    assert run.await_count == 1

    marker = admin_tasks._retention_marker_path()
    old = dt.datetime.now().timestamp() - 30 * 3600
    os.utime(marker, (old, old))
    await admin_tasks._catch_up_missed_retention()  # прошло больше суток
    assert run.await_count == 2


async def test_failed_retention_leaves_no_success_marker(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "training_log.db"))
    monkeypatch.setattr(admin_tasks, "_run_retention_cleanup", AsyncMock(return_value=False))

    await admin_tasks._catch_up_missed_retention()

    assert admin_tasks._retention_age_hours() is None  # догон повторится на следующем старте


# ---------- 9. бэкап ----------


async def test_backup_is_verified_and_swapped_in_atomically(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "training_log.db"))

    path = await admin_tasks._rotate_disk_backup()

    check = sqlite3.connect(path)
    assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    check.close()
    leftovers = [f for f in os.listdir(os.path.dirname(path)) if f.startswith(admin_tasks._BACKUP_TMP_PREFIX)]
    assert leftovers == []


async def test_truncated_backup_does_not_look_fresh_and_keeps_the_old_copy(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "training_log.db"))
    backup_dir = admin_tasks._backup_dir()
    os.makedirs(backup_dir)
    today = os.path.join(backup_dir, f"training_log_backup_{dt.date.today().isoformat()}.db")
    with open(today, "wb") as f:
        f.write(b"previous good copy")
    old = dt.datetime.now().timestamp() - 5 * 3600
    os.utime(today, (old, old))

    async def truncated(dest):
        with open(dest, "wb") as f:
            f.write(b"SQLite format 3\0" + b"\0" * 10)  # оборвано на полпути

    monkeypatch.setattr(db, "backup_to_file", truncated)

    with pytest.raises(RuntimeError):
        await admin_tasks._rotate_disk_backup()

    with open(today, "rb") as f:
        assert f.read() == b"previous good copy"  # прежняя копия не затёрта
    assert admin_tasks._latest_backup_age_hours() > 4.9  # огрызок не освежил возраст
    assert not [f for f in os.listdir(backup_dir) if f.startswith(admin_tasks._BACKUP_TMP_PREFIX)]


async def test_empty_backup_file_is_rejected(tmp_path):
    empty = tmp_path / "x.db"
    empty.write_bytes(b"")
    with pytest.raises(RuntimeError):
        db.verify_backup_file(str(empty))


# ---------- 10. доставка пушей ----------


async def test_unexpected_delivery_error_does_not_abort_the_tick(fresh_db, user_id, monkeypatch, caplog):
    other = (await db.get_or_create_user(telegram_id=333, username="third"))["telegram_id"]
    for uid in (user_id, other):
        await db.create_finished_workout(
            uid, started_at="2026-07-01T10:00:00", finished_at="2026-07-01T11:00:00"
        )
    delivered = []

    async def deliver(bot, telegram_id, decision, local_date):
        if not delivered:
            delivered.append(telegram_id)
            raise RuntimeError("не TelegramAPIError: база, клавиатура, что угодно")
        delivered.append(telegram_id)

    async def build(telegram_id, today):
        return engagement.PushDecision(push_texts.SKIP_3, "текст")

    async def build_newbie(telegram_id, created_at, today):
        return engagement.PushDecision(push_texts.NEWBIE_NUDGE, "текст")

    monkeypatch.setattr(engagement, "_deliver", deliver)
    monkeypatch.setattr(engagement, "build_daily_push", build)
    monkeypatch.setattr(engagement, "build_newbie_push", build_newbie)
    monkeypatch.setattr(engagement, "should_send_now", lambda tz, hour: True)
    monkeypatch.setattr(engagement, "SEND_DELAY", 0)

    await engagement._send_daily_pushes(MagicMock())

    assert sorted(set(delivered)) == sorted([user_id, other])
    assert any("Failed to deliver" in r.message for r in caplog.records)


# ---------- 15. реплика Litestream ----------

WAL_TABLE = (
    "replica  generation        index  offset  size   created\n"
    "s3       a1b2c3d4e5f6a7b8  12     0       4096   {created}\n"
)


def _fresh_replica_state(monkeypatch, tmp_path=None, db_age_hours=0.0):
    import time as _time

    if tmp_path is not None:
        path = tmp_path / "training_log.db"
        path.write_bytes(b"x")
        stamp = _time.time() - db_age_hours * 3600
        os.utime(path, (stamp, stamp))
        monkeypatch.setattr(config, "DB_PATH", str(path))
    monkeypatch.setattr(admin_tasks, "_replica_alerted_at", None)
    monkeypatch.setattr(config, "BUCKET_NAME", "bucket")
    monkeypatch.setattr(admin_tasks, "_litestream_binary", lambda: "/usr/bin/litestream")


def _iso(hours_ago):
    stamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_replica_check_is_silent_without_binary_or_bucket(monkeypatch):
    monkeypatch.setattr(admin_tasks, "_replica_alerted_at", None)
    monkeypatch.setattr(admin_tasks, "_litestream_binary", lambda: None)
    monkeypatch.setattr(config, "BUCKET_NAME", "bucket")
    run = AsyncMock()
    monkeypatch.setattr(admin_tasks, "_run_litestream", run)
    assert await admin_tasks.check_replica_health() is None

    monkeypatch.setattr(admin_tasks, "_litestream_binary", lambda: "/usr/bin/litestream")
    monkeypatch.setattr(config, "BUCKET_NAME", "")
    assert await admin_tasks.check_replica_health() is None
    run.assert_not_awaited()


async def test_fresh_replica_is_fine(monkeypatch):
    _fresh_replica_state(monkeypatch)
    monkeypatch.setattr(
        admin_tasks, "_run_litestream",
        AsyncMock(return_value=WAL_TABLE.format(created=_iso(0.01))),
    )
    assert await admin_tasks.check_replica_health() is None


async def test_stale_replica_alerts_and_is_throttled(monkeypatch, tmp_path):
    _fresh_replica_state(monkeypatch, tmp_path)
    monkeypatch.setattr(
        admin_tasks, "_run_litestream",
        AsyncMock(return_value=WAL_TABLE.format(created=_iso(5))),
    )
    first = await admin_tasks.check_replica_health()
    assert first is not None and "Litestream" in first and "5." in first
    assert await admin_tasks.check_replica_health() is None  # не чаще раза в N часов

    monkeypatch.setattr(admin_tasks, "_replica_alerted_at", 1.0)  # давно
    assert await admin_tasks.check_replica_health() is not None


async def test_replica_recovery_resets_the_throttle(monkeypatch, tmp_path):
    _fresh_replica_state(monkeypatch, tmp_path)
    run = AsyncMock(return_value=WAL_TABLE.format(created=_iso(5)))
    monkeypatch.setattr(admin_tasks, "_run_litestream", run)
    assert await admin_tasks.check_replica_health() is not None
    run.return_value = WAL_TABLE.format(created=_iso(0.01))
    assert await admin_tasks.check_replica_health() is None
    run.return_value = WAL_TABLE.format(created=_iso(5))
    assert await admin_tasks.check_replica_health() is not None  # снова поломка — снова тревога


async def test_idle_database_at_night_does_not_alert(monkeypatch, tmp_path):
    # Реплика 5 часов назад, а база не писалась с тех пор (ночь) — это норма.
    _fresh_replica_state(monkeypatch, tmp_path, db_age_hours=6)
    monkeypatch.setattr(
        admin_tasks, "_run_litestream", AsyncMock(return_value=WAL_TABLE.format(created=_iso(5)))
    )
    assert await admin_tasks.check_replica_health() is None


async def test_write_after_last_replica_stamp_alerts(monkeypatch, tmp_path):
    _fresh_replica_state(monkeypatch, tmp_path, db_age_hours=1)  # писали час назад, реплика — 5 ч
    monkeypatch.setattr(
        admin_tasks, "_run_litestream", AsyncMock(return_value=WAL_TABLE.format(created=_iso(5)))
    )
    assert await admin_tasks.check_replica_health() is not None


async def test_wal_file_mtime_counts_as_a_write(monkeypatch, tmp_path):
    import time as _time

    _fresh_replica_state(monkeypatch, tmp_path, db_age_hours=6)
    (tmp_path / "training_log.db-wal").write_bytes(b"w")  # свежий -wal
    assert admin_tasks._db_written_since_replica(5) is True
    stamp = _time.time() - 6 * 3600
    os.utime(tmp_path / "training_log.db-wal", (stamp, stamp))
    assert admin_tasks._db_written_since_replica(5) is False


async def test_failing_litestream_command_alerts(monkeypatch):
    _fresh_replica_state(monkeypatch)
    monkeypatch.setattr(
        admin_tasks, "_run_litestream", AsyncMock(side_effect=RuntimeError("exit code 1: no replica"))
    )
    alert = await admin_tasks.check_replica_health()
    assert alert is not None and "no replica" in alert


async def test_hanging_litestream_is_killed_by_timeout(monkeypatch, tmp_path):
    script = tmp_path / "litestream"
    script.write_text("#!/bin/sh\nexec sleep 30\n")
    script.chmod(0o755)
    monkeypatch.setattr(config, "REPLICA_CHECK_TIMEOUT_SECONDS", 0.3)
    with pytest.raises(RuntimeError, match="нет ответа"):
        await asyncio.wait_for(admin_tasks._run_litestream(str(script), "wal"), timeout=12)


async def test_run_litestream_reads_stdout_and_reports_exit_code(monkeypatch, tmp_path):
    ok = tmp_path / "ok"
    ok.write_text("#!/bin/sh\necho 's3 gen 1 0 10 2026-01-01T00:00:00Z'\n")
    ok.chmod(0o755)
    bad = tmp_path / "bad"
    bad.write_text("#!/bin/sh\necho 'boom' >&2\nexit 3\n")
    bad.chmod(0o755)
    assert "2026-01-01" in await admin_tasks._run_litestream(str(ok), "wal")
    with pytest.raises(RuntimeError, match="кодом 3"):
        await admin_tasks._run_litestream(str(bad), "wal")


def test_newest_timestamp_picks_the_latest_of_all_rows():
    out = WAL_TABLE.format(created="2026-01-01T00:00:00Z") + "s3 gen 13 0 5 2026-02-01T10:00:00.123456789+03:00\n"
    stamp = admin_tasks._newest_timestamp(out)
    assert stamp is not None and stamp.year == 2026 and stamp.month == 2


async def test_hourly_loop_sends_replica_alert_through_ops_alerts(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "training_log.db"))
    sent = []
    monkeypatch.setattr(ops_alerts, "enqueue_text", sent.append)
    monkeypatch.setattr(admin_tasks, "check_replica_health", AsyncMock(return_value="реплика умерла"))

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(admin_tasks.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await admin_tasks.run_backup_staleness_check(MagicMock())

    assert sent == ["реплика умерла"]


async def test_startup_warns_on_prod_without_bucket(monkeypatch, caplog):
    monkeypatch.setattr(config, "APNS_ENV", "production")
    monkeypatch.setattr(config, "BUCKET_NAME", "")
    assert admin_tasks.warn_if_replication_missing() is True
    assert any(r.levelname == "ERROR" and "BUCKET_NAME" in r.message for r in caplog.records)

    caplog.clear()
    monkeypatch.setattr(config, "BUCKET_NAME", "bucket")
    assert admin_tasks.warn_if_replication_missing() is False
    monkeypatch.setattr(config, "BUCKET_NAME", "")
    monkeypatch.setattr(config, "APNS_ENV", "sandbox")
    assert admin_tasks.warn_if_replication_missing() is False  # локально/в тестах — молчим
    assert not caplog.records
