import sqlite3

import pytest

import db


class _FlakyConn:
    """Wraps the real connection, failing the first N VACUUM INTO calls with
    the exact race backup_to_file is meant to survive (see its docstring)."""

    def __init__(self, real_conn, fail_times):
        self._real = real_conn
        self._fail_times = fail_times
        self.calls = 0

    async def execute(self, sql, params=()):
        if sql.startswith("VACUUM INTO") and self.calls < self._fail_times:
            self.calls += 1
            raise sqlite3.OperationalError("cannot VACUUM - SQL statements in progress")
        self.calls += 1
        return await self._real.execute(sql, params)


@pytest.mark.asyncio
async def test_backup_to_file_retries_past_a_transient_vacuum_collision(fresh_db, tmp_path, monkeypatch):
    flaky = _FlakyConn(db.conn(), fail_times=2)
    monkeypatch.setattr(db, "conn", lambda: flaky)

    dest = str(tmp_path / "backup.db")
    await db.backup_to_file(dest)

    assert flaky.calls == 3
    import os

    assert os.path.exists(dest)


@pytest.mark.asyncio
async def test_backup_to_file_gives_up_after_persistent_vacuum_collisions(fresh_db, tmp_path, monkeypatch):
    flaky = _FlakyConn(db.conn(), fail_times=99)
    monkeypatch.setattr(db, "conn", lambda: flaky)

    dest = str(tmp_path / "backup.db")
    with pytest.raises(sqlite3.OperationalError):
        await db.backup_to_file(dest)


@pytest.mark.asyncio
async def test_backup_to_file_does_not_swallow_unrelated_errors(fresh_db, tmp_path, monkeypatch):
    async def boom(sql, params=()):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "conn", lambda: type("C", (), {"execute": staticmethod(boom)})())

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        await db.backup_to_file(str(tmp_path / "backup.db"))
