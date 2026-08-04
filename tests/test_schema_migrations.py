"""Миграции схемы на базе, которая уже лежит на диске.

Обычные тесты поднимают `:memory:` и получают схему из CREATE TABLE, то есть
ветки миграций в них не выполняются вообще. А выполняются они ровно там, где
цена ошибки максимальна: на боевой базе с живыми пользователями при первом
запуске после релиза. Поэтому здесь база создаётся файлом, в неё вносится
«старое» состояние, и `init_db` прогоняется по ней вторым разом — как в бою.
"""
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
