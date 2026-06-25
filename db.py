"""SQLite data access layer.

Single shared connection guarded by a write lock — a personal-bot's write
volume never justifies a real connection pool, and since aiosqlite already
funnels every statement through one dedicated worker thread, there's never
more than one query in flight regardless of journal mode. Journal mode is
the default rollback journal rather than WAL: WAL needs the filesystem to
support shared-memory mmap for its -wal/-shm files, which mounted
persistent-disk volumes (e.g. Amvera's persistenceMount) often don't,
causing sporadic "disk I/O error" — and WAL's only upside (concurrent
readers) doesn't apply to a single-connection app anyway.
"""

import asyncio
import datetime as dt
import os
from typing import Any, Iterable, Optional

import aiosqlite

import config
from seed_data import EXERCISE_TEMPLATES, MUSCLE_GROUP_PRESETS

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    created_at TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'kg',
    bodyweight REAL,
    e1rm_formula TEXT NOT NULL DEFAULT 'epley',
    hide_warmups INTEGER NOT NULL DEFAULT 0,
    show_extra_stats INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS muscle_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT NOT NULL,
    emoji TEXT,
    sort_order INTEGER NOT NULL DEFAULT 100,
    is_archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT NOT NULL,
    primary_group_id INTEGER,
    equipment TEXT,
    unilateral INTEGER NOT NULL DEFAULT 0,
    attachment TEXT,
    display_name TEXT NOT NULL,
    original_name TEXT,
    is_archived INTEGER NOT NULL DEFAULT 0,
    is_template INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    FOREIGN KEY (primary_group_id) REFERENCES muscle_groups (id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_exercises_user_name_ci
    ON exercises (user_id, LOWER(display_name)) WHERE is_template = 0;
CREATE INDEX IF NOT EXISTS idx_exercises_user_group ON exercises (user_id, primary_group_id);

CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    note TEXT,
    source TEXT NOT NULL DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS idx_workouts_user_status ON workouts (user_id, status);

CREATE TABLE IF NOT EXISTS workout_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_id INTEGER NOT NULL,
    order_index INTEGER NOT NULL,
    type TEXT NOT NULL DEFAULT 'single',
    FOREIGN KEY (workout_id) REFERENCES workouts (id)
);
CREATE INDEX IF NOT EXISTS idx_blocks_workout ON workout_blocks (workout_id);

CREATE TABLE IF NOT EXISTS block_exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    block_id INTEGER NOT NULL,
    exercise_id INTEGER NOT NULL,
    order_in_block INTEGER NOT NULL,
    FOREIGN KEY (block_id) REFERENCES workout_blocks (id),
    FOREIGN KEY (exercise_id) REFERENCES exercises (id)
);
CREATE INDEX IF NOT EXISTS idx_block_exercises_block ON block_exercises (block_id);

CREATE TABLE IF NOT EXISTS sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    block_id INTEGER NOT NULL,
    exercise_id INTEGER NOT NULL,
    round_index INTEGER NOT NULL,
    order_in_round INTEGER NOT NULL DEFAULT 0,
    weight REAL NOT NULL,
    reps INTEGER NOT NULL,
    is_warmup INTEGER NOT NULL DEFAULT 0,
    rpe REAL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (block_id) REFERENCES workout_blocks (id),
    FOREIGN KEY (exercise_id) REFERENCES exercises (id)
);
CREATE INDEX IF NOT EXISTS idx_sets_exercise ON sets (exercise_id);
CREATE INDEX IF NOT EXISTS idx_sets_block ON sets (block_id);

CREATE TABLE IF NOT EXISTS bodyweight_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    weight REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bodyweight_user ON bodyweight_logs (user_id, date);
"""

_conn: Optional[aiosqlite.Connection] = None
_write_lock = asyncio.Lock()


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def build_display_name(
    name: str,
    equipment: Optional[str] = None,
    unilateral: bool = False,
    attachment: Optional[str] = None,
) -> str:
    parts = [name.strip()]
    if unilateral:
        parts.append("одной рукой")
    if attachment:
        parts.append(attachment.strip())
    if equipment:
        parts.append(equipment.strip())
    return " · ".join(p for p in parts if p)


async def init_db(db_path: str = config.DB_PATH) -> None:
    global _conn
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _conn = await aiosqlite.connect(db_path)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA journal_mode=DELETE")
    await _conn.execute("PRAGMA foreign_keys=ON")
    await _conn.executescript(SCHEMA)
    await _conn.commit()
    await _migrate_schema()
    await _seed_globals()
    await _migrate_muscle_groups()


async def _column_names(table: str) -> set[str]:
    cur = await _conn.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    return {r["name"] for r in rows}


async def _migrate_schema() -> None:
    """Upgrade older on-disk databases to the current column set in-place."""
    await _conn.execute("DROP INDEX IF EXISTS idx_exercises_user_name")

    workout_cols = await _column_names("workouts")
    if "source" not in workout_cols:
        await _conn.execute("ALTER TABLE workouts ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")

    exercise_cols = await _column_names("exercises")
    if "original_name" not in exercise_cols:
        await _conn.execute("ALTER TABLE exercises ADD COLUMN original_name TEXT")
        await _conn.execute("UPDATE exercises SET original_name = name WHERE original_name IS NULL")

    await _conn.commit()


async def close_db() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def conn() -> aiosqlite.Connection:
    assert _conn is not None, "DB not initialized — call init_db() first"
    return _conn


async def _seed_globals() -> None:
    db = conn()
    cur = await db.execute("SELECT COUNT(*) FROM muscle_groups WHERE user_id IS NULL")
    (count,) = await cur.fetchone()
    if count == 0:
        async with _write_lock:
            for name, emoji, sort_order in MUSCLE_GROUP_PRESETS:
                await db.execute(
                    "INSERT INTO muscle_groups (user_id, name, emoji, sort_order) "
                    "VALUES (NULL, ?, ?, ?)",
                    (name, emoji, sort_order),
                )
            await db.commit()

    cur = await db.execute("SELECT COUNT(*) FROM exercises WHERE is_template = 1")
    (count,) = await cur.fetchone()
    if count == 0:
        groups = await list_muscle_groups(user_id=None, global_only=True)
        group_id_by_name = {g["name"]: g["id"] for g in groups}
        async with _write_lock:
            for group_name, ex_name in EXERCISE_TEMPLATES:
                group_id = group_id_by_name.get(group_name)
                display_name = build_display_name(ex_name)
                await db.execute(
                    "INSERT INTO exercises "
                    "(user_id, name, primary_group_id, display_name, original_name, is_template, created_at) "
                    "VALUES (NULL, ?, ?, ?, ?, 1, ?)",
                    (ex_name, group_id, display_name, ex_name, now_iso()),
                )
            await db.commit()


GROUP_MERGE_MAP = {
    "Ягодицы": "Ноги",
    "Икры": "Ноги",
    "Пресс": "Другое",
    "Предплечья": "Другое",
    "Трапеции": "Другое",
}


async def _migrate_muscle_groups() -> None:
    """Merge legacy muscle groups (from older on-disk DBs) into the current 7-group set."""
    db = conn()
    cur = await db.execute("SELECT id, name FROM muscle_groups WHERE user_id IS NULL")
    rows = await cur.fetchall()
    by_name = {r["name"]: r["id"] for r in rows}

    old_names = [n for n in GROUP_MERGE_MAP if n in by_name]
    if not old_names:
        return

    async with _write_lock:
        for target_name in {"Ноги", "Другое"}:
            if target_name not in by_name:
                preset = next((p for p in MUSCLE_GROUP_PRESETS if p[0] == target_name), None)
                emoji, sort_order = (preset[1], preset[2]) if preset else (None, 100)
                cur2 = await db.execute(
                    "INSERT INTO muscle_groups (user_id, name, emoji, sort_order) VALUES (NULL, ?, ?, ?)",
                    (target_name, emoji, sort_order),
                )
                by_name[target_name] = cur2.lastrowid

        for old_name in old_names:
            old_id = by_name[old_name]
            target_id = by_name[GROUP_MERGE_MAP[old_name]]
            await db.execute(
                "UPDATE exercises SET primary_group_id = ? WHERE primary_group_id = ?",
                (target_id, old_id),
            )
            await db.execute("UPDATE muscle_groups SET is_archived = 1 WHERE id = ?", (old_id,))
        await db.commit()


# ---------- users ----------

async def get_or_create_user(telegram_id: int, username: Optional[str]) -> aiosqlite.Row:
    db = conn()
    cur = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = await cur.fetchone()
    if row:
        return row
    async with _write_lock:
        await db.execute(
            "INSERT INTO users (telegram_id, username, created_at, unit, e1rm_formula) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                telegram_id,
                username,
                now_iso(),
                config.DEFAULT_UNIT,
                config.DEFAULT_E1RM_FORMULA,
            ),
        )
        await db.commit()
    cur = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    return await cur.fetchone()


async def get_user(telegram_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    return await cur.fetchone()


async def update_user(telegram_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    async with _write_lock:
        await conn().execute(
            f"UPDATE users SET {cols} WHERE telegram_id = ?",
            (*fields.values(), telegram_id),
        )
        await conn().commit()


# ---------- muscle groups ----------

async def list_muscle_groups(user_id: Optional[int], global_only: bool = False) -> list[aiosqlite.Row]:
    db = conn()
    if global_only or user_id is None:
        cur = await db.execute(
            "SELECT * FROM muscle_groups WHERE user_id IS NULL AND is_archived = 0 "
            "ORDER BY sort_order, name"
        )
    else:
        cur = await db.execute(
            "SELECT * FROM muscle_groups WHERE (user_id IS NULL OR user_id = ?) AND is_archived = 0 "
            "ORDER BY sort_order, name",
            (user_id,),
        )
    return await cur.fetchall()


async def get_muscle_group(group_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute("SELECT * FROM muscle_groups WHERE id = ?", (group_id,))
    return await cur.fetchone()


async def create_muscle_group(user_id: int, name: str, emoji: Optional[str] = None) -> int:
    async with _write_lock:
        cur = await conn().execute(
            "INSERT INTO muscle_groups (user_id, name, emoji, sort_order) VALUES (?, ?, ?, 100)",
            (user_id, name, emoji),
        )
        await conn().commit()
        return cur.lastrowid


async def archive_muscle_group(group_id: int) -> None:
    async with _write_lock:
        await conn().execute("UPDATE muscle_groups SET is_archived = 1 WHERE id = ?", (group_id,))
        await conn().commit()


# ---------- exercises ----------

async def list_user_exercises_in_group(user_id: int, group_id: int, limit: Optional[int] = None) -> list[aiosqlite.Row]:
    sql = (
        "SELECT e.*, "
        "(SELECT COUNT(DISTINCT wb.workout_id) FROM block_exercises be "
        "   JOIN workout_blocks wb ON wb.id = be.block_id "
        "   WHERE be.exercise_id = e.id) AS usage_count "
        "FROM exercises e "
        "WHERE e.user_id = ? AND e.primary_group_id = ? "
        "AND e.is_archived = 0 AND e.is_template = 0 "
        "ORDER BY e.last_used_at IS NULL, e.last_used_at DESC, usage_count DESC, e.display_name"
    )
    params: list[Any] = [user_id, group_id]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    cur = await conn().execute(sql, params)
    return await cur.fetchall()


async def list_user_exercises(user_id: int, limit: Optional[int] = None) -> list[aiosqlite.Row]:
    sql = (
        "SELECT e.*, "
        "(SELECT COUNT(DISTINCT wb.workout_id) FROM block_exercises be "
        "   JOIN workout_blocks wb ON wb.id = be.block_id "
        "   WHERE be.exercise_id = e.id) AS usage_count "
        "FROM exercises e "
        "WHERE e.user_id = ? "
        "AND e.is_archived = 0 AND e.is_template = 0 "
        "ORDER BY usage_count DESC, e.last_used_at IS NULL, e.last_used_at DESC, e.display_name"
    )
    params: list[Any] = [user_id]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    cur = await conn().execute(sql, params)
    return await cur.fetchall()


async def list_templates_in_group(group_id: int) -> list[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT * FROM exercises WHERE is_template = 1 AND primary_group_id = ? ORDER BY display_name",
        (group_id,),
    )
    return await cur.fetchall()


async def search_exercises(user_id: int, query: str, limit: int = 20) -> list[aiosqlite.Row]:
    # SQLite's LOWER()/LIKE only case-fold ASCII, so Cyrillic search is done in Python.
    cur = await conn().execute(
        "SELECT * FROM exercises WHERE user_id = ? AND is_archived = 0 AND is_template = 0 "
        "ORDER BY last_used_at IS NULL, last_used_at DESC, display_name",
        (user_id,),
    )
    rows = await cur.fetchall()
    needle = query.lower()
    matches = [r for r in rows if needle in r["display_name"].lower()]
    return matches[:limit]


async def get_exercise(exercise_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute("SELECT * FROM exercises WHERE id = ?", (exercise_id,))
    return await cur.fetchone()


async def find_exercise_by_name(user_id: int, name: str) -> Optional[aiosqlite.Row]:
    """Exact case-insensitive match on the bare name or full display name (Cyrillic-safe)."""
    cur = await conn().execute(
        "SELECT * FROM exercises WHERE user_id = ? AND is_archived = 0 AND is_template = 0", (user_id,)
    )
    rows = await cur.fetchall()
    needle = name.strip().lower()
    for r in rows:
        if r["name"].strip().lower() == needle or r["display_name"].strip().lower() == needle:
            return r
    return None


async def find_exercise_by_display_name(user_id: int, display_name: str) -> Optional[aiosqlite.Row]:
    """Match the unique index (user_id, LOWER(display_name)) exactly, archived or not."""
    cur = await conn().execute(
        "SELECT * FROM exercises WHERE user_id = ? AND is_template = 0 AND LOWER(display_name) = LOWER(?)",
        (user_id, display_name),
    )
    return await cur.fetchone()


async def create_exercise(
    user_id: int,
    name: str,
    group_id: Optional[int],
    equipment: Optional[str] = None,
    unilateral: bool = False,
    attachment: Optional[str] = None,
    notes: Optional[str] = None,
) -> int:
    """Create a new exercise, reusing an existing one with the same display name.

    A name collision (e.g. typing the same name twice, or forking the same template
    a second time) would otherwise hit the unique index and raise an unhandled
    IntegrityError, silently dropping whatever triggered the creation.
    """
    display_name = build_display_name(name, equipment, unilateral, attachment)

    existing = await find_exercise_by_display_name(user_id, display_name)
    if existing:
        if existing["is_archived"]:
            async with _write_lock:
                await conn().execute(
                    "UPDATE exercises SET is_archived = 0 WHERE id = ?", (existing["id"],)
                )
                await conn().commit()
        return existing["id"]

    async with _write_lock:
        try:
            cur = await conn().execute(
                "INSERT INTO exercises "
                "(user_id, name, primary_group_id, equipment, unilateral, attachment, "
                " display_name, original_name, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    name,
                    group_id,
                    equipment,
                    int(unilateral),
                    attachment,
                    display_name,
                    name,
                    notes,
                    now_iso(),
                ),
            )
            await conn().commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            existing = await find_exercise_by_display_name(user_id, display_name)
            if existing:
                return existing["id"]
            raise


async def fork_exercise_from_template(
    user_id: int,
    template_id: int,
    equipment: Optional[str] = None,
    unilateral: Optional[bool] = None,
    attachment: Optional[str] = None,
) -> int:
    template = await get_exercise(template_id)
    if template is None:
        raise ValueError("template not found")
    final_equipment = equipment if equipment is not None else template["equipment"]
    final_unilateral = unilateral if unilateral is not None else bool(template["unilateral"])
    final_attachment = attachment if attachment is not None else template["attachment"]
    return await create_exercise(
        user_id,
        template["name"],
        template["primary_group_id"],
        final_equipment,
        final_unilateral,
        final_attachment,
    )


async def update_exercise_name(exercise_id: int, name: str) -> bool:
    """Rename in place (same row/id) so existing sets keep their stats. Returns False on name clash."""
    ex = await get_exercise(exercise_id)
    display_name = build_display_name(name, ex["equipment"], bool(ex["unilateral"]), ex["attachment"])
    async with _write_lock:
        try:
            await conn().execute(
                "UPDATE exercises SET name = ?, display_name = ? WHERE id = ?",
                (name, display_name, exercise_id),
            )
        except aiosqlite.IntegrityError:
            return False
        await conn().commit()
        return True


async def touch_exercise_last_used(exercise_id: int) -> None:
    async with _write_lock:
        await conn().execute(
            "UPDATE exercises SET last_used_at = ? WHERE id = ?", (now_iso(), exercise_id)
        )
        await conn().commit()


async def archive_exercise(exercise_id: int) -> None:
    async with _write_lock:
        await conn().execute("UPDATE exercises SET is_archived = 1 WHERE id = ?", (exercise_id,))
        await conn().commit()


# ---------- workouts ----------

async def get_active_workout(user_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT * FROM workouts WHERE user_id = ? AND status = 'active'", (user_id,)
    )
    return await cur.fetchone()


async def create_workout(user_id: int) -> int:
    async with _write_lock:
        cur = await conn().execute(
            "INSERT INTO workouts (user_id, started_at, status) VALUES (?, ?, 'active')",
            (user_id, now_iso()),
        )
        await conn().commit()
        return cur.lastrowid


async def create_finished_workout(
    user_id: int, started_at: str, finished_at: str, source: str = "manual", note: Optional[str] = None
) -> int:
    """Insert a workout that's already finished — used for backfill/import (no live FSM)."""
    async with _write_lock:
        cur = await conn().execute(
            "INSERT INTO workouts (user_id, started_at, finished_at, status, source, note) "
            "VALUES (?, ?, ?, 'finished', ?, ?)",
            (user_id, started_at, finished_at, source, note),
        )
        await conn().commit()
        return cur.lastrowid


async def update_workout_date(workout_id: int, started_at: str, finished_at: Optional[str]) -> None:
    async with _write_lock:
        await conn().execute(
            "UPDATE workouts SET started_at = ?, finished_at = ? WHERE id = ?",
            (started_at, finished_at, workout_id),
        )
        await conn().commit()


async def get_workout(workout_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute("SELECT * FROM workouts WHERE id = ?", (workout_id,))
    return await cur.fetchone()


async def finish_workout(workout_id: int, note: Optional[str] = None) -> None:
    async with _write_lock:
        await conn().execute(
            "UPDATE workouts SET status = 'finished', finished_at = ?, note = ? WHERE id = ?",
            (now_iso(), note, workout_id),
        )
        await conn().commit()


async def discard_workout(workout_id: int) -> None:
    async with _write_lock:
        db = conn()
        await db.execute(
            "DELETE FROM sets WHERE block_id IN "
            "(SELECT id FROM workout_blocks WHERE workout_id = ?)",
            (workout_id,),
        )
        await db.execute(
            "DELETE FROM block_exercises WHERE block_id IN "
            "(SELECT id FROM workout_blocks WHERE workout_id = ?)",
            (workout_id,),
        )
        await db.execute("DELETE FROM workout_blocks WHERE workout_id = ?", (workout_id,))
        await db.execute("DELETE FROM workouts WHERE id = ?", (workout_id,))
        await db.commit()


async def list_workouts(
    user_id: int, limit: int = 10, offset: int = 0, status: str = "finished"
) -> list[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT * FROM workouts WHERE user_id = ? AND status = ? "
        "ORDER BY started_at DESC LIMIT ? OFFSET ?",
        (user_id, status, limit, offset),
    )
    return await cur.fetchall()


async def count_workouts(user_id: int, status: str = "finished") -> int:
    cur = await conn().execute(
        "SELECT COUNT(*) FROM workouts WHERE user_id = ? AND status = ?", (user_id, status)
    )
    (count,) = await cur.fetchone()
    return count


# ---------- blocks / block exercises ----------

async def create_block(workout_id: int, block_type: str) -> int:
    db = conn()
    cur = await db.execute(
        "SELECT COALESCE(MAX(order_index), -1) + 1 FROM workout_blocks WHERE workout_id = ?",
        (workout_id,),
    )
    (order_index,) = await cur.fetchone()
    async with _write_lock:
        cur = await db.execute(
            "INSERT INTO workout_blocks (workout_id, order_index, type) VALUES (?, ?, ?)",
            (workout_id, order_index, block_type),
        )
        await db.commit()
        return cur.lastrowid


async def add_block_exercise(block_id: int, exercise_id: int, order_in_block: int) -> None:
    async with _write_lock:
        await conn().execute(
            "INSERT INTO block_exercises (block_id, exercise_id, order_in_block) VALUES (?, ?, ?)",
            (block_id, exercise_id, order_in_block),
        )
        await conn().commit()


async def get_block(block_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute("SELECT * FROM workout_blocks WHERE id = ?", (block_id,))
    return await cur.fetchone()


async def get_block_exercises(block_id: int) -> list[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT be.*, e.display_name FROM block_exercises be "
        "JOIN exercises e ON e.id = be.exercise_id "
        "WHERE be.block_id = ? ORDER BY be.order_in_block",
        (block_id,),
    )
    return await cur.fetchall()


async def list_blocks_for_workout(workout_id: int) -> list[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT * FROM workout_blocks WHERE workout_id = ? ORDER BY order_index", (workout_id,)
    )
    return await cur.fetchall()


async def get_block_owner(block_id: int) -> Optional[int]:
    cur = await conn().execute(
        "SELECT w.user_id FROM workout_blocks b JOIN workouts w ON w.id = b.workout_id WHERE b.id = ?",
        (block_id,),
    )
    row = await cur.fetchone()
    return row["user_id"] if row else None


# ---------- sets ----------

async def add_set(
    block_id: int,
    exercise_id: int,
    round_index: int,
    order_in_round: int,
    weight: float,
    reps: int,
    is_warmup: bool = False,
    rpe: Optional[float] = None,
) -> int:
    async with _write_lock:
        cur = await conn().execute(
            "INSERT INTO sets "
            "(block_id, exercise_id, round_index, order_in_round, weight, reps, is_warmup, rpe, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (block_id, exercise_id, round_index, order_in_round, weight, reps, int(is_warmup), rpe, now_iso()),
        )
        await conn().commit()
        return cur.lastrowid


async def next_round_index(block_id: int, exercise_id: int) -> int:
    cur = await conn().execute(
        "SELECT COALESCE(MAX(round_index), 0) + 1 FROM sets WHERE block_id = ? AND exercise_id = ?",
        (block_id, exercise_id),
    )
    (idx,) = await cur.fetchone()
    return idx


async def delete_last_set_in_block(block_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT * FROM sets WHERE block_id = ? ORDER BY id DESC LIMIT 1", (block_id,)
    )
    row = await cur.fetchone()
    if row is None:
        return None
    async with _write_lock:
        await conn().execute("DELETE FROM sets WHERE id = ?", (row["id"],))
        await conn().commit()
    return row


async def list_sets_for_block(block_id: int) -> list[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT * FROM sets WHERE block_id = ? ORDER BY round_index, order_in_round, id",
        (block_id,),
    )
    return await cur.fetchall()


async def get_set(set_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute("SELECT * FROM sets WHERE id = ?", (set_id,))
    return await cur.fetchone()


async def get_set_owner(set_id: int) -> Optional[int]:
    cur = await conn().execute(
        "SELECT w.user_id FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "WHERE s.id = ?",
        (set_id,),
    )
    row = await cur.fetchone()
    return row["user_id"] if row else None


async def update_set(set_id: int, weight: float, reps: int) -> None:
    async with _write_lock:
        await conn().execute(
            "UPDATE sets SET weight = ?, reps = ? WHERE id = ?", (weight, reps, set_id)
        )
        await conn().commit()


async def delete_set(set_id: int) -> None:
    async with _write_lock:
        await conn().execute("DELETE FROM sets WHERE id = ?", (set_id,))
        await conn().commit()


async def list_sets_for_exercise(exercise_id: int, exclude_workout_id: Optional[int] = None) -> list[aiosqlite.Row]:
    """All working (non-warmup) sets for an exercise across finished workouts, oldest first."""
    sql = (
        "SELECT s.*, w.id AS workout_id, w.started_at FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "WHERE s.exercise_id = ? AND s.is_warmup = 0 AND w.status = 'finished'"
    )
    params: list[Any] = [exercise_id]
    if exclude_workout_id is not None:
        sql += " AND w.id != ?"
        params.append(exclude_workout_id)
    sql += " ORDER BY w.started_at, s.id"
    cur = await conn().execute(sql, params)
    return await cur.fetchall()


async def list_sets_for_workout_exercise(workout_id: int, exercise_id: int) -> list[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT s.* FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "WHERE b.workout_id = ? AND s.exercise_id = ? "
        "ORDER BY s.round_index, s.order_in_round, s.id",
        (workout_id, exercise_id),
    )
    return await cur.fetchall()


async def list_exercise_ids_for_workout(workout_id: int) -> list[int]:
    cur = await conn().execute(
        "SELECT DISTINCT s.exercise_id FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id WHERE b.workout_id = ?",
        (workout_id,),
    )
    rows = await cur.fetchall()
    return [r["exercise_id"] for r in rows]


# ---------- bodyweight diary ----------

async def add_bodyweight_entry(user_id: int, date: str, weight: float) -> int:
    async with _write_lock:
        cur = await conn().execute(
            "INSERT INTO bodyweight_logs (user_id, date, weight, created_at) VALUES (?, ?, ?, ?)",
            (user_id, date, weight, now_iso()),
        )
        await conn().commit()
        return cur.lastrowid


async def list_bodyweight_entries(user_id: int, limit: Optional[int] = None) -> list[aiosqlite.Row]:
    sql = "SELECT * FROM bodyweight_logs WHERE user_id = ? ORDER BY date, id"
    params: list[Any] = [user_id]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    cur = await conn().execute(sql, params)
    return await cur.fetchall()


async def get_latest_bodyweight(user_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT * FROM bodyweight_logs WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 1",
        (user_id,),
    )
    return await cur.fetchone()


# ---------- export ----------

async def export_rows_for_user(user_id: int) -> list[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT w.started_at, e.display_name AS exercise, "
        "s.round_index, s.weight, s.reps, s.is_warmup "
        "FROM sets s "
        "JOIN workout_blocks bt ON bt.id = s.block_id "
        "JOIN workouts w ON w.id = bt.workout_id "
        "JOIN exercises e ON e.id = s.exercise_id "
        "WHERE w.user_id = ? AND w.status = 'finished' "
        "ORDER BY w.started_at, s.id",
        (user_id,),
    )
    return await cur.fetchall()


# ---------- admin: daily stats & backup ----------

async def daily_workout_stats(date_str: str) -> dict[str, int]:
    """Distinct users and total workouts finished on a given calendar day (YYYY-MM-DD)."""
    cur = await conn().execute(
        "SELECT COUNT(DISTINCT user_id), COUNT(*) FROM workouts "
        "WHERE status = 'finished' AND date(finished_at) = ?",
        (date_str,),
    )
    users, workouts = await cur.fetchone()
    return {"users": users, "workouts": workouts}


async def backup_to_file(dest_path: str) -> None:
    """Write a consistent snapshot of the live database to dest_path (must not already exist)."""
    async with _write_lock:
        await conn().execute("VACUUM INTO ?", (dest_path,))
