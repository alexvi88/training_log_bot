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
import json
import os
import secrets
from typing import Any, Optional

import aiosqlite

import config
from seed_data import EXERCISE_TEMPLATES, MUSCLE_GROUP_PRESETS

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    created_at TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'kg',
    e1rm_formula TEXT NOT NULL DEFAULT 'epley',
    show_extra_stats INTEGER NOT NULL DEFAULT 1,
    pushes_enabled INTEGER NOT NULL DEFAULT 1,
    reply_keyboard_version INTEGER NOT NULL DEFAULT 0,
    ai_comments_enabled INTEGER NOT NULL DEFAULT 0,
    progression_hint_enabled INTEGER NOT NULL DEFAULT 1,
    tz_offset INTEGER NOT NULL DEFAULT 0,
    stickers_enabled INTEGER NOT NULL DEFAULT 1,
    -- Тренировочный профиль. Ровно те пять вводных, которые AI-тренер и так
    -- спрашивает перед сборкой программы (см. ai_trainer._system_prompt) — но
    -- раньше они жили только в переписке, и на следующий раз он спрашивал их
    -- заново. Всё nullable: профиль заполняется по кусочкам, из разговора или
    -- из настроек, и «не знаю» — нормальный ответ. `equipment` — JSON-список.
    experience TEXT,
    goal TEXT,
    days_per_week INTEGER,
    equipment TEXT,
    limitations TEXT
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
    seeded_from_program INTEGER NOT NULL DEFAULT 0,
    custom_photo_file_id TEXT,
    description TEXT,
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
    source TEXT NOT NULL DEFAULT 'manual',
    ai_comment TEXT,
    routine_id INTEGER
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

-- A note ("!болит плечо", "new training scheme") is tied to one workout's
-- attempt at an exercise, not the exercise itself — it shouldn't resurface on
-- every later session, only on the session it was actually written for.
CREATE TABLE IF NOT EXISTS exercise_notes (
    workout_id INTEGER NOT NULL,
    exercise_id INTEGER NOT NULL,
    note TEXT NOT NULL,
    PRIMARY KEY (workout_id, exercise_id),
    FOREIGN KEY (workout_id) REFERENCES workouts (id),
    FOREIGN KEY (exercise_id) REFERENCES exercises (id)
);
CREATE INDEX IF NOT EXISTS idx_exercise_notes_exercise ON exercise_notes (exercise_id);

CREATE TABLE IF NOT EXISTS sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    block_id INTEGER NOT NULL,
    exercise_id INTEGER NOT NULL,
    round_index INTEGER NOT NULL,
    order_in_round INTEGER NOT NULL DEFAULT 0,
    weight REAL NOT NULL,
    reps INTEGER NOT NULL,
    rpe REAL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (block_id) REFERENCES workout_blocks (id),
    FOREIGN KEY (exercise_id) REFERENCES exercises (id)
);
CREATE INDEX IF NOT EXISTS idx_sets_exercise ON sets (exercise_id);
CREATE INDEX IF NOT EXISTS idx_sets_block ON sets (block_id);

-- sent_on is the recipient's *own* calendar date, which is what the
-- one-push-per-day rule is about; sent_at stays server time, for the admin log.
-- They disagree whenever the user's send hour falls on the other side of the
-- server's midnight — every American zone on a UTC server.
CREATE TABLE IF NOT EXISTS pushes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    text TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    sent_on TEXT
);
CREATE INDEX IF NOT EXISTS idx_pushes_sent_at ON pushes (sent_at);
CREATE INDEX IF NOT EXISTS idx_pushes_telegram_id ON pushes (telegram_id, sent_at);

CREATE TABLE IF NOT EXISTS push_rotation (
    telegram_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    bag TEXT NOT NULL,
    PRIMARY KEY (telegram_id, category)
);

CREATE TABLE IF NOT EXISTS ai_search_usage (
    telegram_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (telegram_id, date)
);

CREATE TABLE IF NOT EXISTS ai_question_usage (
    telegram_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (telegram_id, date)
);

CREATE TABLE IF NOT EXISTS ai_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_chat_messages_user ON ai_chat_messages (telegram_id, id);

CREATE TABLE IF NOT EXISTS bodyweight_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    weight REAL NOT NULL,
    logged_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bodyweight_user ON bodyweight_logs (telegram_id, logged_at);

-- Дневник питания (см. handlers/food_diary.py). eaten_on — календарная дата
-- пользователя, к которой относится еда (она же ключ подневной группировки),
-- а не момент ввода: запись за прошлую дату заносится тем же путём.
-- Макросы/калории — оценка модели, поэтому все они nullable: запись без цифр
-- (модель не смогла или пользователь ввёл текстом без деталей) всё равно
-- имеет смысл как строка «что съел».
CREATE TABLE IF NOT EXISTS food_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    eaten_on TEXT NOT NULL,
    description TEXT NOT NULL,
    details TEXT,
    calories REAL,
    protein REAL,
    fat REAL,
    carbs REAL,
    photo_file_id TEXT,
    source TEXT NOT NULL DEFAULT 'text',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_food_entries_user_day ON food_entries (telegram_id, eaten_on, id);

-- A share is a snapshot, not a live reference: the link keeps working if the
-- owner later edits or deletes the original, and the recipient never gets read
-- access to someone else's live rows. Payload is JSON (see handlers/sharing.py).
CREATE TABLE IF NOT EXISTS shared_items (
    token TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- Сколько раз визитку забрали. Единственная обратная связь автору: отдал
    -- программу — и до сих пор не знал, взял её кто-нибудь или нет.
    taken_count INTEGER NOT NULL DEFAULT 0
);

-- A program is a named, ordered set of training days. It used to be nothing but
-- the string its days happened to share (routines.program_name), which meant
-- two programs with the same name *were* one program: renaming one onto another
-- merged them, adding the same catalog program twice piled duplicate days into
-- it, and the "handle" a screen was opened by had to be faked as MAX(routine.id).
-- Giving it a row of its own is what makes those states impossible rather than
-- merely unlikely — see _migrate_programs_from_names for the one-shot move.
--
-- `source` is where the program came from (manual|workout|catalog|ai|import) and
-- `source_ref` the detail worth keeping: the catalog key, or the @username who
-- shared it. Used for attribution on the program card, never for behaviour.
CREATE TABLE IF NOT EXISTS programs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    source_ref TEXT
);
-- The index is the point of the table: a name collision is now an error the
-- caller has to handle, not a silent merge.
--
-- It keys on `name_key` rather than LOWER(name) because SQLite's built-in
-- LOWER() only folds ASCII — «СПЛИТ» and «сплит» would sail straight past it,
-- and this bot's programs are named in Russian. `name_key` is the same string
-- run through Python's Unicode-aware str.lower() (see _program_key), so the
-- fold the index enforces is the fold find_program_by_name performs.
CREATE UNIQUE INDEX IF NOT EXISTS idx_programs_user_name
    ON programs (user_id, name_key);

-- program_name is dead weight kept for one release so a rollback still reads:
-- the live answer to "which program is this day in" is the programs join (see
-- _ROUTINE_SELECT). day_order is the day's position — it used to be implied by
-- ascending id, which is why days could never be reordered.
CREATE TABLE IF NOT EXISTS routines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    program_name TEXT,
    program_id INTEGER,
    day_order INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (program_id) REFERENCES programs (id)
);
CREATE INDEX IF NOT EXISTS idx_routines_user ON routines (user_id);
CREATE INDEX IF NOT EXISTS idx_routines_program ON routines (program_id, day_order);

CREATE TABLE IF NOT EXISTS routine_exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id INTEGER NOT NULL,
    exercise_id INTEGER NOT NULL,
    order_index INTEGER NOT NULL,
    target TEXT,
    progression TEXT,
    FOREIGN KEY (routine_id) REFERENCES routines (id),
    FOREIGN KEY (exercise_id) REFERENCES exercises (id)
);
CREATE INDEX IF NOT EXISTS idx_routine_exercises_routine ON routine_exercises (routine_id);

CREATE TABLE IF NOT EXISTS cost_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    event_type TEXT NOT NULL,
    model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cost_events_created ON cost_events (created_at);

CREATE TABLE IF NOT EXISTS achievements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    earned_at TEXT NOT NULL,
    UNIQUE (user_id, code)
);
CREATE INDEX IF NOT EXISTS idx_achievements_user ON achievements (user_id);
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
    # SQLite's built-in LOWER() only case-folds ASCII; register a Python-backed
    # one so Cyrillic search can be filtered in SQL instead of fetching every
    # row into the app and filtering there.
    await _conn.create_function("py_lower", 1, lambda s: s.lower() if s is not None else None)
    # WAL: в режиме DELETE каждый коммит создаёт и удаляет файл журнала (две
    # операции с метаданными на запись) и писатель блокирует читателей — на
    # единственном соединении это значит, что INSERT подхода останавливает все
    # чтения. Замер на файловой БД, 200 коммитов: 3.96 мс → 2.29 мс (−42%).
    await _conn.execute("PRAGMA journal_mode=WAL")
    await _conn.execute("PRAGMA foreign_keys=ON")
    await _conn.executescript(SCHEMA)
    await _conn.commit()
    await _migrate_schema()
    await _seed_globals()
    await _migrate_muscle_groups()
    await _sync_exercise_templates()
    await _run_one_shot_migrations()


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
    if "ai_comment" not in workout_cols:
        await _conn.execute("ALTER TABLE workouts ADD COLUMN ai_comment TEXT")
    if "routine_id" not in workout_cols:
        # Which saved routine the session was started from, when it was — the
        # only record of it (the plan itself lives in the FSM and is gone by the
        # next screen). Feeds the "programs you've actually been training by"
        # shortcuts on the picker; older rows just stay NULL.
        await _conn.execute("ALTER TABLE workouts ADD COLUMN routine_id INTEGER")
    if "followup_due_at" in workout_cols:
        # Post-workout followup push was removed — drop the columns a DB that
        # already ran the earlier migration would have.
        await _conn.execute("ALTER TABLE workouts DROP COLUMN followup_due_at")
        await _conn.execute("ALTER TABLE workouts DROP COLUMN followup_sent")

    exercise_cols = await _column_names("exercises")
    if "original_name" not in exercise_cols:
        await _conn.execute("ALTER TABLE exercises ADD COLUMN original_name TEXT")
        await _conn.execute("UPDATE exercises SET original_name = name WHERE original_name IS NULL")
    if "seeded_from_program" not in exercise_cols:
        await _conn.execute(
            "ALTER TABLE exercises ADD COLUMN seeded_from_program INTEGER NOT NULL DEFAULT 0"
        )
    if "custom_photo_file_id" not in exercise_cols:
        await _conn.execute("ALTER TABLE exercises ADD COLUMN custom_photo_file_id TEXT")
    if "description" not in exercise_cols:
        await _conn.execute("ALTER TABLE exercises ADD COLUMN description TEXT")

    shared_cols = await _column_names("shared_items")
    if "taken_count" not in shared_cols:
        await _conn.execute(
            "ALTER TABLE shared_items ADD COLUMN taken_count INTEGER NOT NULL DEFAULT 0"
        )

    user_cols = await _column_names("users")
    for profile_col, col_type in (
        ("experience", "TEXT"), ("goal", "TEXT"), ("days_per_week", "INTEGER"),
        ("equipment", "TEXT"), ("limitations", "TEXT"),
    ):
        if profile_col not in user_cols:
            await _conn.execute(f"ALTER TABLE users ADD COLUMN {profile_col} {col_type}")
    if "hide_warmups" in user_cols:
        await _conn.execute("ALTER TABLE users DROP COLUMN hide_warmups")
    if "bodyweight" in user_cols:
        await _conn.execute("ALTER TABLE users DROP COLUMN bodyweight")
    if "pushes_enabled" not in user_cols:
        await _conn.execute("ALTER TABLE users ADD COLUMN pushes_enabled INTEGER NOT NULL DEFAULT 1")
    if "ai_comments_enabled" not in user_cols:
        await _conn.execute("ALTER TABLE users ADD COLUMN ai_comments_enabled INTEGER NOT NULL DEFAULT 0")
    if "progression_hint_enabled" not in user_cols:
        await _conn.execute(
            "ALTER TABLE users ADD COLUMN progression_hint_enabled INTEGER NOT NULL DEFAULT 1"
        )
    if "tz_offset" not in user_cols:
        await _conn.execute("ALTER TABLE users ADD COLUMN tz_offset INTEGER NOT NULL DEFAULT 0")
    if "stickers_enabled" not in user_cols:
        await _conn.execute("ALTER TABLE users ADD COLUMN stickers_enabled INTEGER NOT NULL DEFAULT 1")
    if "rank_level_seen" not in user_cols:
        # Последнее объявленное звание. Нужно, чтобы «🎖 Новое звание» показывалось
        # ровно один раз: само звание считается на лету из тренировок и тоннажа,
        # так что без этой отметки карточка объявляла бы его каждый раз.
        await _conn.execute("ALTER TABLE users ADD COLUMN rank_level_seen INTEGER NOT NULL DEFAULT -1")
    if "food_macros_enabled" not in user_cols:
        # 1 = model estimates КБЖУ for food-diary entries (current default);
        # 0 = it just describes/saves the meal, no numbers — see handlers/food_diary.py.
        await _conn.execute("ALTER TABLE users ADD COLUMN food_macros_enabled INTEGER NOT NULL DEFAULT 1")
    if "e1rm_hint_seen" in user_cols:
        # Counted showings of the e1RM footnote, back when it faded out after a
        # few — it lives permanently on the progress screen now.
        await _conn.execute("ALTER TABLE users DROP COLUMN e1rm_hint_seen")
    if "reply_keyboard_version" not in user_cols:
        if "reply_keyboard_shown" in user_cols:
            # Superseded by a version counter so future button-set changes can
            # auto-refresh everyone's keyboard instead of only ever showing once.
            await _conn.execute(
                "ALTER TABLE users ADD COLUMN reply_keyboard_version INTEGER NOT NULL DEFAULT 0"
            )
            await _conn.execute(
                "UPDATE users SET reply_keyboard_version = 1 WHERE reply_keyboard_shown = 1"
            )
            await _conn.execute("ALTER TABLE users DROP COLUMN reply_keyboard_shown")
        else:
            await _conn.execute(
                "ALTER TABLE users ADD COLUMN reply_keyboard_version INTEGER NOT NULL DEFAULT 0"
            )

    set_cols = await _column_names("sets")
    if "is_warmup" in set_cols:
        await _conn.execute("ALTER TABLE sets DROP COLUMN is_warmup")

    routine_ex_cols = await _column_names("routine_exercises")
    if "target" not in routine_ex_cols:
        await _conn.execute("ALTER TABLE routine_exercises ADD COLUMN target TEXT")
    if "progression" not in routine_ex_cols:
        # Правило прогрессии для этого упражнения в этой программе, JSON — см.
        # set_routine_exercise_progression. До этого «сделал верх диапазона —
        # добавь 2.5кг» существовало только прозой в ответе AI-тренера и
        # умирало вместе с чатом.
        await _conn.execute("ALTER TABLE routine_exercises ADD COLUMN progression TEXT")

    routine_cols = await _column_names("routines")
    if "program_name" not in routine_cols:
        await _conn.execute("ALTER TABLE routines ADD COLUMN program_name TEXT")
    if "program_id" not in routine_cols:
        await _conn.execute("ALTER TABLE routines ADD COLUMN program_id INTEGER")
    if "day_order" not in routine_cols:
        await _conn.execute(
            "ALTER TABLE routines ADD COLUMN day_order INTEGER NOT NULL DEFAULT 0"
        )
    # The table may predate the index (and, on a fresh DB, executescript already
    # made it) — either way it has to exist before the one-shot migration starts
    # inserting programs, or the collision it is there to prevent slips through.
    program_cols = await _column_names("programs")
    if "name_key" not in program_cols:
        await _conn.execute("ALTER TABLE programs ADD COLUMN name_key TEXT NOT NULL DEFAULT ''")
    await _conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_programs_user_name ON programs (user_id, name_key)"
    )
    await _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_routines_program ON routines (program_id, day_order)"
    )

    push_cols = await _column_names("pushes")
    if "sent_on" not in push_cols:
        await _conn.execute("ALTER TABLE pushes ADD COLUMN sent_on TEXT")
        # Best available approximation for rows sent before the column existed:
        # the server date they were written on.
        await _conn.execute("UPDATE pushes SET sent_on = date(sent_at) WHERE sent_on IS NULL")

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


async def _sync_exercise_templates() -> None:
    """Reconcile the global template catalog with EXERCISE_TEMPLATES (idempotent).

    Templates (user_id IS NULL, is_template = 1) are a read-only catalog: picking
    one forks an independent user-owned copy (fork_exercise_from_template), so the
    template rows themselves are never referenced by workouts/sets and can be added,
    renamed or removed without touching anyone's history. Running this on every
    startup — rather than seeding once when the table is empty — is what lets an
    edit to EXERCISE_TEMPLATES reach already-deployed databases.

    Reconciliation is declarative: anything in the list but not in the DB is added,
    any global template no longer in the list is removed, and accidental duplicates
    (same group + name) left by older seeds are pruned down to a single row.
    """
    db = conn()
    groups = await list_muscle_groups(user_id=None, global_only=True)
    group_id_by_name = {g["name"]: g["id"] for g in groups}

    # Desired catalog keyed by (group_id, lower(name)) so matching is case-insensitive.
    desired: dict[tuple[int, str], str] = {}
    for group_name, ex_name in EXERCISE_TEMPLATES:
        group_id = group_id_by_name.get(group_name)
        if group_id is None:
            continue  # group missing (shouldn't happen for presets) — skip rather than orphan
        desired[(group_id, ex_name.lower())] = ex_name

    cur = await db.execute(
        "SELECT id, primary_group_id, name FROM exercises WHERE is_template = 1 AND user_id IS NULL"
    )
    existing = await cur.fetchall()

    kept: set[tuple[int, str]] = set()
    to_delete: list[int] = []
    for row in existing:
        key = (row["primary_group_id"], (row["name"] or "").lower())
        if key in desired and key not in kept:
            kept.add(key)  # first row for this catalog entry — keep it
        else:
            to_delete.append(row["id"])  # obsolete entry, or a duplicate of a kept one

    to_insert = [(gid, name) for (gid, _lname), name in desired.items() if (gid, _lname) not in kept]

    if not to_delete and not to_insert:
        return

    async with _write_lock:
        for ex_id in to_delete:
            await db.execute("DELETE FROM exercises WHERE id = ?", (ex_id,))
        for group_id, ex_name in to_insert:
            display_name = build_display_name(ex_name)
            await db.execute(
                "INSERT INTO exercises "
                "(user_id, name, primary_group_id, display_name, original_name, is_template, created_at) "
                "VALUES (NULL, ?, ?, ?, ?, 1, ?)",
                (ex_name, group_id, display_name, ex_name, now_iso()),
            )
        await db.commit()


# Bumped whenever a one-shot migration is added to _run_one_shot_migrations.
_SCHEMA_VERSION = 4


async def _run_one_shot_migrations() -> None:
    """Run migrations that must happen exactly once per database.

    Unlike the idempotent column adds in `_migrate_schema`, these rewrite user
    data based on what it looks like *right now*, so re-running them on every
    startup would keep re-applying them to rows the user has since created.
    `PRAGMA user_version` is SQLite's built-in slot for tracking this.
    """
    cur = await _conn.execute("PRAGMA user_version")
    (version,) = await cur.fetchone()
    if version >= _SCHEMA_VERSION:
        return
    if version < 1:
        await _backfill_seeded_from_program()
    if version < 2:
        await _group_prefixed_program_days()
    if version < 3:
        await _group_program_days_saved_together()
    if version < 4:
        # Must run last of the four: the three above are what put `program_name`
        # on the rows this one reads.
        await _migrate_programs_from_names()
    # Not parameterizable — SQLite only accepts a literal here. The value is an
    # internal constant, never user input.
    await _conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    await _conn.commit()


async def _migrate_programs_from_names() -> None:
    """Give every existing program a row of its own (see the `programs` table).

    Until now a program was the string its days shared, so this reads the
    distinct (user_id, program_name) pairs and mints one `programs` row per
    pair, then points the days at it. `day_order` becomes the position in
    ascending id — which is exactly what the old `list_program_days` already
    treated as day order, so nobody's days get shuffled by the move.

    Case matters here in a way it didn't before: the new unique index folds
    case, but the old grouping didn't, so a user with both «Сплит» and «сплит»
    had two programs and can only be given one. They are merged rather than
    dropped — the alternative is inventing a name for the loser — and the days
    of both keep their own names, so nothing becomes unreachable. This is also
    the state that made the old resolver delete the wrong program, so merging
    is the outcome the user was already living with, minus the data loss.

    One-shot (see `_run_one_shot_migrations`): afterwards programs are created
    by get_or_create_program and `program_name` stops being written at all.
    """
    cur = await _conn.execute(
        "SELECT id, user_id, program_name, created_at FROM routines "
        "WHERE program_name IS NOT NULL AND program_name != '' AND program_id IS NULL "
        "ORDER BY id"
    )
    rows = await cur.fetchall()
    if not rows:
        return
    program_ids: dict[tuple[int, str], int] = {}
    day_counts: dict[int, int] = {}
    for row in rows:
        key = (row["user_id"], _program_key(row["program_name"]))
        program_id = program_ids.get(key)
        if program_id is None:
            inserted = await _conn.execute(
                "INSERT INTO programs (user_id, name, name_key, created_at, source) "
                "VALUES (?, ?, ?, ?, 'manual')",
                (row["user_id"], row["program_name"].strip(), key[1], row["created_at"]),
            )
            program_id = inserted.lastrowid
            program_ids[key] = program_id
        order = day_counts.get(program_id, 0)
        day_counts[program_id] = order + 1
        await _conn.execute(
            "UPDATE routines SET program_id = ?, day_order = ? WHERE id = ?",
            (program_id, order, row["id"]),
        )
    await _conn.commit()


async def _group_prefixed_program_days() -> None:
    """Recover the program grouping for days saved before `program_name` existed.

    For a short while a saved program's days carried their program in the name
    itself — "PPL гипертрофия 3 дня — День 1 — Жим" — because there was nowhere
    else to put it. Those rows still exist, and without this they'd sit in the
    list as long, standalone, visibly-related-but-ungrouped entries forever.
    The prefix is real data, so split it back out: everything before the first
    " — " becomes program_name, the rest stays the day's name.

    Only prefixes shared by two or more of the user's routines are grouped —
    one routine that merely happens to contain " — " (e.g. "Грудь — тяжёлая")
    is not a program, and renaming it would be pure damage. Days from before
    the prefix existed carry no program information at all and are left alone;
    nothing in the row says which program they came from, and inventing a name
    would be worse than leaving them standalone.

    One-shot (see `_run_one_shot_migrations`): after this runs, a user is free
    to name two routines with a shared "X — " prefix on purpose, and re-running
    would silently regroup and rename them.
    """
    cur = await _conn.execute(
        "SELECT id, user_id, name FROM routines WHERE program_name IS NULL AND name LIKE '% — %'"
    )
    groups: dict[tuple[int, str], list[tuple[int, str]]] = {}
    for row in await cur.fetchall():
        program, _, day = row["name"].partition(" — ")
        program, day = program.strip(), day.strip()
        if not program or not day:
            continue
        groups.setdefault((row["user_id"], program), []).append((row["id"], day))
    for (_user_id, program), days in groups.items():
        if len(days) < 2:
            continue
        for routine_id, day in days:
            await _conn.execute(
                "UPDATE routines SET program_name = ?, name = ? WHERE id = ?",
                (program, day, routine_id),
            )
    await _conn.commit()


# A program's days are written back-to-back by one loop, so consecutive days
# land within a second or two of each other even with exercise forking in
# between. Saving a routine by hand can't be that fast — it needs a workout
# picked and a name typed — so a gap this small means "same batch".
_PROGRAM_BATCH_SECONDS = 10


async def _group_program_days_saved_together() -> None:
    """Group days of programs saved before anything recorded which program they
    belonged to — neither a program_name column nor the name prefix that
    briefly stood in for it (see _group_prefixed_program_days).

    Nothing in such a row names its program, but the rows still remember being
    written *together*: create_routine_from_program is called in a loop, so a
    program's days are consecutive in id and seconds apart, while a routine
    saved from a workout arrives alone after the user picks a session and types
    a name. That burst is the grouping; only the program's name is genuinely
    unrecoverable, so it gets a dated placeholder the user can rename rather
    than an invented title claiming to be the original.

    One-shot (see `_run_one_shot_migrations`): once programs record their own
    name, a burst of new routines is never again evidence of anything.
    """
    cur = await _conn.execute(
        "SELECT id, user_id, created_at FROM routines WHERE program_name IS NULL "
        "ORDER BY user_id, created_at, id"
    )
    batches: list[list[aiosqlite.Row]] = []
    for row in await cur.fetchall():
        current = batches[-1] if batches else None
        if current and current[-1]["user_id"] == row["user_id"] and _within_batch(current[-1], row):
            current.append(row)
        else:
            batches.append([row])
    for batch in batches:
        if len(batch) < 2:
            continue
        name = _batch_program_name(batch[0]["created_at"])
        for row in batch:
            await _conn.execute(
                "UPDATE routines SET program_name = ? WHERE id = ?", (name, row["id"])
            )
    await _conn.commit()


def _within_batch(previous, row) -> bool:
    try:
        gap = dt.datetime.fromisoformat(row["created_at"]) - dt.datetime.fromisoformat(
            previous["created_at"]
        )
    except ValueError:  # a hand-edited or pre-ISO timestamp — don't guess
        return False
    return 0 <= gap.total_seconds() <= _PROGRAM_BATCH_SECONDS


def _batch_program_name(created_at: str) -> str:
    try:
        return f"Программа от {dt.datetime.fromisoformat(created_at):%d.%m}"
    except ValueError:
        return "Программа"


async def _backfill_seeded_from_program() -> None:
    """Retroactively flag pristine, untouched, routine-less forks of a global
    template as seeded_from_program.

    One-shot (see `_run_one_shot_migrations`): the WHERE clause below can't
    tell a leftover from a program from an exercise the user picked out of
    "📋 Выбрать из шаблонов" themselves and hasn't trained yet — both are
    untrained, un-renamed forks of a template. Running it on every startup
    therefore made a just-added exercise vanish from the user's list (and from
    search) at the next restart, which reads as data loss.

    get_or_create_user_exercise_by_name only sets this flag going forward; a
    user exercise created the same way before that flag existed (e.g. by
    adding a ready-made program prior to this migration) defaulted to 0 and
    stays invisible to _VISIBLE_EXERCISE_FILTER's "seeded and orphaned" check
    forever. Re-deriving it here from display_name (matches only if never
    renamed — see update_exercise_name) plus "never trained, not in any
    routine" catches those old leftovers too. Idempotent — cheap to re-run
    on every startup, and something the user later adds to a routine or
    trains simply won't match the WHERE clause next time.
    """
    await _conn.execute(
        "UPDATE exercises SET seeded_from_program = 1 "
        "WHERE is_template = 0 AND seeded_from_program = 0 AND user_id IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM exercises t WHERE t.is_template = 1 AND t.display_name = exercises.display_name) "
        "AND NOT EXISTS (SELECT 1 FROM block_exercises be JOIN workout_blocks wb "
        "                ON wb.id = be.block_id WHERE be.exercise_id = exercises.id) "
        "AND NOT EXISTS (SELECT 1 FROM routine_exercises re WHERE re.exercise_id = exercises.id)"
    )
    await _conn.commit()


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


async def list_users_with_workout_counts(limit: int = 10, offset: int = 0) -> list[aiosqlite.Row]:
    """All users with their finished-workout count, most workouts first — for the admin panel."""
    cur = await conn().execute(
        "SELECT u.telegram_id, u.username, "
        "COUNT(w.id) FILTER (WHERE w.status = 'finished') AS workout_count "
        "FROM users u LEFT JOIN workouts w ON w.user_id = u.telegram_id "
        "GROUP BY u.telegram_id ORDER BY workout_count DESC, u.telegram_id "
        "LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return await cur.fetchall()


async def list_users_with_ai_message_counts(limit: int = 10, offset: int = 0) -> list[aiosqlite.Row]:
    """All users with their AI-trainer message count, most messages first — for the admin panel."""
    cur = await conn().execute(
        "SELECT u.telegram_id, u.username, "
        "COUNT(m.id) AS ai_message_count "
        "FROM users u LEFT JOIN ai_chat_messages m ON m.telegram_id = u.telegram_id "
        "GROUP BY u.telegram_id ORDER BY ai_message_count DESC, u.telegram_id "
        "LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return await cur.fetchall()


async def count_users() -> int:
    cur = await conn().execute("SELECT COUNT(*) FROM users")
    (count,) = await cur.fetchone()
    return count


async def list_all_telegram_ids() -> list[int]:
    """Every registered user's id — for admin broadcasts."""
    cur = await conn().execute("SELECT telegram_id FROM users")
    return [row["telegram_id"] for row in await cur.fetchall()]


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

async def list_muscle_groups(
    user_id: Optional[int], global_only: bool = False, order_by_usage: bool = False
) -> list[aiosqlite.Row]:
    """order_by_usage: put the groups this user actually trains most (by sets
    logged) first, so the exercise-adding picker doesn't make everyone scan past
    groups they never touch — falls back to the fixed sort_order/name for ties
    and for anyone with no history yet.
    """
    db = conn()
    if global_only or user_id is None:
        cur = await db.execute(
            "SELECT * FROM muscle_groups WHERE user_id IS NULL AND is_archived = 0 "
            "ORDER BY sort_order, name"
        )
        return await cur.fetchall()
    if order_by_usage:
        cur = await db.execute(
            "SELECT mg.*, COALESCE(uc.cnt, 0) AS usage_count FROM muscle_groups mg "
            "LEFT JOIN ("
            "  SELECT e.primary_group_id AS gid, COUNT(*) AS cnt FROM sets s "
            "  JOIN exercises e ON e.id = s.exercise_id "
            "  JOIN workout_blocks wb ON wb.id = s.block_id "
            "  JOIN workouts w ON w.id = wb.workout_id "
            "  WHERE w.user_id = ? GROUP BY e.primary_group_id"
            ") uc ON uc.gid = mg.id "
            "WHERE (mg.user_id IS NULL OR mg.user_id = ?) AND mg.is_archived = 0 "
            "ORDER BY usage_count DESC, mg.sort_order, mg.name",
            (user_id, user_id),
        )
        return await cur.fetchall()
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


async def unarchive_muscle_group(group_id: int) -> None:
    async with _write_lock:
        await conn().execute("UPDATE muscle_groups SET is_archived = 0 WHERE id = ?", (group_id,))
        await conn().commit()


# ---------- exercises ----------

# An exercise auto-created purely as a side effect of adding a ready-made program
# (see get_or_create_user_exercise_by_name) is hidden from the user's exercise
# lists again once it's neither actually used nor still referenced by one of
# their routines — otherwise deleting that program/routine leaves junk behind
# that the user never manually added and never trained.
_VISIBLE_EXERCISE_FILTER = (
    "(e.seeded_from_program = 0 "
    "OR EXISTS (SELECT 1 FROM block_exercises be JOIN workout_blocks wb "
    "           ON wb.id = be.block_id WHERE be.exercise_id = e.id) "
    "OR EXISTS (SELECT 1 FROM routine_exercises re WHERE re.exercise_id = e.id))"
)


async def list_user_exercises_in_group(
    user_id: int, group_id: int, limit: Optional[int] = None, offset: int = 0
) -> list[aiosqlite.Row]:
    sql = (
        "SELECT e.*, "
        "(SELECT COUNT(DISTINCT wb.workout_id) FROM block_exercises be "
        "   JOIN workout_blocks wb ON wb.id = be.block_id "
        "   WHERE be.exercise_id = e.id) AS usage_count "
        "FROM exercises e "
        "WHERE e.user_id = ? AND e.primary_group_id = ? "
        "AND e.is_archived = 0 AND e.is_template = 0 "
        f"AND {_VISIBLE_EXERCISE_FILTER} "
        "ORDER BY usage_count DESC, e.last_used_at IS NULL, e.last_used_at DESC, e.display_name"
    )
    params: list[Any] = [user_id, group_id]
    if limit:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    cur = await conn().execute(sql, params)
    return await cur.fetchall()


async def count_user_exercises_in_group(user_id: int, group_id: int) -> int:
    cur = await conn().execute(
        "SELECT COUNT(*) FROM exercises e WHERE e.user_id = ? AND e.primary_group_id = ? "
        "AND e.is_archived = 0 AND e.is_template = 0 "
        f"AND {_VISIBLE_EXERCISE_FILTER}",
        (user_id, group_id),
    )
    (count,) = await cur.fetchone()
    return count


async def list_user_exercises(
    user_id: int, limit: Optional[int] = None, offset: int = 0
) -> list[aiosqlite.Row]:
    sql = (
        "SELECT e.*, "
        "(SELECT COUNT(DISTINCT wb.workout_id) FROM block_exercises be "
        "   JOIN workout_blocks wb ON wb.id = be.block_id "
        "   WHERE be.exercise_id = e.id) AS usage_count "
        "FROM exercises e "
        "WHERE e.user_id = ? "
        "AND e.is_archived = 0 AND e.is_template = 0 "
        f"AND {_VISIBLE_EXERCISE_FILTER} "
        "ORDER BY usage_count DESC, e.last_used_at IS NULL, e.last_used_at DESC, e.display_name"
    )
    params: list[Any] = [user_id]
    if limit:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    cur = await conn().execute(sql, params)
    return await cur.fetchall()


async def list_recent_exercises(
    user_id: int, limit: int, exclude_ids: tuple[int, ...] = ()
) -> list[aiosqlite.Row]:
    """The user's most recently logged exercises, strictly by recency (unlike
    list_user_exercises, which ranks by usage_count first) — for a one-tap
    shortcut row so re-opening what was just done doesn't need the
    group-then-list picker.
    """
    sql = (
        "SELECT * FROM exercises e WHERE e.user_id = ? "
        "AND e.is_archived = 0 AND e.is_template = 0 AND e.last_used_at IS NOT NULL "
        f"AND {_VISIBLE_EXERCISE_FILTER} "
    )
    params: list[Any] = [user_id]
    if exclude_ids:
        sql += f"AND e.id NOT IN ({','.join('?' * len(exclude_ids))}) "
        params.extend(exclude_ids)
    sql += "ORDER BY e.last_used_at DESC LIMIT ?"
    params.append(limit)
    cur = await conn().execute(sql, params)
    return await cur.fetchall()


async def list_superset_partners(
    user_id: int, exercise_id: int, limit: int, exclude_ids: tuple[int, ...] = ()
) -> list[aiosqlite.Row]:
    """Exercises whose sets were actually logged in the same time window as
    `exercise_id`'s, within a shared workout — i.e. genuinely worked as a
    superset (switching back and forth), not just done somewhere else in the
    same session. Supersets aren't tracked as their own relationship, so this
    is inferred from each exercise's earliest/latest set timestamp per
    workout overlapping. Ranked by how many distinct workouts they overlapped in.
    """
    sql = (
        "WITH ranges AS ("
        "  SELECT wb.workout_id AS workout_id, s.exercise_id AS exercise_id, "
        "         MIN(s.created_at) AS min_at, MAX(s.created_at) AS max_at "
        "  FROM sets s JOIN workout_blocks wb ON wb.id = s.block_id "
        "  GROUP BY wb.workout_id, s.exercise_id"
        ") "
        "SELECT e.*, COUNT(DISTINCT r2.workout_id) AS pair_count "
        "FROM ranges r1 "
        "JOIN ranges r2 ON r2.workout_id = r1.workout_id AND r2.exercise_id != r1.exercise_id "
        "  AND r2.min_at <= r1.max_at AND r2.max_at >= r1.min_at "
        "JOIN exercises e ON e.id = r2.exercise_id "
        "WHERE r1.exercise_id = ? AND e.user_id = ? AND e.is_archived = 0 AND e.is_template = 0 "
        f"AND {_VISIBLE_EXERCISE_FILTER} "
    )
    params: list[Any] = [exercise_id, user_id]
    if exclude_ids:
        sql += f"AND e.id NOT IN ({','.join('?' * len(exclude_ids))}) "
        params.extend(exclude_ids)
    sql += "GROUP BY e.id ORDER BY pair_count DESC, e.display_name LIMIT ?"
    params.append(limit)
    cur = await conn().execute(sql, params)
    return await cur.fetchall()


_FOLLOWUP_MIN_WORKOUTS = 2


async def list_common_followups(
    user_id: int, exercise_id: int, limit: int, exclude_ids: tuple[int, ...] = ()
) -> list[aiosqlite.Row]:
    """Exercises most often logged *after* `exercise_id` within the same past
    workout (by block order), ranked by how many distinct workouts followed it
    that way — what the idle shortcuts offer once an exercise is finished,
    instead of just whatever was logged most recently regardless of sequence.

    A single past pairing isn't a habit: one leg day that happened to end with
    abs was enough to put "abs - pull down block" on an upper-body screen, and
    the alphabetical tiebreak between all the other one-off pairings put it
    first. So a followup has to have happened in at least
    _FOLLOWUP_MIN_WORKOUTS distinct workouts to be offered at all, and equally
    frequent ones are broken by recency rather than by name. The caller gets an
    empty list when nothing qualifies (see handlers.workout._idle_view).
    """
    sql = (
        "SELECT e.*, COUNT(DISTINCT wb2.workout_id) AS followup_count "
        "FROM workout_blocks wb1 "
        "JOIN block_exercises be1 ON be1.block_id = wb1.id AND be1.exercise_id = ? "
        "JOIN workout_blocks wb2 ON wb2.workout_id = wb1.workout_id AND wb2.order_index > wb1.order_index "
        "JOIN block_exercises be2 ON be2.block_id = wb2.id AND be2.exercise_id != ? "
        "JOIN exercises e ON e.id = be2.exercise_id "
        "WHERE e.user_id = ? AND e.is_archived = 0 AND e.is_template = 0 "
        f"AND {_VISIBLE_EXERCISE_FILTER} "
    )
    params: list[Any] = [exercise_id, exercise_id, user_id]
    if exclude_ids:
        sql += f"AND e.id NOT IN ({','.join('?' * len(exclude_ids))}) "
        params.extend(exclude_ids)
    sql += (
        "GROUP BY e.id HAVING followup_count >= ? "
        "ORDER BY followup_count DESC, e.last_used_at DESC LIMIT ?"
    )
    params.extend([_FOLLOWUP_MIN_WORKOUTS, limit])
    cur = await conn().execute(sql, params)
    return await cur.fetchall()


async def count_user_exercises(user_id: int) -> int:
    cur = await conn().execute(
        "SELECT COUNT(*) FROM exercises e WHERE e.user_id = ? AND e.is_archived = 0 AND e.is_template = 0 "
        f"AND {_VISIBLE_EXERCISE_FILTER}",
        (user_id,),
    )
    (count,) = await cur.fetchone()
    return count


async def list_all_exercise_templates() -> list[aiosqlite.Row]:
    """The whole catalog, unfiltered by group — used to spot catalog exercises
    the AI trainer names in an answer even if the user hasn't added them yet."""
    cur = await conn().execute("SELECT * FROM exercises WHERE is_template = 1 ORDER BY display_name")
    return await cur.fetchall()


async def list_templates_in_group(group_id: int) -> list[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT * FROM exercises WHERE is_template = 1 AND primary_group_id = ? ORDER BY display_name",
        (group_id,),
    )
    return await cur.fetchall()


async def search_exercises(user_id: int, query: str, limit: int = 20) -> list[aiosqlite.Row]:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    cur = await conn().execute(
        "SELECT * FROM exercises e WHERE e.user_id = ? AND e.is_archived = 0 AND e.is_template = 0 "
        "AND py_lower(e.display_name) LIKE '%' || py_lower(?) || '%' ESCAPE '\\' "
        f"AND {_VISIBLE_EXERCISE_FILTER} "
        "ORDER BY e.last_used_at IS NULL, e.last_used_at DESC, e.display_name "
        "LIMIT ?",
        (user_id, escaped, limit),
    )
    return await cur.fetchall()


async def search_exercise_templates(user_id: int, query: str, limit: int = 8) -> list[aiosqlite.Row]:
    """Catalog templates matching `query` that the user hasn't already got under
    the same name — the other half of exercise search.

    search_exercises only looks at exercises the user has already added, so a
    brand-new user typing "жим лёжа" gets "ничего не нашлось" even though a
    template with that exact name (photo, technique notes) exists — the picker
    routers fork a matching template on tap (see keyboards.exercises_keyboard's
    `templates` param) instead of leaving them to build one from scratch.
    """
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    cur = await conn().execute(
        "SELECT * FROM exercises t WHERE t.is_template = 1 "
        "AND py_lower(t.display_name) LIKE '%' || py_lower(?) || '%' ESCAPE '\\' "
        "AND NOT EXISTS ("
        "   SELECT 1 FROM exercises o WHERE o.user_id = ? AND o.is_template = 0 "
        "   AND o.is_archived = 0 AND py_lower(o.display_name) = py_lower(t.display_name)"
        ") "
        "ORDER BY t.display_name "
        "LIMIT ?",
        (escaped, user_id, limit),
    )
    return await cur.fetchall()


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
    """Find a user's exercise by name, case-insensitively, archived or not.

    Uses `py_lower`, not SQL `LOWER()`: the built-in only case-folds ASCII, so
    "Жим лёжа" and "жим лёжа" compare as different names — and since
    `create_exercise` relies on this lookup to reuse an existing row, that
    split one exercise into two, each with its own history, records and e1RM.
    (The unique index behind it has the same ASCII-only limitation and can't be
    fixed the same way: an index can only use deterministic built-ins. This
    check runs first, so the index never sees the collision.)

    Oldest first, so an account that already accumulated such a pair keeps
    resolving to the same one of them.
    """
    cur = await conn().execute(
        "SELECT * FROM exercises WHERE user_id = ? AND is_template = 0 "
        "AND py_lower(display_name) = py_lower(?) ORDER BY id LIMIT 1",
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


async def update_exercise_group(exercise_id: int, group_id: int) -> None:
    """Move an exercise to another muscle group in place (same row/id) so its
    sets and history stay attached to it."""
    async with _write_lock:
        await conn().execute(
            "UPDATE exercises SET primary_group_id = ? WHERE id = ?", (group_id, exercise_id)
        )
        await conn().commit()


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


async def unarchive_exercise(exercise_id: int) -> None:
    async with _write_lock:
        await conn().execute("UPDATE exercises SET is_archived = 0 WHERE id = ?", (exercise_id,))
        await conn().commit()


async def list_archived_exercises(user_id: int) -> list[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT * FROM exercises e WHERE e.user_id = ? AND e.is_archived = 1 AND e.is_template = 0 "
        "ORDER BY e.display_name",
        (user_id,),
    )
    return await cur.fetchall()


async def set_exercise_photo(exercise_id: int, file_id: str) -> None:
    """Store a user-uploaded reference photo (Telegram file_id) for an exercise,
    replacing whatever custom photo it had before."""
    async with _write_lock:
        await conn().execute(
            "UPDATE exercises SET custom_photo_file_id = ? WHERE id = ?", (file_id, exercise_id)
        )
        await conn().commit()


async def delete_exercise_photo(exercise_id: int) -> None:
    """Remove a user-uploaded reference photo, falling back to any bundled demo photos."""
    async with _write_lock:
        await conn().execute(
            "UPDATE exercises SET custom_photo_file_id = NULL WHERE id = ?", (exercise_id,)
        )
        await conn().commit()


async def set_workout_exercise_note(workout_id: int, exercise_id: int, note: Optional[str]) -> None:
    """Store a free-text note (technique cue, injury flag) for this exercise in
    this specific workout — not the exercise in general, so it doesn't
    resurface on unrelated later sessions. Passing None/empty clears it."""
    async with _write_lock:
        if note:
            await conn().execute(
                "INSERT INTO exercise_notes (workout_id, exercise_id, note) VALUES (?, ?, ?) "
                "ON CONFLICT (workout_id, exercise_id) DO UPDATE SET note = excluded.note",
                (workout_id, exercise_id, note),
            )
        else:
            await conn().execute(
                "DELETE FROM exercise_notes WHERE workout_id = ? AND exercise_id = ?",
                (workout_id, exercise_id),
            )
        await conn().commit()


async def get_workout_exercise_note(workout_id: int, exercise_id: int) -> Optional[str]:
    cur = await conn().execute(
        "SELECT note FROM exercise_notes WHERE workout_id = ? AND exercise_id = ?",
        (workout_id, exercise_id),
    )
    row = await cur.fetchone()
    return row["note"] if row else None


async def list_workout_notes_for_exercise(exercise_id: int) -> dict[int, str]:
    """Every {workout_id: note} this exercise has, for the progress screen to
    show each past session's own note next to it."""
    cur = await conn().execute(
        "SELECT workout_id, note FROM exercise_notes WHERE exercise_id = ?", (exercise_id,)
    )
    rows = await cur.fetchall()
    return {r["workout_id"]: r["note"] for r in rows}


async def set_exercise_description(exercise_id: int, description: Optional[str]) -> None:
    """Store the user's own technique description for an exercise — the same
    role exercise_descriptions.EXERCISE_DESCRIPTIONS plays for catalog templates,
    but per-user and editable, since a custom exercise has no template entry to
    fall back on. Passing None/empty clears it."""
    async with _write_lock:
        await conn().execute(
            "UPDATE exercises SET description = ? WHERE id = ?", (description or None, exercise_id)
        )
        await conn().commit()


async def merge_exercises(user_id: int, keep_id: int, drop_id: int) -> bool:
    """Merges drop_id into keep_id — for when the same movement got logged
    under two different names/entries (e.g. typed once as "ягодичный мостик",
    later as "glute bridge") and the user wants one combined history instead
    of two split ones.

    Repoints every set, workout block and routine slot from drop_id to
    keep_id, carries over drop's description/photo/notes if keep has none of
    its own, and then removes drop_id entirely. Returns False if either
    exercise isn't the user's own, is a catalog template, or they're the same
    row.
    """
    if keep_id == drop_id:
        return False
    keep = await get_exercise(keep_id)
    drop = await get_exercise(drop_id)
    if keep is None or drop is None:
        return False
    if keep["user_id"] != user_id or drop["user_id"] != user_id:
        return False
    if keep["is_template"] or drop["is_template"]:
        return False
    async with _write_lock:
        await conn().execute(
            "UPDATE sets SET exercise_id = ? WHERE exercise_id = ?", (keep_id, drop_id)
        )
        await conn().execute(
            "UPDATE block_exercises SET exercise_id = ? WHERE exercise_id = ?", (keep_id, drop_id)
        )
        await conn().execute(
            "UPDATE routine_exercises SET exercise_id = ? WHERE exercise_id = ?", (keep_id, drop_id)
        )
        # exercise_notes is keyed on (workout_id, exercise_id) — if a workout
        # already has a note under keep_id, drop_id's note for that same
        # workout would collide with the row it's about to become, so it's
        # dropped in favor of keep's.
        await conn().execute(
            "DELETE FROM exercise_notes WHERE exercise_id = ? AND workout_id IN "
            "(SELECT workout_id FROM exercise_notes WHERE exercise_id = ?)",
            (drop_id, keep_id),
        )
        await conn().execute(
            "UPDATE exercise_notes SET exercise_id = ? WHERE exercise_id = ?", (keep_id, drop_id)
        )
        if drop["last_used_at"] and (not keep["last_used_at"] or drop["last_used_at"] > keep["last_used_at"]):
            await conn().execute(
                "UPDATE exercises SET last_used_at = ? WHERE id = ?", (drop["last_used_at"], keep_id)
            )
        if not keep["description"] and drop["description"]:
            await conn().execute(
                "UPDATE exercises SET description = ? WHERE id = ?", (drop["description"], keep_id)
            )
        if not keep["custom_photo_file_id"] and drop["custom_photo_file_id"]:
            await conn().execute(
                "UPDATE exercises SET custom_photo_file_id = ? WHERE id = ?",
                (drop["custom_photo_file_id"], keep_id),
            )
        if not keep["notes"] and drop["notes"]:
            await conn().execute(
                "UPDATE exercises SET notes = ? WHERE id = ?", (drop["notes"], keep_id)
            )
        await conn().execute("DELETE FROM exercises WHERE id = ?", (drop_id,))
        await conn().commit()
    return True


# ---------- workouts ----------

async def get_active_workout(user_id: int) -> Optional[aiosqlite.Row]:
    # Explicit ordering so an account that already accumulated two active rows
    # keeps resolving to the same one instead of an arbitrary one per query.
    cur = await conn().execute(
        "SELECT * FROM workouts WHERE user_id = ? AND status = 'active' ORDER BY id LIMIT 1",
        (user_id,),
    )
    return await cur.fetchone()


async def get_or_create_active_workout(user_id: int) -> tuple[int, bool]:
    """The user's active workout, starting one if there isn't one. Returns
    (workout_id, created).

    Check and insert happen under the same lock: aiogram processes updates
    concurrently, so a double-tapped "🏋️ Начать тренировку" had both taps see
    no active workout and create one each. The loser became a permanent ghost —
    an empty active workout that "Продолжить" might open instead of the real
    one, and that resurfaces later as a stale-workout warning.
    """
    async with _write_lock:
        db = conn()
        cur = await db.execute(
            "SELECT id FROM workouts WHERE user_id = ? AND status = 'active' ORDER BY id LIMIT 1",
            (user_id,),
        )
        row = await cur.fetchone()
        if row is not None:
            return row["id"], False
        cur = await db.execute(
            "INSERT INTO workouts (user_id, started_at, status) VALUES (?, ?, 'active')",
            (user_id, now_iso()),
        )
        await db.commit()
        return cur.lastrowid, True


async def create_workout(
    user_id: int,
    started_at: Optional[str] = None,
    status: str = "active",
    routine_id: Optional[int] = None,
) -> int:
    """`routine_id` — программа, с которой стартовали (см. list_recent_programs);
    у обычной тренировки «с нуля» его нет."""
    async with _write_lock:
        cur = await conn().execute(
            "INSERT INTO workouts (user_id, started_at, status, routine_id) VALUES (?, ?, ?, ?)",
            (user_id, started_at or now_iso(), status, routine_id),
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


async def update_workout_note(workout_id: int, note: Optional[str]) -> None:
    """Attach/replace a finished workout's note — used by the "📝 Заметка" button
    on the completion card, after the workout has already been saved."""
    async with _write_lock:
        await conn().execute(
            "UPDATE workouts SET note = ? WHERE id = ?", (note or None, workout_id)
        )
        await conn().commit()


async def finish_workout(
    workout_id: int, note: Optional[str] = None, finished_at: Optional[str] = None
) -> bool:
    """Mark an active workout finished. Returns False if it wasn't active any
    more — i.e. somebody else just finished it.

    The status check lives in the UPDATE so it can't be raced: the caller's own
    "is it still active?" guard is several awaits away from getting here, which
    is enough of a window for a double-tapped "🏁 Завершить" to produce two
    finish cards for one workout.
    """
    async with _write_lock:
        cur = await conn().execute(
            "UPDATE workouts SET status = 'finished', finished_at = ?, note = ? "
            "WHERE id = ? AND status != 'finished'",
            (finished_at or now_iso(), note, workout_id),
        )
        await conn().commit()
        return cur.rowcount > 0


async def set_workout_ai_comment(workout_id: int, comment: Optional[str]) -> None:
    async with _write_lock:
        await conn().execute(
            "UPDATE workouts SET ai_comment = ? WHERE id = ?", (comment, workout_id)
        )
        await conn().commit()


async def discard_workout(workout_id: int) -> None:
    """Снести тренировку целиком — вместе со всем, что на неё ссылается.

    Порядок обязателен: `exercise_notes` держит FK на `workouts`, и без их
    удаления финальный DELETE падает по констрейнту уже после того, как
    подходы и блоки стёрты. Отсюда же и rollback: частичное удаление,
    оставленное в открытой транзакции, закоммитит первый же следующий
    (чужой) commit на этом соединении — и тренировка останется в базе
    выпотрошенной, без подходов, но со статусом.
    """
    async with _write_lock:
        db = conn()
        try:
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
            await db.execute("DELETE FROM exercise_notes WHERE workout_id = ?", (workout_id,))
            await db.execute("DELETE FROM workouts WHERE id = ?", (workout_id,))
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def list_workouts(
    user_id: int, limit: int = 10, offset: int = 0, status: str = "finished"
) -> list[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT * FROM workouts WHERE user_id = ? AND status = ? "
        "ORDER BY started_at DESC LIMIT ? OFFSET ?",
        (user_id, status, limit, offset),
    )
    return await cur.fetchall()


async def list_workout_contents(workout_ids: list[int]) -> dict[int, tuple[list[str], int]]:
    """{workout_id: ([exercise names in block order], total set count)} for a page
    of workouts, in two queries rather than a couple per workout.

    Feeds the history list, where the exercise names go in the message body —
    they're far too long for button labels.
    """
    if not workout_ids:
        return {}
    placeholders = ",".join("?" * len(workout_ids))

    cur = await conn().execute(
        "SELECT wb.workout_id, be.exercise_id, e.display_name "
        "FROM workout_blocks wb "
        "JOIN block_exercises be ON be.block_id = wb.id "
        "JOIN exercises e ON e.id = be.exercise_id "
        f"WHERE wb.workout_id IN ({placeholders}) "
        "ORDER BY wb.workout_id, wb.order_index, be.order_in_block",
        workout_ids,
    )
    names: dict[int, list[str]] = {}
    seen: dict[int, set[int]] = {}
    for r in await cur.fetchall():
        wid = r["workout_id"]
        if r["exercise_id"] in seen.setdefault(wid, set()):
            continue
        seen[wid].add(r["exercise_id"])
        names.setdefault(wid, []).append(r["display_name"])

    cur = await conn().execute(
        "SELECT wb.workout_id, COUNT(*) AS n FROM sets s "
        "JOIN workout_blocks wb ON wb.id = s.block_id "
        f"WHERE wb.workout_id IN ({placeholders}) GROUP BY wb.workout_id",
        workout_ids,
    )
    counts = {r["workout_id"]: r["n"] for r in await cur.fetchall()}

    return {wid: (names.get(wid, []), counts.get(wid, 0)) for wid in workout_ids}


async def search_workouts_by_exercise(user_id: int, query: str, limit: int = 20) -> list[aiosqlite.Row]:
    """Finished workouts containing an exercise whose name matches `query`,
    most recent first — the "в какой тренировке был жим" lookup, which the
    date-only history list can't answer.
    """
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    cur = await conn().execute(
        "SELECT DISTINCT w.* FROM workouts w "
        "JOIN workout_blocks b ON b.workout_id = w.id "
        "JOIN block_exercises be ON be.block_id = b.id "
        "JOIN exercises e ON e.id = be.exercise_id "
        "WHERE w.user_id = ? AND w.status = 'finished' "
        "  AND py_lower(e.display_name) LIKE '%' || py_lower(?) || '%' ESCAPE '\\' "
        "ORDER BY w.started_at DESC LIMIT ?",
        (user_id, escaped, limit),
    )
    return await cur.fetchall()


async def count_workouts(user_id: int, status: str = "finished") -> int:
    cur = await conn().execute(
        "SELECT COUNT(*) FROM workouts WHERE user_id = ? AND status = ?", (user_id, status)
    )
    (count,) = await cur.fetchone()
    return count


async def list_finished_workout_dates(user_id: int) -> list[str]:
    """Calendar date (YYYY-MM-DD) of each finished workout, ascending — for the dashboard.

    One row per workout (same-day workouts appear twice), so counts reflect
    workout volume rather than distinct active days.
    """
    cur = await conn().execute(
        "SELECT date(started_at) AS d FROM workouts "
        "WHERE user_id = ? AND status = 'finished' ORDER BY d",
        (user_id,),
    )
    return [r["d"] for r in await cur.fetchall()]


def _e1rm_sql(formula: str) -> str:
    """SQL mirror of analytics.e1rm — reps<=1 is the weight itself; brzycki
    falls back to epley above BRZYCKI_MAX_REPS (10), exactly like the Python."""
    epley = "s.weight * (1 + s.reps / 30.0)"
    if formula == "brzycki":
        return (
            "CASE WHEN s.reps <= 1 THEN s.weight "
            f"WHEN s.reps > 10 THEN {epley} "
            "ELSE s.weight * 36.0 / (37 - s.reps) END"
        )
    return f"CASE WHEN s.reps <= 1 THEN s.weight ELSE {epley} END"


async def max_e1rm_before_workout(
    user_id: int, exercise_id: int, workout_id: int, formula: str = "epley"
) -> float:
    """All-time best e1RM of the exercise across finished workouts, excluding
    `workout_id` — the bar a set must clear to earn the live 🥇 mark."""
    cur = await conn().execute(
        f"SELECT COALESCE(MAX({_e1rm_sql(formula)}), 0) AS mx FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "WHERE w.user_id = ? AND w.status = 'finished' AND w.id != ? "
        "AND s.exercise_id = ? AND s.reps > 0",
        (user_id, workout_id, exercise_id),
    )
    return (await cur.fetchone())["mx"]


async def achievement_extremes(user_id: int) -> dict[str, Any]:
    """Пер-сессионные экстремумы и счётчики для новых семей ачивок — одним
    проходом по finished-тренировкам, чтобы resync не ходил по ним по одной.

    Тоннаж и повторы — сырые, в единицах пользователя: нормализация в кг
    остаётся на achievement_sync, как и у остальных полей контекста.
    """
    cur = await conn().execute(
        "SELECT COALESCE(MAX(sets_count), 0) AS max_sets, "
        "       COALESCE(MAX(tonnage), 0) AS max_tonnage, "
        "       COALESCE(MAX(exercises_count), 0) AS max_exercises "
        "FROM ("
        "  SELECT w.id, COUNT(s.id) AS sets_count, SUM(s.weight * s.reps) AS tonnage, "
        "         COUNT(DISTINCT s.exercise_id) AS exercises_count "
        "  FROM sets s "
        "  JOIN workout_blocks b ON b.id = s.block_id "
        "  JOIN workouts w ON w.id = b.workout_id "
        "  WHERE w.user_id = ? AND w.status = 'finished' "
        "  GROUP BY w.id"
        ")",
        (user_id,),
    )
    per_session = dict(await cur.fetchone())
    cur = await conn().execute(
        "SELECT COALESCE(MAX(CASE WHEN s.weight = 0 THEN s.reps END), 0) AS max_bw_reps, "
        "       MAX(s.rpe) AS max_rpe, "
        "       COUNT(s.rpe) AS rpe_sets, "
        "       COUNT(DISTINCT e.primary_group_id) AS distinct_groups "
        "FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "JOIN exercises e ON e.id = s.exercise_id "
        "WHERE w.user_id = ? AND w.status = 'finished'",
        (user_id,),
    )
    per_set = dict(await cur.fetchone())
    cur = await conn().execute(
        "SELECT EXISTS("
        "  SELECT 1 FROM workout_blocks b JOIN workouts w ON w.id = b.workout_id "
        "  WHERE w.user_id = ? AND w.status = 'finished' AND b.type != 'single'"
        ") AS has_superset, "
        "(SELECT COUNT(*) FROM workouts w2 WHERE w2.user_id = ? AND w2.status = 'finished' "
        " AND CAST(strftime('%H', w2.started_at) AS INTEGER) < 7) AS early_workouts",
        (user_id, user_id),
    )
    extra = dict(await cur.fetchone())
    return {**per_session, **per_set, **extra}


async def count_bodyweight_logs(user_id: int) -> int:
    cur = await conn().execute(
        "SELECT COUNT(*) AS c FROM bodyweight_logs WHERE telegram_id = ?", (user_id,)
    )
    return (await cur.fetchone())["c"]


async def list_food_entry_dates(telegram_id: int) -> list[str]:
    """Все дни, в которые есть хоть одна запись еды, — для «Недели учёта»."""
    cur = await conn().execute(
        "SELECT DISTINCT eaten_on FROM food_entries WHERE telegram_id = ? ORDER BY eaten_on",
        (telegram_id,),
    )
    return [r["eaten_on"] for r in await cur.fetchall()]


async def max_weight_ever(user_id: int) -> float:
    """Heaviest single set (any exercise) across finished workouts — for weight-club
    achievements."""
    cur = await conn().execute(
        "SELECT COALESCE(MAX(s.weight), 0) AS mx FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "WHERE w.user_id = ? AND w.status = 'finished'",
        (user_id,),
    )
    return (await cur.fetchone())["mx"] or 0.0


async def count_distinct_exercises_used(user_id: int) -> int:
    cur = await conn().execute(
        "SELECT COUNT(DISTINCT s.exercise_id) AS c FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "WHERE w.user_id = ? AND w.status = 'finished'",
        (user_id,),
    )
    return (await cur.fetchone())["c"] or 0


async def list_achievement_codes(user_id: int) -> set[str]:
    cur = await conn().execute("SELECT code FROM achievements WHERE user_id = ?", (user_id,))
    return {r["code"] for r in await cur.fetchall()}


async def award_achievements(user_id: int, codes: set[str]) -> list[str]:
    """Record any of `codes` the user doesn't already hold; return the newly added
    ones (in a stable sorted order) so the caller can celebrate just those."""
    if not codes:
        return []
    existing = await list_achievement_codes(user_id)
    new = sorted(codes - existing)
    if not new:
        return []
    async with _write_lock:
        await conn().executemany(
            "INSERT OR IGNORE INTO achievements (user_id, code, earned_at) VALUES (?, ?, ?)",
            [(user_id, code, now_iso()) for code in new],
        )
        await conn().commit()
    return new


async def revoke_achievements(user_id: int, codes: set[str]) -> list[str]:
    """Take back badges the user's history no longer supports (a deleted or
    corrected workout), returning the codes actually removed."""
    if not codes:
        return []
    held = await list_achievement_codes(user_id)
    gone = sorted(codes & held)
    if not gone:
        return []
    async with _write_lock:
        await conn().executemany(
            "DELETE FROM achievements WHERE user_id = ? AND code = ?",
            [(user_id, code) for code in gone],
        )
        await conn().commit()
    return gone


async def list_finished_workouts_meta(user_id: int) -> list[aiosqlite.Row]:
    """id/started_at/finished_at of every finished workout, oldest first — enough
    to re-derive the per-workout achievements (early bird, marathon, 1 января)
    without loading their sets."""
    cur = await conn().execute(
        "SELECT id, started_at, finished_at FROM workouts "
        "WHERE user_id = ? AND status = 'finished' ORDER BY started_at",
        (user_id,),
    )
    return await cur.fetchall()


async def hall_of_fame_aggregates(user_id: int) -> dict[str, float]:
    """Lifetime totals for the Hall of Fame: tonnage moved, total working sets,
    and the longest single finished workout (seconds). All over finished workouts."""
    cur = await conn().execute(
        "SELECT COALESCE(SUM(s.weight * s.reps), 0) AS tonnage, COUNT(s.id) AS sets_count "
        "FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "WHERE w.user_id = ? AND w.status = 'finished'",
        (user_id,),
    )
    row = await cur.fetchone()
    cur2 = await conn().execute(
        "SELECT MAX((julianday(finished_at) - julianday(started_at)) * 86400.0) AS longest "
        "FROM workouts WHERE user_id = ? AND status = 'finished' AND finished_at IS NOT NULL",
        (user_id,),
    )
    longest = (await cur2.fetchone())["longest"]
    return {
        "tonnage": row["tonnage"] or 0.0,
        "sets_count": row["sets_count"] or 0,
        "longest_workout_seconds": longest or 0.0,
    }


async def last_session_by_group(user_id: int) -> dict[Optional[int], tuple[str, int]]:
    """Per muscle group: the date it was last trained and how many sets that
    session had — the two inputs a recovery estimate needs.

    One query rather than one per group: this feeds a screen the user opens
    several times per workout.
    """
    cur = await conn().execute(
        "SELECT gid, day, cnt FROM ("
        "  SELECT e.primary_group_id AS gid, date(w.started_at) AS day, COUNT(s.id) AS cnt,"
        "         ROW_NUMBER() OVER ("
        "             PARTITION BY e.primary_group_id ORDER BY date(w.started_at) DESC"
        "         ) AS rn"
        "  FROM sets s"
        "  JOIN workout_blocks b ON b.id = s.block_id"
        "  JOIN workouts w ON w.id = b.workout_id"
        "  JOIN exercises e ON e.id = s.exercise_id"
        "  WHERE w.user_id = ? AND w.status = 'finished'"
        "  GROUP BY e.primary_group_id, date(w.started_at)"
        ") WHERE rn = 1",
        (user_id,),
    )
    return {row["gid"]: (row["day"], row["cnt"]) for row in await cur.fetchall()}


async def weekly_volume_by_group(
    user_id: int, start_date: str, end_date: str
) -> dict[Optional[int], int]:
    """Count of working sets per muscle group across finished workouts in [start_date, end_date].

    Keyed by exercises.primary_group_id (None bucketed under the NULL key). Dates
    are calendar days (YYYY-MM-DD) compared against date(workouts.started_at).
    """
    cur = await conn().execute(
        "SELECT e.primary_group_id AS gid, COUNT(s.id) AS cnt "
        "FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "JOIN exercises e ON e.id = s.exercise_id "
        "WHERE w.user_id = ? AND w.status = 'finished' "
        "AND date(w.started_at) BETWEEN ? AND ? "
        "GROUP BY e.primary_group_id",
        (user_id, start_date, end_date),
    )
    return {row["gid"]: row["cnt"] for row in await cur.fetchall()}


# ---------- blocks / block exercises ----------

async def create_block(workout_id: int, block_type: str) -> int:
    """Append a block, choosing its order_index inside the INSERT.

    Reading MAX(order_index) first left a window: aiogram handles updates
    concurrently, so two blocks opened in quick succession (tapping an
    exercise twice, or "➕ Суперсет" racing the picker) both read the same
    value and inserted with it. Two blocks then shared an order_index and the
    workout's exercise order became arbitrary — in the card, in the history and
    in "next exercise" lookups. Same fix as append_set.
    """
    async with _write_lock:
        db = conn()
        cur = await db.execute(
            "INSERT INTO workout_blocks (workout_id, order_index, type) "
            "SELECT ?, COALESCE(MAX(order_index), -1) + 1, ? "
            "FROM workout_blocks WHERE workout_id = ?",
            (workout_id, block_type, workout_id),
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


async def workout_plan(workout_id: int) -> list[dict[str, list[int]]]:
    """Block-by-block exercise layout of a single workout, as
    ``[{"exercise_ids": [...]}, ...]`` — the same shape planned_blocks uses, so it
    can drive the "repeat workout" flow through _load_next_planned_block.

    Supersets (a block with several exercises) are preserved as multi-id blocks.
    Empty list if the workout has no exercises.
    """
    blocks = await list_blocks_for_workout(workout_id)
    plan: list[dict[str, list[int]]] = []
    for block in blocks:
        exercise_ids = [be["exercise_id"] for be in await get_block_exercises(block["id"])]
        if exercise_ids:
            plan.append({"exercise_ids": exercise_ids})
    return plan


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
    rpe: Optional[float] = None,
) -> int:
    async with _write_lock:
        cur = await conn().execute(
            "INSERT INTO sets "
            "(block_id, exercise_id, round_index, order_in_round, weight, reps, rpe, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (block_id, exercise_id, round_index, order_in_round, weight, reps, rpe, now_iso()),
        )
        await conn().commit()
        return cur.lastrowid


async def next_round_index(block_id: int, exercise_id: int) -> int:
    """The round_index a new set would take. Callers that are about to insert
    should use append_set instead — reading here and inserting afterwards leaves
    a gap two concurrent writers can both read through (see append_set)."""
    cur = await conn().execute(
        "SELECT COALESCE(MAX(round_index), 0) + 1 FROM sets WHERE block_id = ? AND exercise_id = ?",
        (block_id, exercise_id),
    )
    (idx,) = await cur.fetchone()
    return idx


async def append_set(
    block_id: int,
    exercise_id: int,
    order_in_round: int,
    weight: float,
    reps: int,
    rpe: Optional[float] = None,
) -> int:
    """Insert a set with the next round_index, choosing it under the write lock.

    aiogram handles updates concurrently, so two sets logged in quick succession
    (a typed set racing the "=" repeat, say) could both read the same
    next_round_index and insert with it. The INSERT itself does the SELECT, so
    there's no window between them.
    """
    async with _write_lock:
        cur = await conn().execute(
            "INSERT INTO sets "
            "(block_id, exercise_id, round_index, order_in_round, weight, reps, rpe, created_at) "
            "SELECT ?, ?, COALESCE(MAX(round_index), 0) + 1, ?, ?, ?, ?, ? "
            "FROM sets WHERE block_id = ? AND exercise_id = ?",
            (
                block_id, exercise_id, order_in_round, weight, reps, rpe, now_iso(),
                block_id, exercise_id,
            ),
        )
        await conn().commit()
        return cur.lastrowid


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


async def delete_block(block_id: int) -> None:
    async with _write_lock:
        await conn().execute("DELETE FROM block_exercises WHERE block_id = ?", (block_id,))
        await conn().execute("DELETE FROM workout_blocks WHERE id = ?", (block_id,))
        await conn().commit()


async def delete_block_and_sets(block_id: int) -> None:
    """Drop a block along with every set it holds — "remove this exercise from
    a past workout entirely". delete_block on its own assumes the block is
    already empty (every existing caller empties it first); this is for the
    one case where it isn't."""
    async with _write_lock:
        await conn().execute("DELETE FROM sets WHERE block_id = ?", (block_id,))
        await conn().execute("DELETE FROM block_exercises WHERE block_id = ?", (block_id,))
        await conn().execute("DELETE FROM workout_blocks WHERE id = ?", (block_id,))
        await conn().commit()


async def list_sets_for_block(block_id: int) -> list[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT * FROM sets WHERE block_id = ? ORDER BY round_index, order_in_round, id",
        (block_id,),
    )
    return await cur.fetchall()


async def delete_empty_blocks(workout_id: int, keep_block_id: Optional[int] = None) -> None:
    """Drop blocks that never got a set logged — an exercise added mid-workout
    and then abandoned shouldn't linger as a "подходов нет" placeholder in the
    finished summary/history. keep_block_id skips one block even if it's empty —
    used while the user is actively looking at it (e.g. just deleted its last set)."""
    for block in await list_blocks_for_workout(workout_id):
        if block["id"] == keep_block_id:
            continue
        if not await list_sets_for_block(block["id"]):
            await delete_block(block["id"])


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


async def update_set(set_id: int, weight: float, reps: int, rpe: Optional[float] = None) -> None:
    async with _write_lock:
        await conn().execute(
            "UPDATE sets SET weight = ?, reps = ?, rpe = ? WHERE id = ?", (weight, reps, rpe, set_id)
        )
        await conn().commit()


async def delete_set(set_id: int) -> None:
    async with _write_lock:
        await conn().execute("DELETE FROM sets WHERE id = ?", (set_id,))
        await conn().commit()


async def list_sets_for_exercise(exercise_id: int, exclude_workout_id: Optional[int] = None) -> list[aiosqlite.Row]:
    """All sets for an exercise across finished workouts, oldest first."""
    sql = (
        "SELECT s.*, w.id AS workout_id, w.started_at FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "WHERE s.exercise_id = ? AND w.status = 'finished'"
    )
    params: list[Any] = [exercise_id]
    if exclude_workout_id is not None:
        sql += " AND w.id != ?"
        params.append(exclude_workout_id)
    sql += " ORDER BY w.started_at, s.id"
    cur = await conn().execute(sql, params)
    return await cur.fetchall()


async def list_all_sets_by_exercise(user_id: int) -> list[aiosqlite.Row]:
    """Every set this user has logged across finished workouts, oldest first,
    carrying its exercise's display name.

    One query for the whole hall-of-fame screen: computing it per exercise means
    a DB round-trip per exercise the user has ever created, which on a long-lived
    account is slow enough for the tapped button's callback to time out.
    """
    cur = await conn().execute(
        "SELECT s.weight, s.reps, e.id AS exercise_id, e.display_name, "
        "       w.id AS workout_id, w.started_at "
        "FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "JOIN exercises e ON e.id = s.exercise_id "
        "WHERE w.user_id = ? AND w.status = 'finished' "
        "  AND e.is_archived = 0 AND e.is_template = 0 "
        "ORDER BY e.id, w.started_at, s.id",
        (user_id,),
    )
    return await cur.fetchall()


async def list_sets_for_workout_exercise(workout_id: int, exercise_id: int) -> list[aiosqlite.Row]:
    """Every set of one exercise in one workout, in the order the tracker shows
    them.

    Block order comes first because `round_index` restarts per block (see
    `append_set`): an exercise logged, closed and reopened has two blocks whose
    rounds both count from 1, so ordering by round alone would interleave them
    — 1st set of block 2 between the 1st and 2nd of block 1. view_builder
    merges blocks in `order_index` order, and anything indexing into this list
    by what the user sees (editing "2: 100 8", carry-forward's "last set")
    has to agree with it.
    """
    cur = await conn().execute(
        "SELECT s.* FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "WHERE b.workout_id = ? AND s.exercise_id = ? "
        "ORDER BY b.order_index, s.round_index, s.order_in_round, s.id",
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


async def get_workout_set_span(workout_id: int) -> Optional[tuple[str, str]]:
    """(first_set_created_at, last_set_created_at) for a workout, or None if it has no sets."""
    cur = await conn().execute(
        "SELECT MIN(s.created_at) AS first_at, MAX(s.created_at) AS last_at FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id WHERE b.workout_id = ?",
        (workout_id,),
    )
    row = await cur.fetchone()
    if row is None or row["first_at"] is None:
        return None
    return row["first_at"], row["last_at"]


async def find_last_finished_workout_with_exercise(user_id: int, exercise_id: int) -> Optional[int]:
    """Most recent finished workout that included this exercise, if any."""
    cur = await conn().execute(
        "SELECT wb.workout_id FROM block_exercises be "
        "JOIN workout_blocks wb ON wb.id = be.block_id "
        "JOIN workouts w ON w.id = wb.workout_id "
        "WHERE be.exercise_id = ? AND w.user_id = ? AND w.status = 'finished' "
        "ORDER BY w.started_at DESC LIMIT 1",
        (exercise_id, user_id),
    )
    row = await cur.fetchone()
    return row["workout_id"] if row else None


async def get_next_exercise_in_workout(workout_id: int, exercise_id: int) -> Optional[aiosqlite.Row]:
    """The exercise from the block right after `exercise_id`'s block, by block creation order.

    Blocks are created in the order exercises were picked during the workout
    (see create_block), so the next block's exercise is what the user did
    right after this one last time.
    """
    blocks = await list_blocks_for_workout(workout_id)
    after_order = None
    for block in blocks:
        block_exercises = await get_block_exercises(block["id"])
        if any(be["exercise_id"] == exercise_id for be in block_exercises):
            after_order = block["order_index"]
    if after_order is None:
        return None
    for block in blocks:
        if block["order_index"] <= after_order:
            continue
        block_exercises = await get_block_exercises(block["id"])
        if block_exercises:
            return block_exercises[0]
    return None


# ---------- routines (saved workout templates / splits) ----------

async def create_shared_item(owner_id: int, kind: str, payload: str) -> str:
    """Store a share snapshot and return its unguessable token."""
    token = secrets.token_urlsafe(8)
    async with _write_lock:
        await conn().execute(
            "INSERT INTO shared_items (token, owner_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, owner_id, kind, payload, now_iso()),
        )
        await conn().commit()
    return token


async def get_shared_item(token: str) -> Optional[aiosqlite.Row]:
    cur = await conn().execute("SELECT * FROM shared_items WHERE token = ?", (token,))
    return await cur.fetchone()


async def mark_shared_item_taken(token: str) -> None:
    async with _write_lock:
        await conn().execute(
            "UPDATE shared_items SET taken_count = taken_count + 1 WHERE token = ?", (token,)
        )
        await conn().commit()


async def delete_shared_item(token: str, owner_id: int) -> bool:
    """Отозвать визитку. Ссылка была вечной и неотзываемой: отдал программу —
    и обратно уже никак. True, если удалили (и это была визитка владельца)."""
    async with _write_lock:
        cur = await conn().execute(
            "DELETE FROM shared_items WHERE token = ? AND owner_id = ?", (token, owner_id)
        )
        await conn().commit()
        return cur.rowcount > 0


async def delete_shared_items_older_than(cutoff_iso: str) -> int:
    """Прополка `shared_items`: таблица только росла, ничто её не чистило."""
    async with _write_lock:
        cur = await conn().execute(
            "DELETE FROM shared_items WHERE created_at < ?", (cutoff_iso,)
        )
        await conn().commit()
        return cur.rowcount


# ---------- programs (a named, ordered set of training days) ----------
#
# Every routine query goes through this SELECT rather than `SELECT *`: the
# program's name lives on `programs` now, and joining it in under the old
# `program_name` alias is what lets the handlers keep reading
# `routine["program_name"]` unchanged. The legacy `routines.program_name`
# column is deliberately *not* selected — one name, one place.

_ROUTINE_COLUMNS = (
    "r.id, r.user_id, r.name, r.created_at, r.program_id, r.day_order, "
    "p.name AS program_name, p.source AS program_source, p.source_ref AS program_source_ref"
)
_ROUTINE_FROM = " FROM routines r LEFT JOIN programs p ON p.id = r.program_id "
_EXERCISE_COUNT = (
    ", (SELECT COUNT(*) FROM routine_exercises re WHERE re.routine_id = r.id) AS exercise_count"
)

_ROUTINE_SELECT = "SELECT " + _ROUTINE_COLUMNS + _ROUTINE_FROM
# Same rows plus how many exercises each day has — the list screens show it.
_ROUTINE_SELECT_COUNTED = "SELECT " + _ROUTINE_COLUMNS + _EXERCISE_COUNT + _ROUTINE_FROM


def _program_key(name: str) -> str:
    """The folded form a program name is deduplicated by.

    Python's str.lower() rather than SQL's: SQLite's LOWER() is ASCII-only, so
    doing this in the query would let «Сплит» and «сплит» coexist — which is the
    exact pair that used to make the AI trainer delete the wrong program.
    """
    return name.strip().lower()


async def create_program(
    user_id: int, name: str, source: str = "manual", source_ref: Optional[str] = None
) -> Optional[int]:
    """A new program, or None if the user already has one by that name.

    None rather than an exception because every caller has something better to
    do with a collision than crash: the catalog offers to open the existing one,
    the importer renames the incoming copy, the AI trainer asks. Before the
    `programs` table there was no collision to report — the days just merged
    into whatever program already had the name.
    """
    async with _write_lock:
        try:
            cur = await conn().execute(
                "INSERT INTO programs (user_id, name, name_key, created_at, source, source_ref) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, name.strip(), _program_key(name), now_iso(), source, source_ref),
            )
        except aiosqlite.IntegrityError:
            return None
        await conn().commit()
        return cur.lastrowid


async def find_program_by_name(user_id: int, name: str) -> Optional[aiosqlite.Row]:
    """Case- and whitespace-insensitive lookup — the same folding the unique
    index uses, so "found nothing" here means create_program will succeed."""
    cur = await conn().execute(
        "SELECT * FROM programs WHERE user_id = ? AND name_key = ?",
        (user_id, _program_key(name)),
    )
    return await cur.fetchone()


async def get_or_create_program(
    user_id: int, name: str, source: str = "manual", source_ref: Optional[str] = None
) -> int:
    """For callers that genuinely want "the program with this name, whichever it
    is" — chiefly the legacy `create_routine(program_name=...)` shim. Anything
    the user can trigger twice should use create_program and handle the None."""
    existing = await find_program_by_name(user_id, name)
    if existing is not None:
        return existing["id"]
    program_id = await create_program(user_id, name, source, source_ref)
    if program_id is None:  # lost a race with a concurrent insert
        existing = await find_program_by_name(user_id, name)
        return existing["id"]
    return program_id


async def get_program(program_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute("SELECT * FROM programs WHERE id = ?", (program_id,))
    return await cur.fetchone()


async def unique_program_name(user_id: int, name: str, suffix: Optional[str] = None) -> str:
    """A free name near `name` — «PPL (от @vasya)», then «PPL (2)», «PPL (3)»…

    For the paths where a collision shouldn't stop the user: importing a shared
    program, or taking the same catalog program a second time on purpose.
    """
    name = name.strip()
    if await find_program_by_name(user_id, name) is None:
        return name
    if suffix:
        candidate = f"{name} ({suffix})"
        if await find_program_by_name(user_id, candidate) is None:
            return candidate
    for n in range(2, 100):
        candidate = f"{name} ({n})"
        if await find_program_by_name(user_id, candidate) is None:
            return candidate
    return f"{name} ({secrets.token_hex(2)})"


async def next_day_order(program_id: int) -> int:
    cur = await conn().execute(
        "SELECT COALESCE(MAX(day_order), -1) + 1 FROM routines WHERE program_id = ?",
        (program_id,),
    )
    (order,) = await cur.fetchone()
    return order


async def move_routine_to_program(routine_id: int, program_id: Optional[int]) -> None:
    """Put a day into a program (appended last), or take it out of one.

    `program_id=None` makes it a standalone routine again — the "вынести день из
    программы" direction, which was impossible while a program was just a shared
    string.
    """
    order = await next_day_order(program_id) if program_id is not None else 0
    async with _write_lock:
        await conn().execute(
            "UPDATE routines SET program_id = ?, day_order = ? WHERE id = ?",
            (program_id, order, routine_id),
        )
        await conn().commit()


async def reorder_program_day(routine_id: int, direction: str) -> None:
    """Swap a day with its neighbour above ("up") or below ("down"). No-op at
    either end — same shape as reorder_routine_exercise one level down."""
    routine = await get_routine(routine_id)
    if routine is None or routine["program_id"] is None:
        return
    days = await list_program_days_by_id(routine["program_id"])
    ids = [d["id"] for d in days]
    if routine_id not in ids:
        return
    idx = ids.index(routine_id)
    neighbour = idx - 1 if direction == "up" else idx + 1
    if not 0 <= neighbour < len(ids):
        return
    a, b = days[idx], days[neighbour]
    async with _write_lock:
        await conn().execute(
            "UPDATE routines SET day_order = ? WHERE id = ?", (b["day_order"], a["id"])
        )
        await conn().execute(
            "UPDATE routines SET day_order = ? WHERE id = ?", (a["day_order"], b["id"])
        )
        await conn().commit()


async def create_routine(
    user_id: int,
    name: str,
    program_name: Optional[str] = None,
    program_id: Optional[int] = None,
    day_order: Optional[int] = None,
) -> int:
    """One training day. Standalone unless it's given a program.

    `program_id` is the real handle; `program_name` is the older shim, kept
    because several callers still speak in names — it resolves to a program,
    creating one if the user has none by that name. Pass one or the other.
    """
    if program_id is None and program_name:
        program_id = await get_or_create_program(user_id, program_name)
    if program_id is not None and day_order is None:
        day_order = await next_day_order(program_id)
    async with _write_lock:
        cur = await conn().execute(
            "INSERT INTO routines (user_id, name, created_at, program_id, day_order) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, name, now_iso(), program_id, day_order or 0),
        )
        await conn().commit()
        return cur.lastrowid


async def add_routine_exercise(
    routine_id: int, exercise_id: int, order_index: int, target: Optional[str] = None
) -> None:
    async with _write_lock:
        await conn().execute(
            "INSERT INTO routine_exercises (routine_id, exercise_id, order_index, target) "
            "VALUES (?, ?, ?, ?)",
            (routine_id, exercise_id, order_index, target),
        )
        await conn().commit()


async def append_routine_exercise(
    routine_id: int, exercise_id: int, target: Optional[str] = None
) -> None:
    """Add an exercise to the end of a routine that already exists — the "✏️ edit
    an already-saved program" path, as opposed to add_routine_exercise's use
    building a fresh routine where the caller tracks order_index itself.

    The order_index is chosen inside the INSERT so two exercises added in quick
    succession can't both read the same MAX and land on the same position —
    see create_block.
    """
    async with _write_lock:
        await conn().execute(
            "INSERT INTO routine_exercises (routine_id, exercise_id, order_index, target) "
            "SELECT ?, ?, COALESCE(MAX(order_index), -1) + 1, ? "
            "FROM routine_exercises WHERE routine_id = ?",
            (routine_id, exercise_id, target, routine_id),
        )
        await conn().commit()


async def remove_routine_exercise(routine_exercise_id: int) -> None:
    async with _write_lock:
        await conn().execute("DELETE FROM routine_exercises WHERE id = ?", (routine_exercise_id,))
        await conn().commit()


async def reorder_routine_exercise(routine_exercise_id: int, direction: str) -> None:
    """Swap a routine exercise with its neighbor above ("up") or below ("down")
    among the routine's visible (non-archived) exercises. No-op at either end."""
    entry = await get_routine_exercise(routine_exercise_id)
    if entry is None:
        return
    exercises = await list_routine_exercises(entry["routine_id"])
    ids = [ex["id"] for ex in exercises]
    if routine_exercise_id not in ids:
        return
    idx = ids.index(routine_exercise_id)
    neighbor_idx = idx - 1 if direction == "up" else idx + 1
    if neighbor_idx < 0 or neighbor_idx >= len(ids):
        return
    a, b = exercises[idx], exercises[neighbor_idx]
    async with _write_lock:
        await conn().execute(
            "UPDATE routine_exercises SET order_index = ? WHERE id = ?", (b["order_index"], a["id"])
        )
        await conn().execute(
            "UPDATE routine_exercises SET order_index = ? WHERE id = ?", (a["order_index"], b["id"])
        )
        await conn().commit()


async def get_routine_exercise(routine_exercise_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT * FROM routine_exercises WHERE id = ?", (routine_exercise_id,)
    )
    return await cur.fetchone()


async def list_routines(user_id: int) -> list[aiosqlite.Row]:
    cur = await conn().execute(
        _ROUTINE_SELECT_COUNTED
        + "WHERE r.user_id = ? ORDER BY r.created_at DESC, r.id DESC",
        (user_id,),
    )
    return await cur.fetchall()


async def list_programs(user_id: int) -> list[aiosqlite.Row]:
    """Multi-day programs the user has saved, one row per program.

    `program_name` is aliased alongside `name` so the callers written against
    the old name-grouped query keep reading; `anchor_id` likewise still points
    at a day, for keyboards that haven't moved to `id` yet.

    Sorted by "when I last actually trained by it", falling back to creation
    time for a program never used: the list is a working set, and a split from
    last spring shouldn't sit above the one you're mid-way through.
    """
    cur = await conn().execute(
        "SELECT p.id, p.id AS program_id, p.name, p.name AS program_name, "
        "p.created_at, p.source, p.source_ref, "
        "COUNT(r.id) AS day_count, MAX(r.id) AS anchor_id, "
        "(SELECT MAX(w.started_at) FROM workouts w JOIN routines rr ON rr.id = w.routine_id "
        "  WHERE rr.program_id = p.id) AS last_trained_at "
        "FROM programs p LEFT JOIN routines r ON r.program_id = p.id "
        "WHERE p.user_id = ? GROUP BY p.id "
        "ORDER BY COALESCE(last_trained_at, p.created_at) DESC, p.id DESC",
        (user_id,),
    )
    return await cur.fetchall()


async def list_recent_programs(
    user_id: int, since: str, limit: int = 3
) -> list[dict[str, Any]]:
    """Программы, по которым человек реально тренировался начиная с `since`.

    Считаем по `workouts.routine_id` — по факту проведённых тренировок, а не по
    тому, что лежит в списке программ: на экране «начать тренировку» нужны те
    два-три сплита, между которыми человек ходит сейчас.

    Дни одной программы схлопываются в одну строку (тренировался по «ноги» и по
    «верх» — это одна программа), одиночные шаблоны без программы идут сами по
    себе. `program_id` — чем открывается экран программы; `anchor_id` оставлен
    для старых клавиатур, он же routine последнего пройденного дня.
    """
    cur = await conn().execute(
        "SELECT r.id AS routine_id, r.name AS routine_name, r.program_id, "
        "p.name AS program_name, MAX(w.started_at) AS last_started "
        "FROM workouts w JOIN routines r ON r.id = w.routine_id "
        "LEFT JOIN programs p ON p.id = r.program_id "
        "WHERE w.user_id = ? AND r.user_id = ? AND w.started_at >= ? "
        "GROUP BY r.id ORDER BY MAX(w.started_at) DESC",
        (user_id, user_id, since),
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in await cur.fetchall():
        key = f"program:{row['program_id']}" if row["program_id"] else f"routine:{row['routine_id']}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "name": row["program_name"] or row["routine_name"],
                "program_id": row["program_id"],
                "routine_id": row["routine_id"],
                "anchor_id": row["routine_id"],
                "last_started": row["last_started"],
            }
        )
        if len(out) >= limit:
            break
    return out


async def list_program_days_by_id(program_id: int) -> list[aiosqlite.Row]:
    """The program's days in the order the user put them in.

    Ordered by `day_order` (id only as a tiebreaker for rows written before the
    column existed) — day order used to be "ascending id", which is why days
    could be added but never moved.
    """
    cur = await conn().execute(
        _ROUTINE_SELECT_COUNTED
        + "WHERE r.program_id = ? ORDER BY r.day_order ASC, r.id ASC",
        (program_id,),
    )
    return await cur.fetchall()


async def list_program_days(user_id: int, program_name: str) -> list[aiosqlite.Row]:
    """By-name shim over list_program_days_by_id, for callers that still hold a
    name rather than an id. Unknown name → no days, never someone else's."""
    program = await find_program_by_name(user_id, program_name)
    if program is None or program["user_id"] != user_id:
        return []
    return await list_program_days_by_id(program["id"])


async def list_standalone_routines(user_id: int) -> list[aiosqlite.Row]:
    """Routines that aren't part of a program — shown directly in the list."""
    cur = await conn().execute(
        _ROUTINE_SELECT_COUNTED
        + "WHERE r.user_id = ? AND r.program_id IS NULL "
        "ORDER BY r.created_at DESC, r.id DESC",
        (user_id,),
    )
    return await cur.fetchall()


async def get_routine(routine_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute(_ROUTINE_SELECT + "WHERE r.id = ?", (routine_id,))
    return await cur.fetchone()


async def list_routine_exercises(routine_id: int) -> list[aiosqlite.Row]:
    """Routine's exercises in order, joined with the (non-archived) exercise display name.

    Exercises archived after being added to the routine are dropped so a routine
    never resurrects something the user removed from their catalog.
    """
    cur = await conn().execute(
        "SELECT re.*, e.display_name FROM routine_exercises re "
        "JOIN exercises e ON e.id = re.exercise_id "
        "WHERE re.routine_id = ? AND e.is_archived = 0 "
        "ORDER BY re.order_index",
        (routine_id,),
    )
    return await cur.fetchall()


async def rename_program_by_id(program_id: int, new_name: str) -> bool:
    """True if renamed, False if the user already has a program by that name.

    The False case used to be a silent merge: `UPDATE routines SET program_name`
    over every day meant renaming «Бета» to «Альфа» quietly folded both into one
    program, with no way back — the UI can't take a day out of a program. Now
    the unique index refuses and the caller gets to ask.
    """
    new_name = new_name.strip()
    async with _write_lock:
        try:
            await conn().execute(
                "UPDATE programs SET name = ?, name_key = ? WHERE id = ?",
                (new_name, _program_key(new_name), program_id),
            )
        except aiosqlite.IntegrityError:
            return False
        await conn().commit()
        return True


async def rename_program(user_id: int, program_name: str, new_name: str) -> bool:
    """By-name shim over rename_program_by_id."""
    program = await find_program_by_name(user_id, program_name)
    if program is None:
        return False
    return await rename_program_by_id(program["id"], new_name)


async def merge_programs(user_id: int, source_id: int, target_id: int) -> None:
    """Fold every day of `source_id` into `target_id` and drop the empty shell.

    The explicit version of what renaming one program onto another used to do by
    accident — offered as a choice when rename_program_by_id reports a
    collision, so "actually yes, join them" stays possible.
    """
    source = await get_program(source_id)
    target = await get_program(target_id)
    if source is None or target is None:
        return
    if source["user_id"] != user_id or target["user_id"] != user_id:
        return
    order = await next_day_order(target_id)
    for day in await list_program_days_by_id(source_id):
        async with _write_lock:
            await conn().execute(
                "UPDATE routines SET program_id = ?, day_order = ? WHERE id = ?",
                (target_id, order, day["id"]),
            )
            await conn().commit()
        order += 1
    async with _write_lock:
        await conn().execute("DELETE FROM programs WHERE id = ?", (source_id,))
        await conn().commit()


async def rename_routine(routine_id: int, name: str) -> None:
    async with _write_lock:
        await conn().execute("UPDATE routines SET name = ? WHERE id = ?", (name, routine_id))
        await conn().commit()


async def delete_routine(routine_id: int) -> None:
    """Delete one day. If it was the last day of its program, the program goes
    too — an empty program is a row nothing can be done with, and it would sit
    in the list reading «· 0 дней»."""
    routine = await get_routine(routine_id)
    program_id = routine["program_id"] if routine else None
    async with _write_lock:
        db = conn()
        await db.execute("DELETE FROM routine_exercises WHERE routine_id = ?", (routine_id,))
        await db.execute("DELETE FROM routines WHERE id = ?", (routine_id,))
        await db.commit()
    if program_id is not None and not await list_program_days_by_id(program_id):
        async with _write_lock:
            await conn().execute("DELETE FROM programs WHERE id = ?", (program_id,))
            await conn().commit()


async def delete_program_by_id(program_id: int) -> None:
    """Delete a program and every day belonging to it."""
    async with _write_lock:
        db = conn()
        day_ids = [
            r["id"] for r in await (
                await db.execute("SELECT id FROM routines WHERE program_id = ?", (program_id,))
            ).fetchall()
        ]
        for day_id in day_ids:
            await db.execute("DELETE FROM routine_exercises WHERE routine_id = ?", (day_id,))
        await db.execute("DELETE FROM routines WHERE program_id = ?", (program_id,))
        await db.execute("DELETE FROM programs WHERE id = ?", (program_id,))
        await db.commit()


async def delete_program(user_id: int, program_name: str) -> None:
    """By-name shim over delete_program_by_id."""
    program = await find_program_by_name(user_id, program_name)
    if program is not None:
        await delete_program_by_id(program["id"])


async def _find_global_template_by_name(name: str) -> Optional[aiosqlite.Row]:
    """Case-insensitive (Cyrillic-safe) match of a global template by its bare name."""
    cur = await conn().execute(
        "SELECT * FROM exercises WHERE is_template = 1 AND user_id IS NULL"
    )
    rows = await cur.fetchall()
    needle = name.strip().lower()
    for r in rows:
        if (r["name"] or "").strip().lower() == needle:
            return r
    return None


async def get_or_create_user_exercise_by_name(user_id: int, name: str) -> Optional[int]:
    """Resolve an exercise name to a user-owned exercise id, for instantiating a
    ready-made program (see create_routine_from_program).

    Returns an existing user exercise if there is one, otherwise forks the global
    template of that name into the user's catalog and returns the fork. Returns
    None only if the name matches neither — programs reference template names, so
    in practice resolution always succeeds.

    A freshly forked exercise is flagged seeded_from_program so the exercise
    lists (list_user_exercises*) can hide it again if the program/routine that
    introduced it gets deleted before the user ever actually trains it.
    """
    existing = await find_exercise_by_name(user_id, name)
    if existing:
        return existing["id"]
    template = await _find_global_template_by_name(name)
    if template is not None:
        ex_id = await fork_exercise_from_template(user_id, template["id"])
        async with _write_lock:
            await conn().execute(
                "UPDATE exercises SET seeded_from_program = 1 WHERE id = ?", (ex_id,)
            )
            await conn().commit()
        return ex_id
    return None


async def resolve_exercise_name(user_id: int, name: str) -> tuple[Optional[str], Optional[str]]:
    """Куда ляжет название упражнения, ничего при этом не создавая.

    Read-only двойник get_or_create_user_exercise_by_name: тем же порядком
    (сначала своё, потом глобальный шаблон) проверяет, резолвится ли имя
    вообще, и возвращает ("own"|"template", каноничное display_name) либо
    (None, None). Нужен AI-тренеру, чтобы показать состав предлагаемой
    программы до того, как пользователь согласился её сохранить, — предложение
    не должно форкать пользователю упражнения (см. ai_trainer.propose_program).
    """
    existing = await find_exercise_by_name(user_id, name)
    if existing is not None:
        return "own", existing["display_name"]
    template = await _find_global_template_by_name(name)
    if template is not None:
        return "template", template["display_name"]
    return None, None


async def count_routines(user_id: int) -> int:
    cur = await conn().execute("SELECT COUNT(*) FROM routines WHERE user_id = ?", (user_id,))
    row = await cur.fetchone()
    return row[0]


async def routine_budget(user_id: int, adding: int, freeing: int = 0) -> Optional[str]:
    """None if `adding` more days fit, else the message to show the user.

    The cap existed but only the AI-trainer path checked it, so the catalog,
    the importer and "save from a workout" each walked straight past it — and
    then the AI path started refusing on a total it hadn't created. One helper,
    called from all four doors.

    `freeing` is how many of the user's current days this operation replaces
    (an AI edit swapping a program's days for new ones): without it, editing a
    program to the same size hits the ceiling just because the old version is
    still there.
    """
    existing = await count_routines(user_id)
    if existing - freeing + adding <= config.MAX_ROUTINES_PER_USER:
        return None
    return (
        f"У тебя уже {existing} дней в программах — больше "
        f"{config.MAX_ROUTINES_PER_USER} не влезет. Удали лишние в «🗂 Программы» "
        "и попробуй ещё раз."
    )


async def set_routine_exercise_target(routine_exercise_id: int, target: Optional[str]) -> None:
    """Change the sets×reps scheme in place.

    Without this, changing «3×10» to «4×8» meant removing the exercise (behind a
    confirmation), adding it again — where it lands at the end — and walking it
    back up with the arrows.
    """
    async with _write_lock:
        await conn().execute(
            "UPDATE routine_exercises SET target = ? WHERE id = ?", (target, routine_exercise_id)
        )
        await conn().commit()


async def set_routine_exercise_progression(
    routine_exercise_id: int, progression: Optional[str]
) -> None:
    """Store the progression rule as JSON (see the `progression` column).

    «Закрыл верх диапазона — добавь 2.5кг» used to live only in the trainer's
    prose and died with the chat; here it survives to the session where it
    actually matters.
    """
    async with _write_lock:
        await conn().execute(
            "UPDATE routine_exercises SET progression = ? WHERE id = ?",
            (progression, routine_exercise_id),
        )
        await conn().commit()


async def progression_rule_for_workout(workout_id: int, exercise_id: int) -> Optional[dict]:
    """Правило прогрессии, которое прописала программа этой тренировки.

    None, если тренировка не по программе, упражнения в её дне нет или правило
    не записано, — тогда подсказка веса считает по умолчанию, как и раньше
    (analytics.suggest_progression). Без этой выборки правило оставалось
    записанным и показанным в превью, но на экране записи подхода не
    участвовало ни в чём.
    """
    cur = await conn().execute(
        "SELECT re.progression FROM workouts w "
        "JOIN routine_exercises re ON re.routine_id = w.routine_id "
        "WHERE w.id = ? AND re.exercise_id = ? AND re.progression IS NOT NULL",
        (workout_id, exercise_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    try:
        rule = json.loads(row["progression"])
    except (TypeError, ValueError):
        return None
    return rule if isinstance(rule, dict) else None


async def program_day_history(program_id: int) -> dict[int, tuple[str, int]]:
    """For each day of the program: (last time it was trained, how many times).

    Read off `workouts.routine_id`, which has been recorded since the column
    existed and until now fed exactly one screen. It's what «какой сегодня день»
    and «ноги ты делал дважды за полтора месяца» are both built from.
    """
    cur = await conn().execute(
        "SELECT r.id AS routine_id, MAX(w.started_at) AS last_started, COUNT(*) AS times "
        "FROM routines r JOIN workouts w ON w.routine_id = r.id "
        "WHERE r.program_id = ? GROUP BY r.id",
        (program_id,),
    )
    return {row["routine_id"]: (row["last_started"], row["times"]) for row in await cur.fetchall()}


async def next_program_day(program_id: int) -> Optional[aiosqlite.Row]:
    """Which day is due next — the thing the user otherwise has to remember.

    The day after the most recently trained one, wrapping round; the first day
    if the program has never been used. Deliberately a suggestion computed from
    history rather than a stored cursor: nothing to get stuck, and a session
    logged out of order fixes itself next time.
    """
    days = await list_program_days_by_id(program_id)
    if not days:
        return None
    history = await program_day_history(program_id)
    trained = [(history[d["id"]][0], i) for i, d in enumerate(days) if d["id"] in history]
    if not trained:
        return days[0]
    _, last_index = max(trained)
    return days[(last_index + 1) % len(days)]


async def create_routine_from_program(
    user_id: int,
    name: str,
    exercise_names: list[str | tuple[str, Optional[str]]],
    program_name: Optional[str] = None,
    program_id: Optional[int] = None,
) -> int:
    """Instantiate one ready-made program day as a routine.

    Each exercise name is resolved to the user's own copy (forking the global
    template when missing). Duplicate or unresolvable names are skipped so the
    routine stays clean.

    Each item may be a bare name, or an (name, target) tuple carrying the
    program's recommended sets×reps for that exercise (e.g. "4×6–8") — stored on
    the routine_exercises row so it can be shown again both on the routine and
    while logging a workout started from it. Programs the AI trainer builds land
    here the same way, carrying the target it picked (ai_trainer.propose_program).

    `program_id` (or the older `program_name`) groups the created day with the
    program's other days — see create_routine.
    """
    routine_id = await create_routine(user_id, name, program_name, program_id=program_id)
    seen: set[int] = set()
    order = 0
    for item in exercise_names:
        ex_name, target = item if isinstance(item, tuple) else (item, None)
        ex_id = await get_or_create_user_exercise_by_name(user_id, ex_name)
        if ex_id is None or ex_id in seen:
            continue
        seen.add(ex_id)
        await add_routine_exercise(routine_id, ex_id, order, target)
        order += 1
    return routine_id



async def workout_exercise_targets(workout_id: int) -> dict[int, str]:
    """«4×8» per exercise, read off what was actually logged in that session.

    A program snapshotted from a workout used to arrive with no scheme at all —
    the one creation path out of four that didn't set `target` — even though the
    sets were sitting right there. Reps that varied across the sets become a
    range («4×6–8»); a single set is «1×8».
    """
    cur = await conn().execute(
        "SELECT s.exercise_id, s.reps FROM sets s "
        "JOIN workout_blocks wb ON wb.id = s.block_id "
        "WHERE wb.workout_id = ? ORDER BY s.id",
        (workout_id,),
    )
    reps_by_exercise: dict[int, list[int]] = {}
    for row in await cur.fetchall():
        reps_by_exercise.setdefault(row["exercise_id"], []).append(row["reps"])
    targets: dict[int, str] = {}
    for ex_id, reps in reps_by_exercise.items():
        low, high = min(reps), max(reps)
        targets[ex_id] = f"{len(reps)}×{low}" if low == high else f"{len(reps)}×{low}–{high}"
    return targets


async def create_routine_from_workout(
    user_id: int,
    workout_id: int,
    name: str,
    program_id: Optional[int] = None,
    with_targets: bool = True,
) -> int:
    """Snapshot a finished workout's exercises (in block order, de-duplicated) as a routine.

    `with_targets` fills each exercise's scheme from the sets that were actually
    done (see workout_exercise_targets); `program_id` makes the snapshot a day
    of an existing program instead of a standalone routine, which is how an A/B
    split that was only ever trained, never saved, becomes one program.
    """
    routine_id = await create_routine(user_id, name, program_id=program_id)
    targets = await workout_exercise_targets(workout_id) if with_targets else {}
    seen: set[int] = set()
    order = 0
    for block in await list_blocks_for_workout(workout_id):
        for be in await get_block_exercises(block["id"]):
            ex_id = be["exercise_id"]
            if ex_id in seen:
                continue
            seen.add(ex_id)
            await add_routine_exercise(routine_id, ex_id, order, targets.get(ex_id))
            order += 1
    return routine_id


# ---------- export ----------

async def export_rows_for_user(user_id: int) -> list[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT w.started_at, e.display_name AS exercise, "
        "s.round_index, s.weight, s.reps, s.rpe "
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


# ---------- admin: AI-trainer cost log (see ai_trainer.py / admin_tasks.py) ----------
#
# One row per real LLM call (chat completion or voice transcription), so the
# daily admin report prices actual token usage instead of a flat per-call
# guess — same pattern as github.com/alexvi88/fun_bot's cost_events.

async def log_cost_event(
    user_id: Optional[int],
    event_type: str,
    *,
    model: Optional[str] = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    async with _write_lock:
        await conn().execute(
            "INSERT INTO cost_events (user_id, event_type, model, prompt_tokens, completion_tokens, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, event_type, model, prompt_tokens, completion_tokens, now_iso()),
        )
        await conn().commit()


async def get_llm_cost_breakdown(date_str: str) -> dict[str, dict[str, int]]:
    """Per-model {calls, prompt_tokens, completion_tokens} for llm_call events on a given calendar day."""
    cur = await conn().execute(
        "SELECT model, COUNT(*), COALESCE(SUM(prompt_tokens), 0), COALESCE(SUM(completion_tokens), 0) "
        "FROM cost_events WHERE event_type = 'llm_call' AND date(created_at) = ? "
        "GROUP BY model",
        (date_str,),
    )
    rows = await cur.fetchall()
    return {
        (model or "unknown"): {"calls": calls, "prompt_tokens": pt, "completion_tokens": ct}
        for model, calls, pt, ct in rows
    }


async def get_transcription_count(date_str: str) -> int:
    """Voice-message transcription calls (config.OPENAI_TRANSCRIBE_MODEL) on a given calendar day."""
    cur = await conn().execute(
        "SELECT COUNT(*) FROM cost_events WHERE event_type = 'transcription' AND date(created_at) = ?",
        (date_str,),
    )
    row = await cur.fetchone()
    return row[0] if row else 0


async def prune_old_cost_events(retention_days: int) -> int:
    """Drop cost_events older than retention_days — only the daily report/backup job reads
    this table, and only ever one day back, so nothing needs it to grow forever."""
    cutoff = (dt.date.today() - dt.timedelta(days=retention_days)).isoformat()
    async with _write_lock:
        cur = await conn().execute("DELETE FROM cost_events WHERE date(created_at) < ?", (cutoff,))
        await conn().commit()
        return cur.rowcount


async def backup_to_file(dest_path: str) -> None:
    """Write a consistent snapshot of the live database to dest_path (must not already exist)."""
    async with _write_lock:
        await conn().execute("VACUUM INTO ?", (dest_path,))


# ---------- push notifications ----------

async def weekly_exercise_rollup(user_id: int, since_date: str) -> list[aiosqlite.Row]:
    """Per exercise since `since_date`: best set, its reps, total tonnage and
    set count — one row per exercise, heaviest tonnage first.

    Feeds the weekly summary table (see formatting.build_weekly_summary), so
    it's one query rather than a walk over every workout of the week.
    """
    cur = await conn().execute(
        "SELECT e.display_name AS name, "
        "       MAX(s.weight) AS top_weight, "
        "       SUM(s.weight * s.reps) AS tonnage, "
        "       COUNT(s.id) AS sets_count "
        "FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "JOIN exercises e ON e.id = s.exercise_id "
        "WHERE w.user_id = ? AND w.status = 'finished' AND w.started_at >= ? AND s.reps > 0 "
        "GROUP BY e.id ORDER BY tonnage DESC",
        (user_id, since_date),
    )
    return await cur.fetchall()


async def tonnage_since(user_id: int, since_date: str) -> float:
    """Total weight x reps across all finished-workout sets on/after since_date — for the weekly digest push."""
    cur = await conn().execute(
        "SELECT COALESCE(SUM(s.weight * s.reps), 0) FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "WHERE w.user_id = ? AND w.status = 'finished' AND w.started_at >= ?",
        (user_id, since_date),
    )
    (total,) = await cur.fetchone()
    return total


async def list_engagement_eligible_user_ids() -> list[tuple[int, int]]:
    """(telegram_id, tz_offset) for users with a finished workout who haven't
    turned pushes off — the daily job's walk pool.

    The offset comes along because the job now runs hourly and sends to each user
    when it's ENGAGEMENT_HOUR *for them*; without it everyone got their "evening"
    push at the server's evening.
    """
    cur = await conn().execute(
        "SELECT DISTINCT w.user_id, u.tz_offset FROM workouts w "
        "JOIN users u ON u.telegram_id = w.user_id "
        "WHERE w.status = 'finished' AND u.pushes_enabled = 1"
    )
    return [(r["user_id"], r["tz_offset"]) for r in await cur.fetchall()]


async def list_newbie_user_ids() -> list[tuple[int, str]]:
    """Users who never finished a workout — the separate walk pool for the newbie nudge.

    Disjoint from `list_engagement_eligible_user_ids` by construction (that one requires
    a finished workout, this one requires the absence of one), so a user is only ever
    walked by one of the two daily loops. Returns `created_at` alongside the id since
    the nudge is timed off signup date, not off a last-workout date these users don't have,
    and `tz_offset` so the send lands at the user's own evening (see the engagement job).
    """
    cur = await conn().execute(
        "SELECT u.telegram_id, u.created_at, u.tz_offset FROM users u "
        "WHERE u.pushes_enabled = 1 AND NOT EXISTS ("
        "SELECT 1 FROM workouts w WHERE w.user_id = u.telegram_id AND w.status = 'finished')"
    )
    return [(r["telegram_id"], r["created_at"], r["tz_offset"]) for r in await cur.fetchall()]


async def get_rotation_bag(telegram_id: int, category: str) -> list[int]:
    cur = await conn().execute(
        "SELECT bag FROM push_rotation WHERE telegram_id = ? AND category = ?",
        (telegram_id, category),
    )
    row = await cur.fetchone()
    if row is None:
        return []
    return json.loads(row["bag"])


async def save_rotation_bag(telegram_id: int, category: str, bag: list[int]) -> None:
    async with _write_lock:
        await conn().execute(
            "INSERT INTO push_rotation (telegram_id, category, bag) VALUES (?, ?, ?) "
            "ON CONFLICT (telegram_id, category) DO UPDATE SET bag = excluded.bag",
            (telegram_id, category, json.dumps(bag)),
        )
        await conn().commit()


# ---------- AI trainer: daily web-search quota ----------

async def get_ai_search_count_today(telegram_id: int) -> int:
    today = dt.date.today().isoformat()
    cur = await conn().execute(
        "SELECT count FROM ai_search_usage WHERE telegram_id = ? AND date = ?",
        (telegram_id, today),
    )
    row = await cur.fetchone()
    return row["count"] if row else 0


async def increment_ai_search_count(telegram_id: int) -> None:
    today = dt.date.today().isoformat()
    async with _write_lock:
        await conn().execute(
            "INSERT INTO ai_search_usage (telegram_id, date, count) VALUES (?, ?, 1) "
            "ON CONFLICT (telegram_id, date) DO UPDATE SET count = count + 1",
            (telegram_id, today),
        )
        await conn().commit()


async def get_ai_question_count_today(telegram_id: int) -> int:
    today = dt.date.today().isoformat()
    cur = await conn().execute(
        "SELECT count FROM ai_question_usage WHERE telegram_id = ? AND date = ?",
        (telegram_id, today),
    )
    row = await cur.fetchone()
    return row["count"] if row else 0


async def increment_ai_question_count(telegram_id: int) -> None:
    today = dt.date.today().isoformat()
    async with _write_lock:
        await conn().execute(
            "INSERT INTO ai_question_usage (telegram_id, date, count) VALUES (?, ?, 1) "
            "ON CONFLICT (telegram_id, date) DO UPDATE SET count = count + 1",
            (telegram_id, today),
        )
        await conn().commit()


# ---------- AI trainer: durable chat history ----------
#
# Separate from the live in-FSM window (handlers/ai_trainer.py's ai_history,
# capped and reset with the conversation) — this is the full, permanent log,
# queryable on demand via the get_full_chat_history tool (ai_trainer.py) when
# a question references something older than what's in the live window.

# Hard cap on a single get_full_chat_history read, not on storage — keeps a
# single tool call bounded regardless of how long the relationship with a
# user has been going.
MAX_AI_CHAT_HISTORY_READ = 300


async def add_ai_chat_message(telegram_id: int, role: str, content: str) -> None:
    async with _write_lock:
        await conn().execute(
            "INSERT INTO ai_chat_messages (telegram_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (telegram_id, role, content, now_iso()),
        )
        await conn().commit()


async def get_ai_chat_history(telegram_id: int, limit: int = MAX_AI_CHAT_HISTORY_READ) -> list[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT role, content, created_at FROM ai_chat_messages WHERE telegram_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (telegram_id, limit),
    )
    rows = await cur.fetchall()
    return list(reversed(rows))


# ---------- bodyweight log ----------

async def add_bodyweight_log(telegram_id: int, weight: float, logged_at: Optional[str] = None) -> int:
    async with _write_lock:
        cur = await conn().execute(
            "INSERT INTO bodyweight_logs (telegram_id, weight, logged_at) VALUES (?, ?, ?)",
            (telegram_id, weight, logged_at or now_iso()),
        )
        await conn().commit()
        return cur.lastrowid


async def list_bodyweight_logs(telegram_id: int, limit: Optional[int] = None) -> list[aiosqlite.Row]:
    """Bodyweight entries oldest-first (for charting). With `limit`, the most recent N, still oldest-first."""
    if limit is None:
        cur = await conn().execute(
            "SELECT * FROM bodyweight_logs WHERE telegram_id = ? ORDER BY logged_at, id",
            (telegram_id,),
        )
        return await cur.fetchall()
    cur = await conn().execute(
        "SELECT * FROM bodyweight_logs WHERE telegram_id = ? ORDER BY logged_at DESC, id DESC LIMIT ?",
        (telegram_id, limit),
    )
    return list(reversed(await cur.fetchall()))


async def get_latest_bodyweight(telegram_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT * FROM bodyweight_logs WHERE telegram_id = ? ORDER BY logged_at DESC, id DESC LIMIT 1",
        (telegram_id,),
    )
    return await cur.fetchone()


async def delete_last_bodyweight(telegram_id: int) -> Optional[aiosqlite.Row]:
    row = await get_latest_bodyweight(telegram_id)
    if row is None:
        return None
    async with _write_lock:
        await conn().execute("DELETE FROM bodyweight_logs WHERE id = ?", (row["id"],))
        await conn().commit()
    return row


async def scale_bodyweight_logs(telegram_id: int, factor: float) -> None:
    """Multiply every stored bodyweight by `factor` — used when a user switches units."""
    async with _write_lock:
        await conn().execute(
            "UPDATE bodyweight_logs SET weight = ROUND(weight * ?, 1) WHERE telegram_id = ?",
            (factor, telegram_id),
        )
        await conn().commit()


# ---------- food diary ----------


async def add_food_entry(
    telegram_id: int,
    eaten_on: str,
    description: str,
    details: Optional[str] = None,
    calories: Optional[float] = None,
    protein: Optional[float] = None,
    fat: Optional[float] = None,
    carbs: Optional[float] = None,
    photo_file_id: Optional[str] = None,
    source: str = "text",
) -> int:
    async with _write_lock:
        cur = await conn().execute(
            "INSERT INTO food_entries (telegram_id, eaten_on, description, details, calories, "
            "protein, fat, carbs, photo_file_id, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                telegram_id, eaten_on, description, details, calories,
                protein, fat, carbs, photo_file_id, source, now_iso(),
            ),
        )
        await conn().commit()
        return cur.lastrowid


async def list_food_entries(telegram_id: int, eaten_on: str) -> list[aiosqlite.Row]:
    """One day's entries, in the order they were added."""
    cur = await conn().execute(
        "SELECT * FROM food_entries WHERE telegram_id = ? AND eaten_on = ? ORDER BY id",
        (telegram_id, eaten_on),
    )
    return await cur.fetchall()


async def list_recent_food_entries(telegram_id: int, since: str, limit: int = 200) -> list[aiosqlite.Row]:
    """Food entries from `since` (YYYY-MM-DD) onward, oldest first.

    For the AI trainer, which reasons over a stretch of days rather than one
    screen's worth: without it the coach advises on nutrition while blind to
    the diary sitting in the same database.
    """
    cur = await conn().execute(
        "SELECT * FROM food_entries WHERE telegram_id = ? AND eaten_on >= ? "
        "ORDER BY eaten_on, id LIMIT ?",
        (telegram_id, since, limit),
    )
    return await cur.fetchall()


async def get_food_entry(entry_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute("SELECT * FROM food_entries WHERE id = ?", (entry_id,))
    return await cur.fetchone()


async def delete_food_entry(entry_id: int) -> None:
    async with _write_lock:
        await conn().execute("DELETE FROM food_entries WHERE id = ?", (entry_id,))
        await conn().commit()


async def list_food_days(
    telegram_id: int, limit: int = 8, offset: int = 0
) -> list[aiosqlite.Row]:
    """Days that have entries, newest first, with per-day totals and the list of
    what was eaten (as newline-joined descriptions) — the history list.

    Aggregated in SQL rather than by loading every entry per day separately —
    the history screen shows a handful of days at a time, but a year of
    logging is thousands of rows. The inner subquery orders by id before the
    GROUP BY so GROUP_CONCAT collects each day's descriptions in the order
    they were logged, not an arbitrary one.
    """
    cur = await conn().execute(
        "SELECT eaten_on, COUNT(*) AS entries, SUM(calories) AS calories, "
        "SUM(protein) AS protein, SUM(fat) AS fat, SUM(carbs) AS carbs, "
        "GROUP_CONCAT(description, char(10)) AS descriptions "
        "FROM (SELECT * FROM food_entries WHERE telegram_id = ? ORDER BY id) "
        "GROUP BY eaten_on ORDER BY eaten_on DESC LIMIT ? OFFSET ?",
        (telegram_id, limit, offset),
    )
    return await cur.fetchall()


async def count_food_days(telegram_id: int) -> int:
    cur = await conn().execute(
        "SELECT COUNT(DISTINCT eaten_on) FROM food_entries WHERE telegram_id = ?",
        (telegram_id,),
    )
    (count,) = await cur.fetchone()
    return count


async def scale_user_set_weights(telegram_id: int, factor: float) -> None:
    """Convert every logged set weight for a user by `factor` (bodyweight 0-sets untouched)."""
    async with _write_lock:
        await conn().execute(
            "UPDATE sets SET weight = ROUND(weight * ?, 1) "
            "WHERE weight != 0 AND block_id IN ("
            "  SELECT b.id FROM workout_blocks b JOIN workouts w ON w.id = b.workout_id "
            "  WHERE w.user_id = ?)",
            (factor, telegram_id),
        )
        await conn().commit()


async def record_push(telegram_id: int, category: str, text: str, sent_on: str) -> None:
    """Log a delivered push. `sent_on` is the recipient's own calendar date
    (YYYY-MM-DD) — see has_push_today."""
    async with _write_lock:
        await conn().execute(
            "INSERT INTO pushes (telegram_id, category, text, sent_at, sent_on) VALUES (?, ?, ?, ?, ?)",
            (telegram_id, category, text, now_iso(), sent_on),
        )
        await conn().commit()


async def has_push_today(telegram_id: int, today: str) -> bool:
    """Whether this user already got a daily-rotation push on this calendar date (YYYY-MM-DD).

    Matches on the stored local date, not on `date(sent_at)`: the caller asks
    in the user's timezone, while sent_at is server time. For a user whose
    19:00 falls after the server's midnight, the previous day's push carried
    the *next* server date — so the check said "already pushed today" and
    silently dropped every other day's push. `COALESCE` covers rows written
    before the column existed.
    """
    cur = await conn().execute(
        "SELECT 1 FROM pushes WHERE telegram_id = ? AND COALESCE(sent_on, date(sent_at)) = ? LIMIT 1",
        (telegram_id, today),
    )
    return await cur.fetchone() is not None


async def count_pushes() -> int:
    cur = await conn().execute("SELECT COUNT(*) FROM pushes")
    (count,) = await cur.fetchone()
    return count


async def list_recent_pushes(limit: int = 20, offset: int = 0) -> list[aiosqlite.Row]:
    """Sent pushes joined with the recipient's username, most recent first."""
    cur = await conn().execute(
        "SELECT p.id, p.telegram_id, p.category, p.text, p.sent_at, u.username "
        "FROM pushes p LEFT JOIN users u ON u.telegram_id = p.telegram_id "
        "ORDER BY p.sent_at DESC, p.id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return await cur.fetchall()
