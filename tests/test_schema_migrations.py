"""Миграции схемы на базе, которая уже лежит на диске.

Обычные тесты поднимают `:memory:` и получают схему из CREATE TABLE, то есть
ветки миграций в них не выполняются вообще. А выполняются они ровно там, где
цена ошибки максимальна: на боевой базе с живыми пользователями при первом
запуске после релиза. Поэтому здесь база создаётся файлом, в неё вносится
«старое» состояние, и `init_db` прогоняется по ней вторым разом — как в бою.
"""
from unittest.mock import AsyncMock

import aiosqlite

import db as db_module


async def _legacy_db(tmp_path, sql: str):
    """База в состоянии до релиза: полная актуальная схема плюс `sql` сверху."""
    path = tmp_path / "legacy.sqlite3"
    await db_module.init_db(str(path))
    await db_module.close_db()
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(sql)
        await conn.commit()
    return str(path)


async def _columns(table: str) -> set[str]:
    cur = await db_module.conn().execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def test_the_stickers_column_is_dropped_from_an_existing_db(tmp_path):
    """Стикеры-реакции выпилены, и колонка настройки уходит вместе с ними. У всех
    существующих пользователей она есть, так что удалить её должна миграция, а не
    только CREATE TABLE для новых баз."""
    path = await _legacy_db(
        tmp_path,
        "ALTER TABLE users ADD COLUMN stickers_enabled INTEGER NOT NULL DEFAULT 1;",
    )

    await db_module.init_db(path)
    try:
        assert "stickers_enabled" not in await _columns("users")
    finally:
        await db_module.close_db()


async def test_dropping_it_does_not_touch_the_rest_of_the_user(tmp_path):
    """DROP COLUMN в SQLite перестраивает таблицу — значит проверять надо не только
    то, что колонка ушла, но и что вместе с ней не уехали данные."""
    path = await _legacy_db(
        tmp_path,
        "ALTER TABLE users ADD COLUMN stickers_enabled INTEGER NOT NULL DEFAULT 1;"
        "INSERT INTO users (telegram_id, username, unit, e1rm_formula, tz_offset, created_at)"
        " VALUES (4242, 'tester', 'lb', 'brzycki', 3, '2026-01-01T00:00:00');",
    )

    await db_module.init_db(path)
    try:
        user = await db_module.get_user(4242)
        assert user is not None
        assert user["username"] == "tester"
        assert user["unit"] == "lb"
        assert user["e1rm_formula"] == "brzycki"
        assert user["tz_offset"] == 3
    finally:
        await db_module.close_db()


async def test_a_second_start_is_a_no_op(tmp_path):
    """Миграции гоняются на каждом запуске: вторая попытка удалить уже удалённую
    колонку не должна ронять бота на старте."""
    path = await _legacy_db(
        tmp_path,
        "ALTER TABLE users ADD COLUMN stickers_enabled INTEGER NOT NULL DEFAULT 1;",
    )

    await db_module.init_db(path)
    await db_module.close_db()
    await db_module.init_db(path)
    try:
        assert "stickers_enabled" not in await _columns("users")
    finally:
        await db_module.close_db()


async def test_wal_pragma_failure_falls_back_instead_of_crashing_startup(monkeypatch):
    """Регрессия: на смонтированном сетевом томе PRAGMA journal_mode=WAL иногда
    падает с "disk I/O error" (см. докстринг db.py) — старт бота не должен
    зависеть от того, разрешит ли конкретный том WAL именно сегодня."""
    calls = []

    async def flaky_execute(sql, *a, **kw):
        if sql == "PRAGMA journal_mode=WAL":
            calls.append(sql)
            raise aiosqlite.OperationalError("disk I/O error")
        return await real_execute(sql, *a, **kw)

    await db_module.init_db(":memory:")
    real_execute = db_module._conn.execute
    monkeypatch.setattr(db_module._conn, "execute", flaky_execute)
    monkeypatch.setattr(db_module.asyncio, "sleep", AsyncMock())
    try:
        await db_module._enable_wal_with_fallback()  # must not raise
        assert len(calls) == 3  # исчерпал все попытки и сдался
    finally:
        monkeypatch.setattr(db_module._conn, "execute", real_execute)
        await db_module.close_db()


_LEGACY_API_TOKENS = (
    "DROP TABLE api_tokens;"
    "CREATE TABLE api_tokens (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE,"
    " created_at TEXT NOT NULL, last_used_at TEXT);"
    "INSERT INTO api_tokens VALUES ('old-token', 4242, '2026-01-01T00:00:00', '2026-02-01T00:00:00');"
)


async def test_api_tokens_unique_user_is_dropped_and_tokens_survive(tmp_path):
    """Раньше api_tokens держал UNIQUE(user_id) — один токен на человека, и
    вход на втором устройстве разлогинивал первое. Ограничение колонки ALTER
    не снимает, таблица перестраивается — и живой токен обязан это пережить,
    иначе релиз разлогинит всех разом."""
    path = await _legacy_db(tmp_path, _LEGACY_API_TOKENS)

    await db_module.init_db(path)
    try:
        assert await db_module.resolve_api_token("old-token") == 4242
        cur = await db_module.conn().execute(
            "SELECT created_at, last_used_at FROM api_tokens WHERE token = 'old-token'"
        )
        row = await cur.fetchone()
        assert row["created_at"] == "2026-01-01T00:00:00"
        new = await db_module.issue_api_token(4242)
        assert await db_module.resolve_api_token(new) == 4242
        assert await db_module.resolve_api_token("old-token") == 4242
        cur = await db_module.conn().execute("PRAGMA index_list(api_tokens)")
        names = {r["name"] for r in await cur.fetchall()}
        assert "idx_api_tokens_user" in names
        cur = await db_module.conn().execute(
            "SELECT name FROM sqlite_master WHERE name = 'api_tokens_rebuild'"
        )
        assert await cur.fetchone() is None
    finally:
        await db_module.close_db()

    # Второй запуск — уже без UNIQUE, перестраивать нечего, токены на месте.
    await db_module.init_db(path)
    try:
        cur = await db_module.conn().execute("SELECT COUNT(*) FROM api_tokens WHERE user_id = 4242")
        assert (await cur.fetchone())[0] == 2
    finally:
        await db_module.close_db()
