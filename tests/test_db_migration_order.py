"""Migration ordering: the block/routine "no duplicate exercise" UNIQUE
indexes must be created AFTER _dedupe_exercise_links runs, not before —
otherwise an on-disk DB that already has a duplicate (the old
merge_exercises bug) fails to start at all."""
import asyncio

import aiosqlite
import pytest

pytestmark = pytest.mark.asyncio


async def test_migrate_schema_dedupes_before_creating_unique_indexes(tmp_path):
    """A DB upgraded from an old build could already have the same exercise
    twice in one block (the bug merge_exercises used to have, before it learned
    to dedupe on the way in). Re-running init_db against such a file must not
    blow up creating the UNIQUE index over rows that violate it —
    _migrate_schema has to dedupe first, then create the index."""
    import db

    path = str(tmp_path / "legacy.db")
    db._write_lock = asyncio.Lock()
    await db.init_db(path)
    # Seed a duplicate the old (buggy) merge_exercises could have left behind.
    await db.conn().execute("DROP INDEX IF EXISTS idx_block_exercises_unique")
    await db.conn().execute("DROP INDEX IF EXISTS idx_routine_exercises_unique")
    group_id = await db.create_muscle_group(1000, "Ноги")
    ex_id = await db.create_exercise(1000, "Присед", group_id)
    workout_id = await db.create_workout(1000)
    block_id = await db.create_block(workout_id, "single")
    await db.conn().execute(
        "INSERT INTO block_exercises (block_id, exercise_id, order_in_block) VALUES (?, ?, 0)",
        (block_id, ex_id),
    )
    await db.conn().execute(
        "INSERT INTO block_exercises (block_id, exercise_id, order_in_block) VALUES (?, ?, 1)",
        (block_id, ex_id),
    )
    await db.conn().commit()
    await db.close_db()

    # Re-opening the same on-disk file re-runs init_db (executescript +
    # _migrate_schema) against a DB that already has the duplicate row and no
    # UNIQUE index — exactly the shape the audited bug hit in production.
    db._write_lock = asyncio.Lock()
    await db.init_db(path)
    try:
        cur = await db.conn().execute(
            "SELECT COUNT(*) AS n FROM block_exercises WHERE block_id = ? AND exercise_id = ?",
            (block_id, ex_id),
        )
        assert (await cur.fetchone())["n"] == 1
        # The index exists and is enforced going forward.
        with pytest.raises(aiosqlite.IntegrityError):
            await db.conn().execute(
                "INSERT INTO block_exercises (block_id, exercise_id, order_in_block) VALUES (?, ?, 2)",
                (block_id, ex_id),
            )
    finally:
        await db.close_db()


async def test_init_db_survives_a_base_without_conversation_id(tmp_path):
    """Живая база заводится один раз, дальше CREATE TABLE IF NOT EXISTS по ней
    — no-op: новая колонка появляется только через ALTER в _migrate_schema,
    который идёт ПОСЛЕ executescript(SCHEMA). Поэтому индекс по такой колонке
    в самом SCHEMA роняет старт целиком — ровно это и случилось на проде
    (`sqlite3.OperationalError: no such column: conversation_id`), а на пустой
    базе тестов не воспроизводилось никогда: там колонка приходит из CREATE
    TABLE.
    """
    import db

    path = str(tmp_path / "legacy.db")
    db._write_lock = asyncio.Lock()
    await db.init_db(path)
    # Откатываем базу к состоянию до архива разговоров: колонки нет, индекса
    # по ней — тоже.
    await db.conn().execute("DROP INDEX IF EXISTS idx_ai_conversation_turns_conv")
    await db.conn().execute("ALTER TABLE ai_conversation_turns DROP COLUMN conversation_id")
    await db.conn().execute("ALTER TABLE users DROP COLUMN ai_conversation_id")
    await db.conn().commit()
    await db.close_db()

    db._write_lock = asyncio.Lock()
    await db.init_db(path)
    try:
        cur = await db.conn().execute("PRAGMA table_info(ai_conversation_turns)")
        assert "conversation_id" in {row["name"] for row in await cur.fetchall()}
        cur = await db.conn().execute("PRAGMA table_info(users)")
        assert "ai_conversation_id" in {row["name"] for row in await cur.fetchall()}
        cur = await db.conn().execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("idx_ai_conversation_turns_conv",),
        )
        assert await cur.fetchone() is not None
    finally:
        await db.close_db()
