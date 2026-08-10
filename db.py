"""SQLite data access layer.

Single shared connection guarded by a write lock — a personal-bot's write
volume never justifies a real connection pool, and since aiosqlite already
funnels every statement through one dedicated worker thread, there's never
more than one query in flight regardless of journal mode. Journal mode is
WAL, not the default rollback journal: in DELETE mode every commit creates
and deletes a journal file (two metadata writes) and the writer blocks
readers — on a single connection that means one INSERT stalls every read
until it commits. Measured on a file-backed DB, 200 commits: 3.96ms →
2.29ms (−42%) after switching. WAL needs the filesystem to support
shared-memory mmap for its -wal/-shm files, which mounted persistent-disk
volumes (e.g. Amvera's persistenceMount) can refuse with a sporadic "disk
I/O error" — init_db() below retries the PRAGMA a couple of times and falls
back to the rollback journal rather than crashing startup if the mount
keeps refusing it.
"""

import asyncio
import datetime as dt
import json
import logging
import os
import secrets
import time
from contextlib import suppress
from typing import Any, Optional

import aiosqlite

import config
import formatting
import search_terms
from seed_data import BODYWEIGHT_TEMPLATES, EXERCISE_TEMPLATES, MUSCLE_GROUP_PRESETS

logger = logging.getLogger(__name__)

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
    -- Тренировочный профиль. Ровно те пять вводных, которые AI-тренер и так
    -- спрашивает перед сборкой программы (см. ai_trainer._system_prompt) — но
    -- раньше они жили только в переписке, и на следующий раз он спрашивал их
    -- заново. Всё nullable: профиль заполняется по кусочкам, из разговора или
    -- из настроек, и «не знаю» — нормальный ответ. `equipment` — JSON-список.
    experience TEXT,
    goal TEXT,
    days_per_week INTEGER,
    equipment TEXT,
    limitations TEXT,
    -- Когда тренер в последний раз показывал человеку, что из этого помнит
    -- (см. handlers.ai_trainer._memory_reminder). В FSM этой отметке не место:
    -- /start чистит состояние целиком, кроме трёх AI-ключей, и напоминание
    -- вылезало бы на каждый заход в диалог вместо раза в неделю.
    profile_shown_on TEXT
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
    -- Как в этом движении участвует собственный вес: 'none' (штанга, тренажёр),
    -- 'full' (подтягивания, брусья — вес тела и есть нагрузка, внешний
    -- добавляется) или 'assisted' (гравитрон, резина — внешний вычитается).
    -- `bodyweight_factor` — какая доля веса тела реально поднимается: у
    -- отжиманий от пола это около двух третей, у подтягиваний — всё.
    bodyweight_load TEXT NOT NULL DEFAULT 'none',
    bodyweight_factor REAL NOT NULL DEFAULT 1.0,
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
    routine_id INTEGER,
    program_id INTEGER
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
-- Одно упражнение не может стоять в блоке дважды. Без этого объединение дублей
-- вставляло вторую строку молча (db.merge_exercises), а суперсет с одним и тем
-- же движением в обеих половинах ломает и живой экран, и правку прошлой
-- тренировки. Второй рубеж — сама merge_exercises чистит коллизию до переноса.
CREATE UNIQUE INDEX IF NOT EXISTS idx_block_exercises_unique
    ON block_exercises (block_id, exercise_id);

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
    -- Сколько на самом деле поднято: внешний вес плюс доля собственного (см.
    -- exercises.bodyweight_load). Снимается в момент записи, потому что вес
    -- тела меняется, а подход — уже нет. NULL у строк, записанных до появления
    -- колонки: читатели берут COALESCE(load_weight, weight), поэтому старые
    -- данные считаются ровно как раньше.
    load_weight REAL,
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

-- Разовые релизные рассылки: где сейчас каждая (см. announcements.py).
-- status: preview — анонс показан админу и ждёт добра; approved — добро есть,
-- рассылка идёт или будет продолжена на следующем старте; declined — отклонена.
CREATE TABLE IF NOT EXISTS announcement_state (
    key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- Отпечаток текста, который админ видел на превью. Перепишут текст —
    -- отпечаток разойдётся, и анонс покажется на проверку заново: одобряли
    -- не эту редакцию (см. announcements.run_pending_announcements).
    text_hash TEXT
);

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

-- Поиски всех пользователей за сутки. Отдельная таблица, а не SUM() по
-- ai_search_usage: там date — КАЛЕНДАРНЫЙ ДЕНЬ ПОЛЬЗОВАТЕЛЯ, у разных часовых
-- поясов он разный, и сумма по одной дате смешивала бы куски разных суток.
-- Здесь дата всегда UTC: глобальный потолок про наш счёт от провайдера, а он
-- живёт по UTC, а не по местному времени атлета.
CREATE TABLE IF NOT EXISTS ai_search_global_usage (
    date TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ai_question_usage (
    telegram_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (telegram_id, date)
);

CREATE TABLE IF NOT EXISTS ai_video_usage (
    telegram_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (telegram_id, date)
);

CREATE TABLE IF NOT EXISTS ai_food_usage (
    telegram_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (telegram_id, date)
);

-- «Понятно» на предупреждении о лимите (см. ai_limits.py): свои аккаунты видят
-- потолок первый раз за сутки и дальше в этот день проходят сквозь него. Строка,
-- а не флаг в памяти: перезапуск контейнера не должен показывать одно и то же
-- предупреждение заново — оно тем и ценно, что означает «сегодня это случилось
-- впервые».
CREATE TABLE IF NOT EXISTS ai_limit_ack (
    telegram_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    date TEXT NOT NULL,
    PRIMARY KEY (telegram_id, kind, date)
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
--
-- Not created here: on an upgrade from an old on-disk DB, this executescript
-- runs before _migrate_schema has a chance to ALTER TABLE ADD COLUMN name_key,
-- so an index referencing it here would fail with "no such column" on a table
-- that already exists without it. _migrate_schema creates it after the ALTER.

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
-- Not created here for the same reason idx_programs_user_name isn't (see
-- above): program_id may not exist yet on an old on-disk DB at executescript
-- time. _migrate_schema creates it after ALTER TABLE ADD COLUMN program_id.

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
-- То же этажом выше: день программы не должен содержать одно упражнение дважды.
CREATE UNIQUE INDEX IF NOT EXISTS idx_routine_exercises_unique
    ON routine_exercises (routine_id, exercise_id);

-- Результаты забегов мини-игры «Кач-Раннер» (см. game_server.py): каждая
-- завершённая попытка одной строкой, рекорд — MAX(distance) по пользователю.
CREATE TABLE IF NOT EXISTS game_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    distance INTEGER NOT NULL,
    score INTEGER NOT NULL,
    fighter TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_game_results_user ON game_results (telegram_id, distance);

CREATE TABLE IF NOT EXISTS cost_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    event_type TEXT NOT NULL,
    model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    -- Часть prompt_tokens, приехавшая из кэша (у xAI она в 6-7 раз дешевле
    -- обычного входа), и токены размышлений, которые тарифицируются как выход и
    -- в completion_tokens не входят. Без этих двух колонок дневной отчёт считал
    -- бы иначе, чем строка в логе, — то есть на один вопрос было бы два ответа.
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    -- Кто фактически инициировал платный вызов: 'bot' (обычный экран/чат) или
    -- 'mcp' (внешний MCP-клиент с OAuth-токеном). Без этого расход от чужого
    -- клиента неотличим в /growth и ночном отчёте от обычной активности бота.
    source TEXT NOT NULL DEFAULT 'bot',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cost_events_created ON cost_events (created_at);

-- Оплаты звёздами (XTR), по строке на успешный платёж. Полный лог, а не
-- состояние: состояние живёт в user_billing и пересчитывается, а здесь — чем
-- именно человек заплатил и когда.
--
-- charge_id — telegram_payment_charge_id, он же ключ идемпотентности: Telegram
-- умеет доставить successful_payment повторно (ретрай апдейта после обрыва), и
-- без UNIQUE один платёж выдал бы доступ дважды. Он же — то, что надо назвать
-- Telegram при возврате звёзд (refund_star_payment).
--
-- refunded_at ставится при возврате и не удаляет строку: вернувшийся платёж —
-- такой же факт истории, как и состоявшийся, и в выручке он должен переставать
-- считаться, а не исчезать.
CREATE TABLE IF NOT EXISTS star_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    charge_id TEXT NOT NULL UNIQUE,
    product TEXT NOT NULL,
    stars INTEGER NOT NULL,
    payload TEXT NOT NULL,
    refunded_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_star_payments_user ON star_payments (telegram_id, id);
CREATE INDEX IF NOT EXISTS idx_star_payments_created ON star_payments (created_at);

-- Что у человека сейчас оплачено: до какой даты действует доступ и сколько
-- разовых вопросов осталось. Строка заводится первой покупкой — у тех, кто не
-- платил, её просто нет, и это отличается от «есть строка с нулями» только
-- размером таблицы.
--
-- pro_until — UTC ISO. Продление считается от максимума из «сейчас» и текущей
-- даты окончания: купить второй месяц, не дождавшись конца первого, должно
-- добавлять срок, а не обнулять остаток.
CREATE TABLE IF NOT EXISTS user_billing (
    telegram_id INTEGER PRIMARY KEY,
    pro_until TEXT,
    pack_questions INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- Сырой лог того, что человек делает в боте: каждое входящее сообщение и каждое
-- нажатие кнопки, одной строкой (см. activity_log.py). Всё остальное, что видит
-- админ — история, пуши, диалоги с AI — это результат: что записалось, что
-- отправилось. По результату не видно ни того, как человек к нему шёл, ни того,
-- что он написал впустую и бот не понял. Отсюда и таблица: она про путь, а не
-- про итог.
--
-- content — то, что реально введено: текст сообщения, подпись к фото или
-- человекочитаемая надпись нажатой кнопки; подрезается на входе
-- (activity_log.MAX_CONTENT_LEN), чтобы простыня из буфера обмена не раздувала
-- базу. payload у кнопок — её callback_data, то есть чем нажатие было для бота.
-- Живёт не вечно: prune_old_user_events чистит по ACTIVITY_RETENTION_DAYS в том
-- же суточном джобе, что и cost_events.
CREATE TABLE IF NOT EXISTS user_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_events_user ON user_events (telegram_id, id);
CREATE INDEX IF NOT EXISTS idx_user_events_created ON user_events (created_at);

CREATE TABLE IF NOT EXISTS achievements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    earned_at TEXT NOT NULL,
    UNIQUE (user_id, code)
);
CREATE INDEX IF NOT EXISTS idx_achievements_user ON achievements (user_id);

-- Токен доступа к своим данным по MCP (см. mcp_server.py): пользователь
-- вставляет его в конфиг внешнего AI-клиента, и тот читает историю тренировок
-- напрямую. Ровно один живой токен на человека — «перевыпустить» удаляет
-- старый, так что отозвать доступ у клиента, который больше не нужен, можно
-- всегда и одним действием.
--
-- last_used_at существует только ради экрана в боте: «последний раз читали
-- тогда-то» — единственный способ заметить, что токеном пользуется кто-то ещё.
CREATE TABLE IF NOT EXISTS mcp_tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);

-- OAuth к тому же доступу (см. mcp_oauth.py). Статический токен выше умеют
-- слать только клиенты, где заголовок можно вписать руками; браузерный
-- claude.ai, нативные коннекторы Claude Desktop и ChatGPT принимают
-- исключительно OAuth — а это ровно те клиенты, где человек не настраивает
-- ничего. Механизмы независимы: отзыв одного не трогает другой.

-- Клиенты динамической регистрации (RFC 7591). Регистрируется приложение
-- (Claude, ChatGPT), а не человек, поэтому user_id тут нет.
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_secret TEXT,
    metadata TEXT NOT NULL,      -- OAuthClientInformationFull в JSON
    created_at TEXT NOT NULL
);

-- Заявка на согласие: /authorize пришёл, человек ещё не подтвердил, что это он.
-- Лежит в базе, а не в памяти процесса: контейнер перезапускается когда угодно,
-- и перезапуск посреди подключения не должен выглядеть как «ничего не работает».
--
-- attempts здесь, а не на коде связывания: у неверного кода строки в базе нет,
-- запирать по нему нечего — перебирают шесть цифр в рамках одной заявки, её и
-- запираем.
CREATE TABLE IF NOT EXISTS oauth_consent_requests (
    request_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    redirect_uri_provided_explicitly INTEGER NOT NULL,
    code_challenge TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT,
    state TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL
);

-- Код авторизации: живёт минуты, гасится при обмене.
CREATE TABLE IF NOT EXISTS oauth_auth_codes (
    code TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    redirect_uri TEXT NOT NULL,
    redirect_uri_provided_explicitly INTEGER NOT NULL,
    code_challenge TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oauth_auth_codes_expires ON oauth_auth_codes (expires_at);

-- Выданные токены. refresh_token в той же строке: отзыв одного обязан гасить
-- парный, а держать их порознь — это способ про это забыть. Два срока на строку,
-- потому что живут они по-разному: access — час, refresh — недели, и строка
-- нужна, пока жив хотя бы refresh.
-- connected_at — когда человек подтвердил доступ, а не когда выдана эта пара:
-- обновление токена каждый час переписывало бы created_at, и экран «Подключённые
-- приложения» показывал бы «подключено сегодня» тому, кто подключился месяц назад.
CREATE TABLE IF NOT EXISTS oauth_tokens (
    access_token TEXT PRIMARY KEY,
    refresh_token TEXT UNIQUE,
    client_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT,
    expires_at REAL NOT NULL,
    refresh_expires_at REAL,
    created_at TEXT NOT NULL,
    connected_at TEXT,
    last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_oauth_tokens_user ON oauth_tokens (user_id);

-- Одноразовый код из бота, которым человек доказывает на странице согласия, что
-- это он. Единственный аккаунт у пользователя — телеграмный, и подтвердить
-- владение им можно только через сам бот.
CREATE TABLE IF NOT EXISTS oauth_link_codes (
    code TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at REAL NOT NULL
);

-- Неудачные попытки ввода кода связывания — по одной строке на попытку.
--
-- Это единственное, что ограничивает перебор. Счётчик на заявке (attempts выше)
-- не ограничивает ничего: заявка создаётся бесплатным GET /authorize, и пять
-- попыток — цена одной заявки, а не механизма. Шесть цифр — двадцать бит, и без
-- этой таблицы их перебирают за время жизни кода.
--
-- Скользящее окно, а не вечный счётчик: человек, который трижды ошибся,
-- не должен остаться запертым навсегда.
CREATE TABLE IF NOT EXISTS oauth_consent_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    client_ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_oauth_failures_at ON oauth_consent_failures (at);
"""

_conn: Optional[aiosqlite.Connection] = None
_write_lock = asyncio.Lock()


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ---------- сутки по часам пользователя ----------
#
# started_at/created_at пишутся по часам сервера, а сервер живёт по UTC. Экраны
# же считают «сегодня» местным временем пользователя (users.tz_offset, см.
# timeutil.user_today). Пока агрегаты резали день через date(started_at), эти две
# системы расходились на тренировках около полуночи, и расхождение было видно
# пользователю: тренировка, закрытая в 23:40 западнее UTC, попадала в UTC-«завтра»
# — окно «за 30 дней», посчитанное от местного «сегодня», её не ловило («0
# тренировок за 30 дней» сразу после тренировки), а «дней с последней» уходило в
# минус. Поэтому день режется сдвинутой меткой: у человека на UTC+3 это
# date(started_at, '+3 hours') — метку двигают к местному времени, а не от него.
#
# Смещение подставляется в SQL текстом, а не параметром: тот же кусок выражения
# идёт в SELECT, GROUP BY и оконную функцию, и три одинаковых плейсхолдера в
# нужном порядке — источник ошибок. int() гарантирует, что в SQL уходит число.


def _local_day(column: str, tz_offset: int) -> str:
    """SQL-выражение «календарный день по часам пользователя» для UTC-столбца."""
    return f"date({column}, '{int(tz_offset):+d} hours')"


async def user_tz_offset(user_id: int) -> int:
    """Смещение пользователя в целых часах; 0, если пользователя или значения нет."""
    cur = await conn().execute(
        "SELECT tz_offset FROM users WHERE telegram_id = ?", (user_id,)
    )
    row = await cur.fetchone()
    try:
        return int(row["tz_offset"])
    except (TypeError, KeyError, IndexError, ValueError):
        return 0


async def _tz_offset_of(user_id: int, tz_offset: Optional[int]) -> int:
    """Смещение для запроса: переданное вызывающей стороной или прочитанное из БД.

    Агрегаты ниже принимают tz_offset необязательным параметром, и None значит
    «возьми из users». Так вызовы, у которых строки пользователя под рукой нет
    (их большинство), остаются корректными без изменений, а тот, кто уже держит
    строку, может передать смещение и сэкономить запрос.
    """
    return await user_tz_offset(user_id) if tz_offset is None else int(tz_offset)


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


async def _enable_wal_with_fallback() -> None:
    """PRAGMA journal_mode=WAL, retried a couple of times against a mounted
    volume's occasional "disk I/O error" (see module docstring) before giving
    up and staying on the default rollback journal — a slower DB beats one
    that refuses to start at all.
    """
    for attempt in range(3):
        try:
            await _conn.execute("PRAGMA journal_mode=WAL")
            return
        except aiosqlite.Error:
            if attempt == 2:
                logger.exception(
                    "PRAGMA journal_mode=WAL failed after retries; staying on the "
                    "default rollback journal (slower, but the DB still starts)"
                )
                return
            await asyncio.sleep(0.2 * (attempt + 1))


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
    # Тот же LOWER плюс ё→е: «жим лежа» и «жим лёжа» — один запрос, а в каталоге
    # встречаются оба написания.
    await _conn.create_function(
        "py_fold", 1, lambda s: search_terms.fold(s) if s is not None else None
    )
    await _enable_wal_with_fallback()
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


async def _dedupe_exercise_links() -> None:
    """Свернуть дубли «одно упражнение дважды в одном блоке/дне» перед тем, как
    ставить на них UNIQUE.

    Их источник — db.merge_exercises, которая переносила ссылки без проверки
    коллизии; на живых базах такие строки уже могли накопиться, и
    CREATE UNIQUE INDEX на них просто не встал бы. Оставляем самую раннюю
    строку (у неё меньший order — то есть позиция, которую человек и видел).
    """
    for table, scope in (("block_exercises", "block_id"), ("routine_exercises", "routine_id")):
        await _conn.execute(
            f"DELETE FROM {table} WHERE id NOT IN "
            f"(SELECT MIN(id) FROM {table} GROUP BY {scope}, exercise_id)"
        )


async def _migrate_schema() -> None:
    """Upgrade older on-disk databases to the current column set in-place."""
    await _conn.execute("DROP INDEX IF EXISTS idx_exercises_user_name")

    cost_cols = await _column_names("cost_events")
    if "cached_tokens" not in cost_cols:
        await _conn.execute("ALTER TABLE cost_events ADD COLUMN cached_tokens INTEGER NOT NULL DEFAULT 0")
    if "reasoning_tokens" not in cost_cols:
        await _conn.execute("ALTER TABLE cost_events ADD COLUMN reasoning_tokens INTEGER NOT NULL DEFAULT 0")
    if "source" not in cost_cols:
        await _conn.execute("ALTER TABLE cost_events ADD COLUMN source TEXT NOT NULL DEFAULT 'bot'")

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
    if "program_id" not in workout_cols:
        # находка 22: routine_id alone loses the program once its day is
        # deleted (delete_routine drops the routines row, not the workouts
        # that pointed at it) — the "N тренировок по ней" total under a
        # program's name would silently shrink even though the workouts
        # themselves stay in history exactly as the delete confirmation
        # promises. A denormalized program_id survives that: it's set at
        # workout creation, from the routine picked, and outlives the day.
        await _conn.execute("ALTER TABLE workouts ADD COLUMN program_id INTEGER")
        await _conn.execute(
            "UPDATE workouts SET program_id = "
            "(SELECT program_id FROM routines WHERE routines.id = workouts.routine_id) "
            "WHERE routine_id IS NOT NULL"
        )
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

    if "bodyweight_load" not in exercise_cols:
        await _conn.execute(
            "ALTER TABLE exercises ADD COLUMN bodyweight_load TEXT NOT NULL DEFAULT 'none'"
        )
        await _conn.execute(
            "ALTER TABLE exercises ADD COLUMN bodyweight_factor REAL NOT NULL DEFAULT 1.0"
        )

    set_cols = await _column_names("sets")
    if "load_weight" not in set_cols:
        await _conn.execute("ALTER TABLE sets ADD COLUMN load_weight REAL")

    # Таблица завелась вместе с OAuth и уже могла уехать на прод без этой колонки:
    # первая версия писала только created_at, который обновление токена переписывало.
    oauth_token_cols = await _column_names("oauth_tokens")
    if "connected_at" not in oauth_token_cols:
        await _conn.execute("ALTER TABLE oauth_tokens ADD COLUMN connected_at TEXT")
        # У уже выданных пар дата подключения неизвестна: created_at — лучшее, что
        # есть, и для строк, которые ещё не обновлялись, он и есть верный ответ.
        await _conn.execute(
            "UPDATE oauth_tokens SET connected_at = created_at WHERE connected_at IS NULL"
        )

    # Разовые рассылки жили без отпечатка текста: превью показывали один раз, а
    # переписанный после этого анонс уходил бы людям, не побывав на проверке.
    announcement_cols = await _column_names("announcement_state")
    if announcement_cols and "text_hash" not in announcement_cols:
        await _conn.execute("ALTER TABLE announcement_state ADD COLUMN text_hash TEXT")

    shared_cols = await _column_names("shared_items")
    if "taken_count" not in shared_cols:
        await _conn.execute(
            "ALTER TABLE shared_items ADD COLUMN taken_count INTEGER NOT NULL DEFAULT 0"
        )

    user_cols = await _column_names("users")
    for profile_col, col_type in (
        ("experience", "TEXT"), ("goal", "TEXT"), ("days_per_week", "INTEGER"),
        ("equipment", "TEXT"), ("limitations", "TEXT"),
        # Дата последнего напоминания «что я про тебя помню» — см. схему выше.
        ("profile_shown_on", "TEXT"),
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
    if "stickers_enabled" in user_cols:
        # Стикеры-реакции выпилены: их никто не отправлял, а тумблер в настройках
        # обещал их пользователю (см. коммит). Колонку убираем тем же способом,
        # что hide_warmups выше.
        await _conn.execute("ALTER TABLE users DROP COLUMN stickers_enabled")
    if "rank_level_seen" not in user_cols:
        # Последнее объявленное звание. Нужно, чтобы «🎖 Новое звание» показывалось
        # ровно один раз: само звание считается на лету из тренировок и тоннажа,
        # так что без этой отметки карточка объявляла бы его каждый раз.
        await _conn.execute("ALTER TABLE users ADD COLUMN rank_level_seen INTEGER NOT NULL DEFAULT -1")
    if "source" not in user_cols:
        # Откуда человек пришёл в бота: метка из deep link'а на первом /start
        # (см. acquisition.py). NULL значит «ещё не размечен» — на этом держится
        # правило первого касания в set_user_source.
        await _conn.execute("ALTER TABLE users ADD COLUMN source TEXT")
        await _conn.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER")
        # Все, кто зарегистрировался до этой миграции, пришли неизвестно откуда,
        # и узнать это уже нечем. Метим их отдельным источником, а не оставляем
        # NULL: иначе первый же их /start после деплоя записал бы им сегодняшнюю
        # метку, и месячная воронка показала бы толпу «новых» из канала, который
        # их не приводил. Из отчётов эта метка исключена целиком.
        await _conn.execute(
            "UPDATE users SET source = 'legacy' WHERE source IS NULL"
        )
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

    await _dedupe_exercise_links()

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
        async with _write_lock:
            for ex_name, (load, factor) in BODYWEIGHT_TEMPLATES.items():
                await db.execute(
                    "UPDATE exercises SET bodyweight_load = ?, bodyweight_factor = ? "
                    "WHERE is_template = 1 AND user_id IS NULL AND name = ? "
                    "AND (bodyweight_load != ? OR bodyweight_factor != ?)",
                    (load, factor, ex_name, load, factor),
                )
            await db.commit()
        return

    async with _write_lock:
        for ex_id in to_delete:
            await db.execute("DELETE FROM exercises WHERE id = ?", (ex_id,))
        for group_id, ex_name in to_insert:
            display_name = build_display_name(ex_name)
            load, factor = BODYWEIGHT_TEMPLATES.get(ex_name, ("none", 1.0))
            await db.execute(
                "INSERT INTO exercises "
                "(user_id, name, primary_group_id, display_name, original_name, is_template, "
                " bodyweight_load, bodyweight_factor, created_at) "
                "VALUES (NULL, ?, ?, ?, ?, 1, ?, ?, ?)",
                (ex_name, group_id, display_name, ex_name, load, factor, now_iso()),
            )
        # Режим нагрузки правится и у шаблонов, которые уже лежат в базе: список
        # BODYWEIGHT_TEMPLATES может пополниться, а шаблоны пересоздаются только
        # когда меняется их имя или группа.
        for ex_name, (load, factor) in BODYWEIGHT_TEMPLATES.items():
            await db.execute(
                "UPDATE exercises SET bodyweight_load = ?, bodyweight_factor = ? "
                "WHERE is_template = 1 AND user_id IS NULL AND name = ?",
                (load, factor, ex_name),
            )
        await db.commit()


# Bumped whenever a one-shot migration is added to _run_one_shot_migrations.
_SCHEMA_VERSION = 5


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
    if version < 5:
        await _backfill_bodyweight_load()
    # Not parameterizable — SQLite only accepts a literal here. The value is an
    # internal constant, never user input.
    await _conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    await _conn.commit()


async def _backfill_bodyweight_load() -> None:
    """Проставить снимок нагрузки историческим подходам с собственным весом.

    До этой миграции подтягивания лежали как «0×12»: нулевой тоннаж, нулевой
    e1RM, плоский график у человека, который за полгода дошёл с пяти повторов до
    двенадцати. Вес тела бот всё это время знал — `bodyweight_logs`, — просто
    ни во что его не включал.

    Берём взвешивание, ближайшее к дате подхода и не позже неё (bodyweight_at):
    подход из марта не должен пересчитываться от июньского веса. У кого
    взвешиваний нет вовсе, строки остаются с NULL и считаются как раньше —
    выдумывать массу человека мы не будем.

    One-shot: дальше снимок ставится при записи подхода (_load_weight_for).
    """
    cur = await _conn.execute(
        "SELECT s.id, s.weight, s.created_at, e.user_id, "
        "       e.bodyweight_load, e.bodyweight_factor "
        "FROM sets s JOIN exercises e ON e.id = s.exercise_id "
        "WHERE s.load_weight IS NULL AND e.bodyweight_load != 'none' AND e.user_id IS NOT NULL"
    )
    rows = await cur.fetchall()
    if not rows:
        return
    # Взвешивания читаются пачкой на пользователя: у активного аккаунта таких
    # подходов тысячи, и запрос на каждый — это минуты на старте бота.
    logs: dict[int, list[tuple[str, float]]] = {}
    for row in rows:
        if row["user_id"] not in logs:
            log_cur = await _conn.execute(
                "SELECT logged_at, weight FROM bodyweight_logs WHERE telegram_id = ? "
                "ORDER BY logged_at, id",
                (row["user_id"],),
            )
            logs[row["user_id"]] = [(r["logged_at"], r["weight"]) for r in await log_cur.fetchall()]

    for row in rows:
        entries = logs.get(row["user_id"]) or []
        if not entries:
            continue
        earlier = [w for at, w in entries if at <= (row["created_at"] or "")]
        bodyweight = earlier[-1] if earlier else entries[0][1]
        await _conn.execute(
            "UPDATE sets SET load_weight = ? WHERE id = ?",
            (
                effective_load(
                    row["weight"], bodyweight, row["bodyweight_load"], row["bodyweight_factor"]
                ),
                row["id"],
            ),
        )
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


async def set_user_source(
    telegram_id: int, source: str, referrer_id: Optional[int] = None
) -> bool:
    """Записать, откуда человек пришёл — но только если он ещё не размечен.

    Первое касание побеждает: `WHERE source IS NULL` в самом UPDATE, а не
    проверка в вызывающем коде, потому что два /start подряд (человек нажал
    Start дважды, пока бот думал) — обычное дело, и второй не должен переписать
    источник первого. Возвращает True, если метка легла.
    """
    async with _write_lock:
        cur = await conn().execute(
            "UPDATE users SET source = ?, referrer_id = ? "
            "WHERE telegram_id = ? AND source IS NULL",
            (source, referrer_id, telegram_id),
        )
        await conn().commit()
    return cur.rowcount > 0


# Подзапрос «сколько тренировок человек закрыл и когда последнюю» — общий для
# воронки и топа пригласивших.
_FINISHED_BY_USER = (
    "SELECT user_id, COUNT(*) AS finished, MAX(started_at) AS last_finished_at "
    "FROM workouts WHERE status = 'finished' GROUP BY user_id"
)


async def acquisition_funnel(days: int = 30, alive_days: int = 7) -> list[aiosqlite.Row]:
    """По источникам: сколько пришло, сколько записало первую тренировку, сколько живо.

    Окно считается по дате регистрации, а не по дате тренировок: канал отвечает
    за тех, кого привёл, даже если тренируются они месяцем позже. `legacy`
    (пришедшие до появления атрибуции) в отчёт не попадает — см. _migrate_schema.
    """
    since = (dt.datetime.now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
    alive_since = (dt.datetime.now() - dt.timedelta(days=alive_days)).isoformat(timespec="seconds")
    cur = await conn().execute(
        "SELECT COALESCE(u.source, 'unknown') AS source, "
        "COUNT(*) AS users, "
        "COUNT(*) FILTER (WHERE f.finished >= 1) AS activated, "
        "COUNT(*) FILTER (WHERE f.finished >= 3) AS engaged, "
        "COUNT(*) FILTER (WHERE f.last_finished_at >= ?) AS alive "
        f"FROM users u LEFT JOIN ({_FINISHED_BY_USER}) f ON f.user_id = u.telegram_id "
        "WHERE u.created_at >= ? AND COALESCE(u.source, 'unknown') <> 'legacy' "
        "GROUP BY source ORDER BY users DESC, source",
        (alive_since, since),
    )
    return await cur.fetchall()


async def top_referrers(limit: int = 10) -> list[aiosqlite.Row]:
    """Кто привёл больше всех — и сколько из приведённых дошли до первой тренировки."""
    cur = await conn().execute(
        "SELECT u.referrer_id AS referrer_id, r.username AS username, "
        "COUNT(*) AS invited, "
        "COUNT(*) FILTER (WHERE f.finished >= 1) AS activated "
        "FROM users u "
        "LEFT JOIN users r ON r.telegram_id = u.referrer_id "
        f"LEFT JOIN ({_FINISHED_BY_USER}) f ON f.user_id = u.telegram_id "
        "WHERE u.referrer_id IS NOT NULL "
        "GROUP BY u.referrer_id ORDER BY invited DESC, activated DESC LIMIT ?",
        (limit,),
    )
    return await cur.fetchall()


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


# Точное совпадение → начинается с запроса → содержит его где-то внутри. Без
# этого «жим» отдавал алфавит («Жим Арнольда», «Жим в тренажёре»…), и то, что
# человек искал на самом деле, оказывалось в хвосте — а хвост срезался лимитом.
_RELEVANCE_RANK = (
    "CASE WHEN py_fold({col}) = py_fold(?) THEN 0 "
    "     WHEN py_fold({col}) LIKE py_fold(?) || '%' ESCAPE '\\' THEN 1 "
    "     ELSE 2 END"
)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _stem_filter(col: str, query: str) -> tuple[str, list[str]]:
    """Условие «в названии есть основы всех слов запроса» и параметры к нему.

    По слову, а не одной подстрокой целиком: «жим лёжа» должен находить «Жим
    штанги лёжа», где между словами запроса стоит третье. По основе, а не по
    слову: «приседания» должны находить «Присед» (см. search_terms).
    """
    stems = search_terms.query_stems(query)
    if not stems:
        # Пустой запрос — не повод показать весь каталог.
        return "0", []
    clause = " AND ".join(f"py_fold({col}) LIKE '%' || ? || '%' ESCAPE '\\'" for _ in stems)
    return clause, [_escape_like(s) for s in stems]


async def search_exercises(user_id: int, query: str, limit: int = 20) -> list[aiosqlite.Row]:
    escaped = _escape_like(query)
    rank = _RELEVANCE_RANK.format(col="e.display_name")
    match, match_params = _stem_filter("e.display_name", query)
    cur = await conn().execute(
        "SELECT * FROM exercises e WHERE e.user_id = ? AND e.is_archived = 0 AND e.is_template = 0 "
        f"AND {match} "
        f"AND {_VISIBLE_EXERCISE_FILTER} "
        f"ORDER BY {rank}, e.last_used_at IS NULL, e.last_used_at DESC, py_fold(e.display_name) "
        "LIMIT ?",
        (user_id, *match_params, escaped, escaped, limit),
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
    escaped = _escape_like(query)
    rank = _RELEVANCE_RANK.format(col="t.display_name")
    match, match_params = _stem_filter("t.display_name", query)
    cur = await conn().execute(
        "SELECT * FROM exercises t WHERE t.is_template = 1 "
        f"AND {match} "
        "AND NOT EXISTS ("
        "   SELECT 1 FROM exercises o WHERE o.user_id = ? AND o.is_template = 0 "
        "   AND o.is_archived = 0 AND py_lower(o.display_name) = py_lower(t.display_name)"
        ") "
        # py_fold и здесь: бинарная коллация ставила «Жим в тренажёре Хаммер»
        # раньше «Жим в тренажёре на плечи» — заглавная Х меньше строчной н.
        f"ORDER BY {rank}, py_fold(t.display_name) "
        "LIMIT ?",
        (*match_params, user_id, escaped, escaped, limit),
    )
    return await cur.fetchall()


async def get_exercise(exercise_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute("SELECT * FROM exercises WHERE id = ?", (exercise_id,))
    return await cur.fetchone()


def _fold_exercise_name(value: str) -> str:
    """Нормализация имени для сравнения: регистр и ё→е.

    .lower() букву «ё» не сворачивает, а люди (и модель) пишут «Жим лёжа» и
    «Жим лежа» вперемешку — без этой замены совпадающее по сути имя уходило в
    unresolved и стоило AI-тренеру лишнего раунда уточнений.
    """
    return value.strip().lower().replace("ё", "е")


async def find_exercise_by_name(user_id: int, name: str) -> Optional[aiosqlite.Row]:
    """Exact case-insensitive match on the bare name or full display name (Cyrillic-safe, ё=е)."""
    cur = await conn().execute(
        "SELECT * FROM exercises WHERE user_id = ? AND is_archived = 0 AND is_template = 0", (user_id,)
    )
    rows = await cur.fetchall()
    needle = _fold_exercise_name(name)
    for r in rows:
        if _fold_exercise_name(r["name"]) == needle or _fold_exercise_name(r["display_name"]) == needle:
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
    ex_id = await create_exercise(
        user_id,
        template["name"],
        template["primary_group_id"],
        final_equipment,
        final_unilateral,
        final_attachment,
    )
    # Режим нагрузки — свойство движения, а не каталога: без переноса форк
    # подтягиваний считался бы с нулевым весом, как и до всего этого.
    async with _write_lock:
        await conn().execute(
            "UPDATE exercises SET bodyweight_load = ?, bodyweight_factor = ? WHERE id = ?",
            (template["bodyweight_load"], template["bodyweight_factor"], ex_id),
        )
        await conn().commit()
    return ex_id


async def update_exercise_name(exercise_id: int, name: str) -> bool:
    """Rename in place (same row/id) so existing sets keep their stats. Returns False on name clash.

    The clash check is the same `py_lower` lookup `create_exercise` does, and
    for the same reason: the unique index behind it folds case with SQL
    `LOWER()`, which is ASCII-only, so on a Cyrillic name it never fires. Left
    to the index alone, renaming «Разводка» to «ЖИМ ЛЁЖА» happily sat down next
    to an existing «Жим лёжа» — two rows for one lift, each accumulating its own
    history, while every name-based resolver (programs, share import, CSV, the
    AI trainer) silently picked whichever was older.
    """
    ex = await get_exercise(exercise_id)
    if ex is None:
        return False
    display_name = build_display_name(name, ex["equipment"], bool(ex["unilateral"]), ex["attachment"])
    clash = await find_exercise_by_display_name(ex["user_id"], display_name)
    # Renaming to its own name (or just changing its case) is not a clash.
    if clash is not None and clash["id"] != exercise_id:
        return False
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


# Почему объединение возвращает не bool, а причину: у отказа их несколько, и
# «не получилось» вместо «это упражнение сейчас в открытой тренировке» —
# ровно тот ответ, после которого человек жмёт кнопку ещё раз.
MERGE_OK = "ok"
MERGE_INVALID = "invalid"            # не своё, шаблон, одна и та же строка
MERGE_TARGET_ARCHIVED = "archived"   # цель в архиве — история уехала бы туда же
MERGE_IN_ACTIVE_WORKOUT = "active"   # одна из сторон открыта в текущей тренировке


async def _exercise_in_active_workout(user_id: int, exercise_id: int) -> bool:
    """Открыто ли упражнение в текущей тренировке.

    Смотрим на `block_exercises`, а не на записанные подходы: таб можно открыть
    и ещё ничего в него не записать, и именно такой — пустой, но открытый —
    самый опасный, FSM уже держит его id.
    """
    active = await get_active_workout(user_id)
    if active is None:
        return False
    cur = await conn().execute(
        "SELECT 1 FROM block_exercises be JOIN workout_blocks b ON b.id = be.block_id "
        "WHERE b.workout_id = ? AND be.exercise_id = ? LIMIT 1",
        (active["id"], exercise_id),
    )
    return await cur.fetchone() is not None


async def merge_exercises(user_id: int, keep_id: int, drop_id: int) -> str:
    """Merges drop_id into keep_id — for when the same movement got logged
    under two different names/entries (e.g. typed once as "ягодичный мостик",
    later as "glute bridge") and the user wants one combined history instead
    of two split ones.

    Repoints every set, workout block and routine slot from drop_id to
    keep_id, carries over drop's description/photo/notes if keep has none of
    its own, and then removes drop_id entirely. Returns one of the MERGE_*
    constants.

    Every table that references `exercise_id` gets an explicit answer to "what
    if both rows are already here", because only one of them (exercise_notes,
    with its composite primary key) would have complained on its own — and the
    silent ones left the survivor listed twice in a routine day and inside a
    superset block.
    """
    if keep_id == drop_id:
        return MERGE_INVALID
    keep = await get_exercise(keep_id)
    drop = await get_exercise(drop_id)
    if keep is None or drop is None:
        return MERGE_INVALID
    if keep["user_id"] != user_id or drop["user_id"] != user_id:
        return MERGE_INVALID
    if keep["is_template"] or drop["is_template"]:
        return MERGE_INVALID
    # Цель в архиве: перенести туда всю историю значит убрать её из каждого
    # списка сразу, а искать в «🗄 Архив» человек не пойдёт — он не знает, что
    # её туда унесло. Цель могла быть заархивирована уже после того, как
    # клавиатура с кнопкой отрисовалась.
    if keep["is_archived"]:
        return MERGE_TARGET_ARCHIVED
    # Открытая тренировка держит id упражнения в FSM (open_exercises/open_blocks);
    # удалить строку из-под неё значит оставить экран записи подхода указывающим
    # в никуда.
    if await _exercise_in_active_workout(user_id, keep_id) or await _exercise_in_active_workout(
        user_id, drop_id
    ):
        return MERGE_IN_ACTIVE_WORKOUT

    async with _write_lock:
        await conn().execute(
            "UPDATE sets SET exercise_id = ? WHERE exercise_id = ?", (keep_id, drop_id)
        )
        # block_exercises: у одного блока (суперсета) могли быть оба упражнения.
        # UNIQUE на (block_id, exercise_id) нет, так что БД пропустила бы дубль
        # молча — блок с одной и той же строкой дважды ломает и экран живой
        # сессии, и редактирование прошлой тренировки.
        await conn().execute(
            "DELETE FROM block_exercises WHERE exercise_id = ? AND block_id IN "
            "(SELECT block_id FROM block_exercises WHERE exercise_id = ?)",
            (drop_id, keep_id),
        )
        await conn().execute(
            "UPDATE block_exercises SET exercise_id = ? WHERE exercise_id = ?", (keep_id, drop_id)
        )
        # routine_exercises: то же самое днём программы. Схему и правило
        # прогрессии подбираем со стороны, которую убираем, если у оставшейся
        # их нет, — иначе объединение молча обнуляло бы «4×8».
        await conn().execute(
            "UPDATE routine_exercises SET "
            "  target = COALESCE(target, (SELECT d.target FROM routine_exercises d "
            "    WHERE d.exercise_id = ? AND d.routine_id = routine_exercises.routine_id)), "
            "  progression = COALESCE(progression, (SELECT d.progression FROM routine_exercises d "
            "    WHERE d.exercise_id = ? AND d.routine_id = routine_exercises.routine_id)) "
            "WHERE exercise_id = ? AND routine_id IN "
            "  (SELECT routine_id FROM routine_exercises WHERE exercise_id = ?)",
            (drop_id, drop_id, keep_id, drop_id),
        )
        await conn().execute(
            "DELETE FROM routine_exercises WHERE exercise_id = ? AND routine_id IN "
            "(SELECT routine_id FROM routine_exercises WHERE exercise_id = ?)",
            (drop_id, keep_id),
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
    return MERGE_OK


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
    у обычной тренировки «с нуля» его нет.

    `program_id` дублируется с routine_id.program_id на момент старта — не
    просто денормализация: если день потом удалят (delete_routine сносит саму
    строку routines), routine_id повиснет без пары, а program_id останется и
    даст программе честно посчитать «N тренировок по ней» даже по дням,
    которых больше нет (находка 22)."""
    program_id = None
    if routine_id is not None:
        routine = await get_routine(routine_id)
        program_id = routine["program_id"] if routine else None
    async with _write_lock:
        cur = await conn().execute(
            "INSERT INTO workouts (user_id, started_at, status, routine_id, program_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, started_at or now_iso(), status, routine_id, program_id),
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


async def shift_workout_set_timestamps(workout_id: int, seconds: float) -> None:
    """Сдвинуть метки времени подходов тренировки на столько же, на сколько
    сдвинули её саму.

    Длительность на карточке считается по разбегу меток подходов, а не по
    started/finished (находка 25: правка через пару часов после тренировки
    читалась как двухчасовая сессия). Из-за этого перенос тренировки на другой
    день молча стирал длительность: метки оставались на старом дне, фильтр
    «не позже finished_at» отсекал их все разом, и разбег становился пустым.
    Сдвигаем вместе — тогда и сессия остаётся целой, и защита из находки 25
    работает: подход, дописанный после завершения, сдвигается ровно так же и
    по-прежнему остаётся за границей.
    """
    if not seconds:
        return
    async with _write_lock:
        await conn().execute(
            "UPDATE sets SET created_at = datetime(created_at, ?) WHERE block_id IN ("
            "  SELECT id FROM workout_blocks WHERE workout_id = ?)",
            (f"{seconds:+.0f} seconds", workout_id),
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


async def search_workouts_by_exercise(
    user_id: int, query: str, limit: int = 20, offset: int = 0
) -> list[aiosqlite.Row]:
    """Finished workouts containing an exercise whose name matches `query`,
    most recent first — the "в какой тренировке был жим" lookup, which the
    date-only history list can't answer.

    offset — вторая и следующие страницы: без неё старые тренировки частого
    упражнения физически недостижимы после первых 20 совпадений.
    """
    match, match_params = _stem_filter("e.display_name", query)
    cur = await conn().execute(
        "SELECT DISTINCT w.* FROM workouts w "
        "JOIN workout_blocks b ON b.workout_id = w.id "
        "JOIN block_exercises be ON be.block_id = b.id "
        "JOIN exercises e ON e.id = be.exercise_id "
        "WHERE w.user_id = ? AND w.status = 'finished' "
        f"  AND {match} "
        "ORDER BY w.started_at DESC LIMIT ? OFFSET ?",
        (user_id, *match_params, limit, offset),
    )
    return await cur.fetchall()


async def count_workouts_by_exercise(user_id: int, query: str) -> int:
    """Total finished workouts matching `query`, ignoring the LIMIT in
    `search_workouts_by_exercise` — the history search header needs the real
    total ("N нашлось"), not "сколько влезло под LIMIT 20".
    """
    match, match_params = _stem_filter("e.display_name", query)
    cur = await conn().execute(
        "SELECT COUNT(DISTINCT w.id) FROM workouts w "
        "JOIN workout_blocks b ON b.workout_id = w.id "
        "JOIN block_exercises be ON be.block_id = b.id "
        "JOIN exercises e ON e.id = be.exercise_id "
        "WHERE w.user_id = ? AND w.status = 'finished' "
        f"  AND {match}",
        (user_id, *match_params),
    )
    (count,) = await cur.fetchone()
    return count


async def count_workouts(user_id: int, status: str = "finished") -> int:
    cur = await conn().execute(
        "SELECT COUNT(*) FROM workouts WHERE user_id = ? AND status = ?", (user_id, status)
    )
    (count,) = await cur.fetchone()
    return count


async def list_finished_workout_dates(
    user_id: int, *, tz_offset: Optional[int] = None
) -> list[str]:
    """Calendar date (YYYY-MM-DD) of each finished workout, ascending — for the dashboard.

    One row per workout (same-day workouts appear twice), so counts reflect
    workout volume rather than distinct active days.

    День — местный для пользователя (см. «сутки по часам пользователя» выше):
    эти даты сравниваются с timeutil.user_today, и UTC-день ломал ровно те
    экраны, ради которых функция и существует — сводку, звание, стрик.
    """
    day = _local_day("started_at", await _tz_offset_of(user_id, tz_offset))
    cur = await conn().execute(
        f"SELECT {day} AS d FROM workouts "
        "WHERE user_id = ? AND status = 'finished' ORDER BY d",
        (user_id,),
    )
    return [r["d"] for r in await cur.fetchall()]


async def list_finished_workout_exercise_ids_by_date(
    user_id: int, *, tz_offset: Optional[int] = None
) -> dict[str, set[int]]:
    """Calendar date → exercise ids logged that day in finished workouts.

    Used by CSV import to tell "this day already has a workout" apart from
    "this day already has THIS exercise" — a manual bodyweight entry or an
    unrelated exercise on the import date must not sink the whole day's
    import (see csv_import._duplicate_dates).
    """
    day = _local_day("started_at", await _tz_offset_of(user_id, tz_offset))
    cur = await conn().execute(
        f"SELECT {day} AS d, be.exercise_id AS ex_id "
        "FROM workouts w "
        "JOIN workout_blocks wb ON wb.workout_id = w.id "
        "JOIN block_exercises be ON be.block_id = wb.id "
        "WHERE w.user_id = ? AND w.status = 'finished'",
        (user_id,),
    )
    result: dict[str, set[int]] = {}
    for row in await cur.fetchall():
        result.setdefault(row["d"], set()).add(row["ex_id"])
    return result


def _e1rm_sql(formula: str) -> str:
    """SQL mirror of analytics.e1rm — reps<=1 is the weight itself; brzycki
    falls back to epley above BRZYCKI_MAX_REPS (10), exactly like the Python."""
    w = LOAD_WEIGHT_SQL
    epley = f"{w} * (1 + s.reps / 30.0)"
    if formula == "brzycki":
        return (
            f"CASE WHEN s.reps <= 1 THEN {w} "
            f"WHEN s.reps > 10 THEN {epley} "
            f"ELSE {w} * 36.0 / (37 - s.reps) END"
        )
    return f"CASE WHEN s.reps <= 1 THEN {w} ELSE {epley} END"


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
        "  SELECT w.id, COUNT(s.id) AS sets_count, SUM(COALESCE(s.load_weight, s.weight) * s.reps) AS tonnage, "
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
    """Lifetime totals for the Hall of Fame: tonnage moved and total working
    sets, over all finished workouts.

    Не включает длину самой долгой тренировки: раньше она считалась тут же,
    сырым started_at/finished_at — тем же способом, от которого
    view_builder.workout_duration_seconds специально ушёл (находка 25:
    разрыв между стартом и финишем — это не время тренировки, если человек
    отвлёкся на часы, а не тренировался). Из-за этого «Самая длинная
    тренировка» на экране Достижений показывала часы явно нетренировочного
    простоя, а «Марафонец» (то же самое правило «длиннее 2 часов») на неё же
    не срабатывал — прямая нестыковка на одном экране. Теперь оба числа
    считает один и тот же view_builder.longest_workout_seconds.
    """
    cur = await conn().execute(
        "SELECT COALESCE(SUM(COALESCE(s.load_weight, s.weight) * s.reps), 0) AS tonnage, COUNT(s.id) AS sets_count "
        "FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "WHERE w.user_id = ? AND w.status = 'finished'",
        (user_id,),
    )
    row = await cur.fetchone()
    return {
        "tonnage": row["tonnage"] or 0.0,
        "sets_count": row["sets_count"] or 0,
    }


async def last_session_by_group(
    user_id: int, *, tz_offset: Optional[int] = None, before: Optional[str] = None
) -> dict[Optional[int], tuple[str, int]]:
    """Per muscle group: the date it was last trained and how many sets that
    session had — the two inputs a recovery estimate needs.

    One query rather than one per group: this feeds a screen the user opens
    several times per workout.

    Дата — местная: восстановление считается как разница с timeutil.user_today
    (analytics.recovery_percent), и UTC-день у вечерней тренировки давал
    отрицательную разницу — группа выглядела «0% восстановления» там, где надо
    было показать прогресс, и наоборот.

    `before` (YYYY-MM-DD) restricts to sessions strictly earlier than that local
    day — задним числом (находка 8) «отдых» должен считаться от даты записи,
    а не от «сегодня», и только по тому, что реально было раньше неё: без
    этого фильтра запись на 03.08 при последней сессии 06.08 читала бы её как
    прошлую, хотя та случилась на три дня позже даты, куда идёт запись.
    """
    day = _local_day("w.started_at", await _tz_offset_of(user_id, tz_offset))
    params: list = [user_id]
    where = "WHERE w.user_id = ? AND w.status = 'finished'"
    if before is not None:
        where += f" AND {day} < ?"
        params.append(before)
    cur = await conn().execute(
        "SELECT gid, day, cnt FROM ("
        f"  SELECT e.primary_group_id AS gid, {day} AS day, COUNT(s.id) AS cnt,"
        "         ROW_NUMBER() OVER ("
        f"             PARTITION BY e.primary_group_id ORDER BY {day} DESC"
        "         ) AS rn"
        "  FROM sets s"
        "  JOIN workout_blocks b ON b.id = s.block_id"
        "  JOIN workouts w ON w.id = b.workout_id"
        "  JOIN exercises e ON e.id = s.exercise_id"
        f"  {where}"
        f"  GROUP BY e.primary_group_id, {day}"
        ") WHERE rn = 1",
        tuple(params),
    )
    return {row["gid"]: (row["day"], row["cnt"]) for row in await cur.fetchall()}


async def weekly_volume_by_group(
    user_id: int, start_date: str, end_date: str, *, tz_offset: Optional[int] = None
) -> dict[Optional[int], int]:
    """Count of working sets per muscle group across finished workouts in [start_date, end_date].

    Keyed by exercises.primary_group_id (None bucketed under the NULL key). Dates
    are calendar days (YYYY-MM-DD) compared against the *local* day of
    workouts.started_at: границы окна вызывающая сторона считает от
    timeutil.user_today, поэтому и день тренировки должен быть местным — иначе
    вечерняя тренировка выпадала из окна, которое заканчивается «сегодня».
    """
    day = _local_day("w.started_at", await _tz_offset_of(user_id, tz_offset))
    cur = await conn().execute(
        "SELECT e.primary_group_id AS gid, COUNT(s.id) AS cnt "
        "FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "JOIN exercises e ON e.id = s.exercise_id "
        "WHERE w.user_id = ? AND w.status = 'finished' "
        f"AND {day} BETWEEN ? AND ? "
        "GROUP BY e.primary_group_id",
        (user_id, start_date, end_date),
    )
    return {row["gid"]: row["cnt"] for row in await cur.fetchall()}


# ---------- сводка на главном экране ----------

# Ниже — агрегаты, которых до сводки не было: тоннаж по неделям, серия e1RM по
# упражнению и счётчик рекордов. Всё поверх finished-тренировок и через
# LOAD_WEIGHT_SQL, то есть собственный вес в подтягиваниях считается так же, как
# везде (см. effective_load).


async def top_exercises_by_frequency(
    user_id: int,
    start_date: str,
    end_date: str,
    limit: int = 3,
    min_sessions: int = 2,
    *,
    tz_offset: Optional[int] = None,
) -> list[aiosqlite.Row]:
    """Упражнения, которые человек делает чаще всего, — по числу тренировок, а не
    подходов.

    Частота считается сессиями осознанно: двадцать подходов за один заход это
    не «часто», а один тяжёлый день, и по такому упражнению линия прогресса
    состоит из одной точки. `min_sessions` по той же причине: рисовать тренд по
    единственной тренировке нечестно, лучше показать то, где точек хватает.

    Ничьи разводятся числом подходов, потом именем — иначе сводка
    перетасовывалась бы между открытиями меню на одних и тех же данных, а
    порядок входит в ключ кэша картинки.

    Границы окна — местные календарные дни (как их и считает вызывающая сторона).
    """
    day = _local_day("w.started_at", await _tz_offset_of(user_id, tz_offset))
    cur = await conn().execute(
        "SELECT e.id, e.display_name, COUNT(DISTINCT w.id) AS sessions, "
        "       COUNT(s.id) AS sets_count "
        "FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "JOIN exercises e ON e.id = s.exercise_id "
        "WHERE w.user_id = ? AND w.status = 'finished' "
        f"AND {day} BETWEEN ? AND ? AND s.reps > 0 "
        "GROUP BY e.id "
        "HAVING sessions >= ? "
        "ORDER BY sessions DESC, sets_count DESC, e.display_name "
        "LIMIT ?",
        (user_id, start_date, end_date, min_sessions, limit),
    )
    return await cur.fetchall()


async def exercise_e1rm_series(
    user_id: int, exercise_id: int, sessions: int = 8, formula: str = "epley"
) -> list[float]:
    """Лучший e1RM упражнения в каждой из последних `sessions` тренировок, по
    возрастанию даты — точки для спарклайна.

    Одна точка на тренировку, а не на подход: внутри дня e1RM гуляет от
    разминочных и откатных подходов, и линия из подходов показывала бы пилу
    вместо тренда.
    """
    cur = await conn().execute(
        f"SELECT MAX({_e1rm_sql(formula)}) AS e1rm FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "WHERE w.user_id = ? AND w.status = 'finished' "
        "AND s.exercise_id = ? AND s.reps > 0 "
        "GROUP BY w.id ORDER BY w.started_at DESC, w.id DESC LIMIT ?",
        (user_id, exercise_id, sessions),
    )
    return [row["e1rm"] for row in reversed(await cur.fetchall())]


async def daily_tonnage(
    user_id: int, start_date: str, end_date: str, *, tz_offset: Optional[int] = None
) -> dict[str, float]:
    """Тоннаж по календарным дням в окне. По неделям сворачивает вызывающая
    сторона: у SQLite %W начинает неделю с воскресенья и ломается на границе
    года, а тут неделя должна совпадать с той, от которой считается всё
    остальное на экране.

    Дни — местные для пользователя, и ключами возвращаются тоже они: экран
    раскладывает тоннаж по своим датам, посчитанным от timeutil.user_today.
    """
    day = _local_day("w.started_at", await _tz_offset_of(user_id, tz_offset))
    cur = await conn().execute(
        f"SELECT {day} AS d, SUM({LOAD_WEIGHT_SQL} * s.reps) AS t "
        "FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "WHERE w.user_id = ? AND w.status = 'finished' "
        f"AND {day} BETWEEN ? AND ? "
        f"GROUP BY {day}",
        (user_id, start_date, end_date),
    )
    return {row["d"]: row["t"] or 0.0 for row in await cur.fetchall()}


async def e1rm_record_count(
    user_id: int, since_date: str, formula: str = "epley", *, tz_offset: Optional[int] = None
) -> int:
    """Сколько упражнений с `since_date` перебили свой прежний лучший e1RM.

    Упражнения, у которых прежнего лучшего нет вовсе, не считаются: первая в
    жизни тренировка движения формально бьёт рекорд в каждом подходе, и называть
    это рекордом значило бы поздравлять человека с тем, что он что-то попробовал.

    `since_date` — местный календарный день, поэтому и день тренировки местный:
    по UTC ночной рекорд первого дня окна попадал «до окна» и не считался.
    """
    e1rm = _e1rm_sql(formula)
    day = _local_day("w.started_at", await _tz_offset_of(user_id, tz_offset))
    cur = await conn().execute(
        "SELECT COUNT(*) AS n FROM ("
        f"  SELECT s.exercise_id,"
        f"         MAX(CASE WHEN {day} >= ? THEN {e1rm} END) AS inside,"
        f"         MAX(CASE WHEN {day} <  ? THEN {e1rm} END) AS earlier"
        "   FROM sets s"
        "   JOIN workout_blocks b ON b.id = s.block_id"
        "   JOIN workouts w ON w.id = b.workout_id"
        "   WHERE w.user_id = ? AND w.status = 'finished' AND s.reps > 0"
        "   GROUP BY s.exercise_id"
        ") WHERE earlier IS NOT NULL AND inside IS NOT NULL AND inside > earlier",
        (since_date, since_date, user_id),
    )
    return (await cur.fetchone())["n"]


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

    Would preserve supersets (a block with several exercises) as multi-id
    blocks — but nothing in the live "➕ Суперсет" flow actually creates one
    (every db.create_block call there passes "single"; what the UI calls a
    superset is two independent blocks whose sets happen to interleave in
    time, see db.list_superset_partners). So in practice every block here
    has exactly one exercise_id, and "Повторить тренировку" offers a
    superset pair as two separate plan entries, not one (находка 21).
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

# Вес подхода, каким его считает вся арифметика: снимок load_weight, если он
# есть, иначе то, что записал пользователь. COALESCE, а не миграция всех строк:
# у подходов, записанных до появления колонки, собственный вес взять неоткуда —
# вес тела на ту дату мог быть не записан вовсе, — и правильный ответ для них
# «как раньше», а не «как будто мы знали».
LOAD_WEIGHT_SQL = "COALESCE(s.load_weight, s.weight)"


def load_of(row) -> float:
    """Питоновский двойник LOAD_WEIGHT_SQL — для строк, выбранных как `s.*`.

    Вся арифметика (тоннаж, e1RM, рекорды, тренд, прогрессия) считает по нему,
    а `row["weight"]` остаётся тем, что человек записал, и показывается как есть.
    """
    try:
        load = row["load_weight"]
    except (IndexError, KeyError):
        load = None
    return row["weight"] if load is None else load


def effective_load(
    weight: float, bodyweight: Optional[float], load: str, factor: float = 1.0
) -> float:
    """Сколько реально поднято в подходе.

    `full` — вес тела и есть нагрузка, внешний добавляется (подтягивания с
    поясом); `assisted` — внешний вычитается (гравитрон, резина), но не ниже
    нуля; `none` — обычное железо, вес тела ни при чём. Без записанного веса
    тела возвращаем внешний: догадываться о массе человека неоткуда, а нулевой
    тоннаж честнее выдуманного.
    """
    if load == "none" or not bodyweight:
        return weight
    own = bodyweight * (factor if factor and factor > 0 else 1.0)
    if load == "assisted":
        return max(own - weight, 0.0)
    return own + weight


async def bodyweight_at(telegram_id: int, when_iso: Optional[str] = None) -> Optional[float]:
    """Ближайшее взвешивание не позже `when_iso` (по умолчанию — последнее).

    Не позже, а не «ближайшее в обе стороны»: подход, записанный в марте, не
    должен пересчитываться, когда человек взвесится в июне. Если до этой даты
    взвешиваний не было — берём самое раннее из имеющихся, иначе вся история до
    первого взвешивания осталась бы с нулевой нагрузкой.

    Без даты — последнее взвешивание, а не первое. Это не мелочь: сюда приходит
    каждый новый подход упражнения на своём весе (`_load_weight_for`), и «первое»
    означало, что человек, похудевший с 95 до 78, продолжал получать подтягивания
    с плюс-95 — e1RM завышен на четверть, тоннаж на 170 кг за подход.
    """
    if when_iso is None:
        cur = await conn().execute(
            "SELECT weight FROM bodyweight_logs WHERE telegram_id = ? "
            "ORDER BY logged_at DESC, id DESC LIMIT 1",
            (telegram_id,),
        )
        row = await cur.fetchone()
        return row["weight"] if row else None
    cur = await conn().execute(
        "SELECT weight FROM bodyweight_logs WHERE telegram_id = ? AND logged_at <= ? "
        "ORDER BY logged_at DESC, id DESC LIMIT 1",
        (telegram_id, when_iso),
    )
    row = await cur.fetchone()
    if row is not None:
        return row["weight"]
    cur = await conn().execute(
        "SELECT weight FROM bodyweight_logs WHERE telegram_id = ? ORDER BY logged_at, id LIMIT 1",
        (telegram_id,),
    )
    row = await cur.fetchone()
    return row["weight"] if row else None


async def _workout_date_of_block(block_id: int) -> Optional[str]:
    cur = await conn().execute(
        "SELECT w.started_at FROM workout_blocks b JOIN workouts w ON w.id = b.workout_id "
        "WHERE b.id = ?",
        (block_id,),
    )
    row = await cur.fetchone()
    return row["started_at"] if row else None


async def _load_weight_for(
    exercise_id: int, weight: float, when_iso: Optional[str] = None
) -> Optional[float]:
    """load_weight для подхода, или None для обычного железа.

    `when_iso` — дата тренировки, к которой подход относится, а не «сейчас»:
    занесённая задним числом тренировка и правка старого подхода должны брать
    вес тела того дня. Без даты берётся последнее взвешивание.
    """
    ex = await get_exercise(exercise_id)
    if ex is None or ex["bodyweight_load"] == "none":
        return None
    owner = ex["user_id"]
    if owner is None:
        return None
    return effective_load(
        weight,
        await bodyweight_at(owner, when_iso),
        ex["bodyweight_load"],
        ex["bodyweight_factor"],
    )


async def add_set(
    block_id: int,
    exercise_id: int,
    round_index: int,
    order_in_round: int,
    weight: float,
    reps: int,
    rpe: Optional[float] = None,
) -> int:
    load_weight = await _load_weight_for(
        exercise_id, weight, await _workout_date_of_block(block_id)
    )
    async with _write_lock:
        cur = await conn().execute(
            "INSERT INTO sets "
            "(block_id, exercise_id, round_index, order_in_round, weight, reps, rpe, "
            " load_weight, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (block_id, exercise_id, round_index, order_in_round, weight, reps, rpe,
             load_weight, now_iso()),
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
    load_weight = await _load_weight_for(
        exercise_id, weight, await _workout_date_of_block(block_id)
    )
    async with _write_lock:
        cur = await conn().execute(
            "INSERT INTO sets "
            "(block_id, exercise_id, round_index, order_in_round, weight, reps, rpe, "
            " load_weight, created_at) "
            "SELECT ?, ?, COALESCE(MAX(round_index), 0) + 1, ?, ?, ?, ?, ?, ? "
            "FROM sets WHERE block_id = ? AND exercise_id = ?",
            (
                block_id, exercise_id, order_in_round, weight, reps, rpe, load_weight, now_iso(),
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
    cur = await conn().execute(
        "SELECT s.exercise_id, w.started_at FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id WHERE s.id = ?",
        (set_id,),
    )
    row = await cur.fetchone()
    # Снимок нагрузки пересчитываем вместе с весом: иначе правка «80 → 85» у
    # подтягиваний с поясом оставила бы старую сумму. Вес тела при этом берётся
    # на дату той тренировки, а не сегодняшний: правка подхода из марта не должна
    # приезжать с июньским весом.
    load_weight = (
        await _load_weight_for(row["exercise_id"], weight, row["started_at"]) if row else None
    )
    async with _write_lock:
        await conn().execute(
            "UPDATE sets SET weight = ?, reps = ?, rpe = ?, load_weight = ? WHERE id = ?",
            (weight, reps, rpe, load_weight, set_id),
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
        f"SELECT {LOAD_WEIGHT_SQL} AS weight, s.reps, e.id AS exercise_id, e.display_name, "
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


async def list_opened_exercise_ids_for_workout(workout_id: int) -> list[int]:
    """Упражнения, которые в этой тренировке уже открывали, — включая те, где
    ещё нет ни одного подхода.

    Отличается от `list_exercise_ids_for_workout` ровно этим: та считает по
    подходам, и упражнение, открытое минуту назад и ещё не записанное, для неё
    не существует. Там, где вопрос звучит «что ещё осталось из плана», это
    ошибка — открытое упражнение возвращалось в очередь как несделанное.
    """
    cur = await conn().execute(
        "SELECT DISTINCT be.exercise_id FROM block_exercises be "
        "JOIN workout_blocks b ON b.id = be.block_id WHERE b.workout_id = ?",
        (workout_id,),
    )
    rows = await cur.fetchall()
    return [r["exercise_id"] for r in rows]


async def list_exercise_ids_for_workout(workout_id: int) -> list[int]:
    cur = await conn().execute(
        "SELECT DISTINCT s.exercise_id FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id WHERE b.workout_id = ?",
        (workout_id,),
    )
    rows = await cur.fetchall()
    return [r["exercise_id"] for r in rows]


async def get_workout_set_span(
    workout_id: int, before: Optional[str] = None
) -> Optional[tuple[str, str]]:
    """(first_set_created_at, last_set_created_at) for a workout, or None if it has no sets.

    `before` excludes sets created at or after that moment — used to keep a set
    added through the post-finish editor (see view_builder.workout_duration_seconds)
    from stretching the span past the workout's own finished_at.
    """
    if before is None:
        cur = await conn().execute(
            "SELECT MIN(s.created_at) AS first_at, MAX(s.created_at) AS last_at FROM sets s "
            "JOIN workout_blocks b ON b.id = s.block_id WHERE b.workout_id = ?",
            (workout_id,),
        )
    else:
        cur = await conn().execute(
            "SELECT MIN(s.created_at) AS first_at, MAX(s.created_at) AS last_at FROM sets s "
            "JOIN workout_blocks b ON b.id = s.block_id WHERE b.workout_id = ? AND s.created_at <= ?",
            (workout_id, before),
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


# ---------- MCP access tokens (см. mcp_server.py) ----------

async def issue_mcp_token(user_id: int) -> str:
    """Выдать пользователю новый токен доступа по MCP, погасив прежний.

    Перевыпуск и есть отзыв: старый токен удаляется той же транзакцией, поэтому
    «я вставил его не туда» лечится одной кнопкой, а не походом в базу. 32 байта
    энтропии (против 8 у визитки-снапшота): визитка отдаёт один снимок, который
    владелец и так собирался показать, а этот токен открывает всю историю
    тренировок целиком и живёт, пока его не отозвали.
    """
    token = secrets.token_urlsafe(32)
    async with _write_lock:
        await conn().execute("DELETE FROM mcp_tokens WHERE user_id = ?", (user_id,))
        await conn().execute(
            "INSERT INTO mcp_tokens (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, user_id, now_iso()),
        )
        await conn().commit()
    return token


async def get_mcp_token(user_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute("SELECT * FROM mcp_tokens WHERE user_id = ?", (user_id,))
    return await cur.fetchone()


async def revoke_mcp_token(user_id: int) -> bool:
    """True, если токен был и его удалили."""
    async with _write_lock:
        cur = await conn().execute("DELETE FROM mcp_tokens WHERE user_id = ?", (user_id,))
        await conn().commit()
        return cur.rowcount > 0


async def resolve_mcp_token(token: str) -> Optional[int]:
    """Токен → telegram_id владельца, или None. Обновляет отметку последнего
    использования (её показывает экран /mcp — по ней и видно чужие обращения).

    Пустой токен отсекается до запроса: `WHERE token = ''` ничего не найдёт и
    сейчас, но полагаться на это в проверке доступа не стоит.
    """
    if not token:
        return None
    cur = await conn().execute("SELECT user_id FROM mcp_tokens WHERE token = ?", (token,))
    row = await cur.fetchone()
    if row is None:
        return None
    async with _write_lock:
        await conn().execute(
            "UPDATE mcp_tokens SET last_used_at = ? WHERE token = ?", (now_iso(), token)
        )
        await conn().commit()
    return row["user_id"]


# ---------- MCP OAuth (см. mcp_oauth.py) ----------
#
# Хранилище OAuth-провайдера. Функции нарочно тонкие: политика — сроки жизни,
# длина кода, лимит попыток — живёт в mcp_oauth.py, здесь только запись и
# чтение. Исключений два, и оба про одноразовость: `consume_*` и
# `verify_oauth_link_code` обязаны проверять и гасить одной транзакцией, иначе
# между «код верный» и «код удалён» проходит второй запрос и одноразовый код
# перестаёт быть одноразовым.
#
# Значения токенов и кодов сюда приходят и здесь остаются: ни одна из функций
# ниже их не логирует.

# Сколько регистрация клиента живёт после того, как на неё не осталось ссылок.
# Клиент помнит свой client_id и приходит с ним же после отключения — снесённая
# регистрация означает «Client ID not found» вместо переподключения.
OAUTH_CLIENT_GRACE_SECONDS = 24 * 3600

# Предел на размер метаданных регистрации. Их пишет кто угодно без всякой
# авторизации (RFC 7591 это и есть), а «кто угодно» присылает и 200 000 символов
# в client_name — база у бота и у MCP одна, и заполненный диск роняет дневник.
OAUTH_CLIENT_METADATA_LIMIT = 4000


async def save_oauth_client(client_id: str, client_secret: Optional[str], metadata: str) -> None:
    """Зарегистрировать клиента (или перезаписать регистрацию под тем же id).

    Метаданные обрезаются по длине: их присылает кто угодно без авторизации, и
    складывать в базу присланные мегабайты незачем — всё, что нам из них нужно,
    это `client_name` и `redirect_uris`.
    """
    if len(metadata) > OAUTH_CLIENT_METADATA_LIMIT:
        raise ValueError("client metadata too large")
    async with _write_lock:
        await conn().execute(
            "INSERT OR REPLACE INTO oauth_clients (client_id, client_secret, metadata, created_at) "
            "VALUES (?, ?, ?, ?)",
            (client_id, client_secret, metadata, now_iso()),
        )
        await conn().commit()


async def get_oauth_client(client_id: str) -> Optional[aiosqlite.Row]:
    cur = await conn().execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,))
    return await cur.fetchone()


async def create_oauth_consent_request(
    request_id: str,
    client_id: str,
    redirect_uri: str,
    redirect_uri_provided_explicitly: bool,
    code_challenge: str,
    scopes: str,
    resource: Optional[str],
    state: Optional[str],
    expires_at: float,
) -> None:
    async with _write_lock:
        await conn().execute(
            "INSERT INTO oauth_consent_requests (request_id, client_id, redirect_uri, "
            "redirect_uri_provided_explicitly, code_challenge, scopes, resource, state, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                client_id,
                redirect_uri,
                int(redirect_uri_provided_explicitly),
                code_challenge,
                scopes,
                resource,
                state,
                expires_at,
            ),
        )
        await conn().commit()


async def get_oauth_consent_request(request_id: str) -> Optional[aiosqlite.Row]:
    if not request_id:
        return None
    cur = await conn().execute(
        "SELECT * FROM oauth_consent_requests WHERE request_id = ?", (request_id,)
    )
    return await cur.fetchone()


async def delete_oauth_consent_request(request_id: str) -> None:
    async with _write_lock:
        await conn().execute(
            "DELETE FROM oauth_consent_requests WHERE request_id = ?", (request_id,)
        )
        await conn().commit()


async def issue_oauth_link_code(
    user_id: int, ttl_seconds: int, digits: int = 6, *, reuse_live: bool = False
) -> str:
    """Код связывания: человек называет его на странице согласия, и только так
    страница узнаёт, кто перед ней.

    Живёт один на пользователя — выдать новый значит погасить прежний, поэтому
    «нажал кнопку дважды» не оставляет позади себя действующий код.

    `reuse_live=True` отдаёт действующий код, если он есть, — и проверка «есть
    ли» делается здесь, под тем же локом, что и вставка: снаружи между чтением и
    записью успевает пройти второй запрос, и два одновременных открытия экрана
    выдают два кода, из которых живёт только последний.
    """
    now = time.time()
    ceiling = 10**digits
    async with _write_lock:
        try:
            if reuse_live:
                cur = await conn().execute(
                    "SELECT code FROM oauth_link_codes WHERE user_id = ? AND expires_at > ?",
                    (user_id, now),
                )
                live = await cur.fetchone()
                if live is not None:
                    return live["code"]
            await conn().execute("DELETE FROM oauth_link_codes WHERE user_id = ?", (user_id,))
            # Заодно чистим просрочку: иначе мёртвый код продолжает занимать шесть
            # цифр из миллиона до ближайшей ночной прополки.
            await conn().execute("DELETE FROM oauth_link_codes WHERE expires_at < ?", (now,))
            for _ in range(20):
                code = f"{secrets.randbelow(ceiling):0{digits}d}"
                cur = await conn().execute(
                    "SELECT 1 FROM oauth_link_codes WHERE code = ?", (code,)
                )
                if await cur.fetchone() is None:
                    break
            else:  # pragma: no cover — миллион живых кодов у личного бота недостижим
                raise RuntimeError("не удалось подобрать свободный код связывания")
            await conn().execute(
                "INSERT INTO oauth_link_codes (code, user_id, expires_at) VALUES (?, ?, ?)",
                (code, user_id, now + ttl_seconds),
            )
            await conn().commit()
            return code
        except Exception:
            await conn().rollback()
            raise


async def get_live_oauth_link_code(user_id: int) -> Optional[str]:
    """Действующий код пользователя, если он ещё жив.

    Нужен, чтобы повторное открытие инструкции не гасило код, который человек
    уже скопировал: он вернулся перечитать шаг, а код в браузере от этого мёртвым
    становиться не должен.
    """
    cur = await conn().execute(
        "SELECT code FROM oauth_link_codes WHERE user_id = ? AND expires_at > ?",
        (user_id, time.time()),
    )
    row = await cur.fetchone()
    return row["code"] if row else None


async def verify_oauth_link_code(
    request_id: str,
    code: str,
    max_attempts: int,
    *,
    client_ip: Optional[str] = None,
    window_seconds: float = 0.0,
    window_limit_per_ip: int = 0,
    window_limit_total: int = 0,
) -> tuple[str, Optional[int]]:
    """Сверить код связывания с заявкой на согласие, одной транзакцией.

    Возвращает («ok», user_id) либо причину отказа: `unknown_request`,
    `expired_request`, `too_many_attempts` (заперта заявка), `rate_limited`
    (исчерпано окно неудач), `bad_code`. Причина нужна странице согласия: «код не
    тот», «заявка устарела» и «слишком часто» лечатся по-разному.

    Три предела, и только последние два действительно ограничивают перебор:

    * `max_attempts` — на заявку. Он про опечатки человека, а не про защиту:
      заявку создаёт бесплатный GET /authorize, так что перебирающий просто
      берёт новую.
    * `window_limit_per_ip` — неудачи с одного адреса за окно. Это и есть замок:
      двадцать бит кода переберут, только если дать пробовать без счёта.
    * `window_limit_total` — предохранитель на случай перебора с многих адресов.
      Ставится заведомо выше того, что способен набрать живой человек.

    Верный код гасится здесь же. Неудача пишется в `oauth_consent_failures` — и
    предел проверяется ДО сверки, иначе счёт попыток не ограничен вовсе.
    """
    now = time.time()
    async with _write_lock:
        try:
            cur = await conn().execute(
                "SELECT attempts, expires_at FROM oauth_consent_requests WHERE request_id = ?",
                (request_id,),
            )
            request = await cur.fetchone()
            if request is None:
                return "unknown_request", None
            if request["expires_at"] < now:
                return "expired_request", None
            if request["attempts"] >= max_attempts:
                return "too_many_attempts", None
            if window_seconds > 0:
                # Просрочку убираем здесь же: окно скользящее, и старые строки в
                # нём не участвуют — держать их незачем.
                await conn().execute(
                    "DELETE FROM oauth_consent_failures WHERE at < ?", (now - window_seconds,)
                )
                cur = await conn().execute(
                    "SELECT COUNT(*) AS total, "
                    "SUM(CASE WHEN client_ip IS ? THEN 1 ELSE 0 END) AS same_ip "
                    "FROM oauth_consent_failures WHERE at >= ?",
                    (client_ip, now - window_seconds),
                )
                seen = await cur.fetchone()
                too_many_here = (
                    window_limit_per_ip > 0 and (seen["same_ip"] or 0) >= window_limit_per_ip
                )
                too_many_anywhere = (
                    window_limit_total > 0 and seen["total"] >= window_limit_total
                )
                if too_many_here or too_many_anywhere:
                    await conn().commit()
                    return "rate_limited", None
            cur = await conn().execute(
                "SELECT user_id FROM oauth_link_codes WHERE code = ? AND expires_at > ?",
                (code, now),
            )
            row = await cur.fetchone()
            if row is None:
                await conn().execute(
                    "UPDATE oauth_consent_requests SET attempts = attempts + 1 "
                    "WHERE request_id = ?",
                    (request_id,),
                )
                await conn().execute(
                    "INSERT INTO oauth_consent_failures (at, client_ip) VALUES (?, ?)",
                    (now, client_ip),
                )
                await conn().commit()
                return "bad_code", None
            await conn().execute("DELETE FROM oauth_link_codes WHERE code = ?", (code,))
            await conn().commit()
            return "ok", row["user_id"]
        except Exception:
            # Иначе недоделанная транзакция достаётся следующему писателю, и он
            # коммитит её вместе со своей (тот же довод, что в discard_workout).
            await conn().rollback()
            raise


async def create_oauth_auth_code(
    code: str,
    client_id: str,
    user_id: int,
    redirect_uri: str,
    redirect_uri_provided_explicitly: bool,
    code_challenge: str,
    scopes: str,
    resource: Optional[str],
    expires_at: float,
) -> None:
    async with _write_lock:
        await conn().execute(
            "INSERT INTO oauth_auth_codes (code, client_id, user_id, redirect_uri, "
            "redirect_uri_provided_explicitly, code_challenge, scopes, resource, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                code,
                client_id,
                user_id,
                redirect_uri,
                int(redirect_uri_provided_explicitly),
                code_challenge,
                scopes,
                resource,
                expires_at,
            ),
        )
        await conn().commit()


async def get_oauth_auth_code(code: str) -> Optional[aiosqlite.Row]:
    if not code:
        return None
    cur = await conn().execute("SELECT * FROM oauth_auth_codes WHERE code = ?", (code,))
    return await cur.fetchone()


async def consume_oauth_auth_code(code: str) -> Optional[aiosqlite.Row]:
    """Забрать код авторизации и погасить его. None — кода нет, значит его уже
    обменяли (или он и не существовал), и второй обмен обязан провалиться."""
    if not code:
        return None
    async with _write_lock:
        cur = await conn().execute("SELECT * FROM oauth_auth_codes WHERE code = ?", (code,))
        row = await cur.fetchone()
        if row is None:
            return None
        await conn().execute("DELETE FROM oauth_auth_codes WHERE code = ?", (code,))
        await conn().commit()
        return row


async def create_oauth_token(
    access_token: str,
    refresh_token: Optional[str],
    client_id: str,
    user_id: int,
    scopes: str,
    resource: Optional[str],
    expires_at: float,
    refresh_expires_at: Optional[float],
    connected_at: Optional[str] = None,
) -> None:
    """`connected_at` передаётся при обновлении токена — это дата, когда человек
    подтвердил доступ. Без него пара считается новым подключением."""
    now = now_iso()
    async with _write_lock:
        await conn().execute(
            "INSERT INTO oauth_tokens (access_token, refresh_token, client_id, user_id, scopes, "
            "resource, expires_at, refresh_expires_at, created_at, connected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                access_token,
                refresh_token,
                client_id,
                user_id,
                scopes,
                resource,
                expires_at,
                refresh_expires_at,
                now,
                connected_at or now,
            ),
        )
        await conn().commit()


async def get_oauth_access_token(access_token: str) -> Optional[aiosqlite.Row]:
    if not access_token:
        return None
    cur = await conn().execute(
        "SELECT * FROM oauth_tokens WHERE access_token = ?", (access_token,)
    )
    return await cur.fetchone()


async def get_oauth_refresh_token(refresh_token: str) -> Optional[aiosqlite.Row]:
    if not refresh_token:
        return None
    cur = await conn().execute(
        "SELECT * FROM oauth_tokens WHERE refresh_token = ?", (refresh_token,)
    )
    return await cur.fetchone()


async def consume_oauth_refresh_token(refresh_token: str) -> Optional[aiosqlite.Row]:
    """Забрать пару по refresh-токену и удалить её целиком: обмен обязан
    ротировать оба токена, а старая пара после обмена не должна открывать
    ничего (RFC 6749 §10.4 — украденный refresh иначе живёт вечно)."""
    if not refresh_token:
        return None
    async with _write_lock:
        cur = await conn().execute(
            "SELECT * FROM oauth_tokens WHERE refresh_token = ?", (refresh_token,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        await conn().execute(
            "DELETE FROM oauth_tokens WHERE refresh_token = ?", (refresh_token,)
        )
        await conn().commit()
        return row


async def touch_oauth_token(access_token: str) -> None:
    """Отметка последнего обращения — её показывает раздел «Подключённые
    приложения»: только по ней и видно, ходит ли приложение за данными."""
    async with _write_lock:
        await conn().execute(
            "UPDATE oauth_tokens SET last_used_at = ? WHERE access_token = ?",
            (now_iso(), access_token),
        )
        await conn().commit()


async def revoke_oauth_token(token: str) -> bool:
    """Погасить пару по любому из её токенов: строка одна, поэтому отзыв access
    убивает и парный refresh, и наоборот."""
    if not token:
        return False
    async with _write_lock:
        cur = await conn().execute(
            "DELETE FROM oauth_tokens WHERE access_token = ? OR refresh_token = ?",
            (token, token),
        )
        await conn().commit()
        return cur.rowcount > 0


async def list_oauth_connections(user_id: int) -> list[aiosqlite.Row]:
    """Подключённые приложения пользователя — по одной строке на клиента.

    Живым считается подключение, у которого не истёк refresh: access живёт час,
    и по нему половина списка исчезала бы у человека на глазах.
    """
    now = time.time()
    cur = await conn().execute(
        "SELECT t.client_id AS client_id, c.metadata AS metadata, COUNT(*) AS tokens, "
        # Именно connected_at: created_at переписывается на каждом обновлении
        # токена, то есть раз в час, и «подключено» уезжало бы вслед за ним.
        "MIN(COALESCE(t.connected_at, t.created_at)) AS connected_at, "
        "MAX(t.last_used_at) AS last_used_at "
        "FROM oauth_tokens t LEFT JOIN oauth_clients c ON c.client_id = t.client_id "
        "WHERE t.user_id = ? AND COALESCE(t.refresh_expires_at, t.expires_at) > ? "
        # client_id вторым ключом — чтобы список не перетасовывался между
        # открытиями экрана: подключить два приложения в одну секунду вполне
        # реально, а кнопка «Отключить» стоит напротив имени.
        "GROUP BY t.client_id ORDER BY connected_at DESC, client_id",
        (user_id, now),
    )
    return list(await cur.fetchall())


async def revoke_oauth_client_tokens(user_id: int, client_id: str) -> int:
    """«Отключить приложение»: всё, чем этот клиент может вернуться.

    Пар может быть несколько — клиент обменял refresh и завёл вторую, — и это
    только половина. Вторая половина: уже выданный код авторизации и открытая
    заявка на согласие. Их не гасить значит оставить приложению путь обратно
    через минуты после того, как человеку сказали «доступ закрыт».
    """
    async with _write_lock:
        try:
            cur = await conn().execute(
                "DELETE FROM oauth_tokens WHERE user_id = ? AND client_id = ?",
                (user_id, client_id),
            )
            revoked = cur.rowcount
            await conn().execute(
                "DELETE FROM oauth_auth_codes WHERE user_id = ? AND client_id = ?",
                (user_id, client_id),
            )
            # Заявка ещё не знает пользователя — она создаётся до подтверждения,
            # поэтому гасим все заявки этого клиента. Чужую этим не сломать:
            # заявка живёт минуты и принадлежит тому же приложению.
            await conn().execute(
                "DELETE FROM oauth_consent_requests WHERE client_id = ?", (client_id,)
            )
            await conn().commit()
            return revoked
        except Exception:
            await conn().rollback()
            raise


async def purge_expired_oauth(now: Optional[float] = None) -> int:
    """Прополка просрочки: коды, заявки и мёртвые пары токенов.

    Без неё таблицы только растут — каждая попытка подключения оставляет по
    строке в трёх из них, и удалять их некому: успешный флоу гасит свои, а
    брошенный на полпути не гасит ничего.
    """
    moment = time.time() if now is None else now
    deleted = 0
    async with _write_lock:
        try:
            for statement in (
                "DELETE FROM oauth_auth_codes WHERE expires_at < ?",
                "DELETE FROM oauth_consent_requests WHERE expires_at < ?",
                "DELETE FROM oauth_link_codes WHERE expires_at < ?",
                "DELETE FROM oauth_tokens WHERE COALESCE(refresh_expires_at, expires_at) < ?",
                # Неудачные попытки нужны только внутри своего окна; окно — минуты.
                "DELETE FROM oauth_consent_failures WHERE at < ?",
            ):
                cur = await conn().execute(statement, (moment,))
                deleted += cur.rowcount
            # Клиенты — последними, когда мёртвых токенов уже нет. Каждое «добавить
            # коннектор» регистрирует нового: приложение, которое человек отключил или
            # не довёл до конца, иначе остаётся в таблице навсегда.
            #
            # Но не сразу: клиент помнит свой client_id и после отключения приходит
            # с ним же. Снесённая регистрация превращает «подключить заново» в
            # «Client ID not found», а обещание на экране — в ложь. Отсюда отсрочка:
            # ссылок нет и регистрация старше суток.
            cur = await conn().execute(
                "DELETE FROM oauth_clients WHERE created_at < ? AND client_id NOT IN "
                "(SELECT client_id FROM oauth_tokens UNION "
                " SELECT client_id FROM oauth_consent_requests UNION "
                " SELECT client_id FROM oauth_auth_codes)",
                (
                    dt.datetime.fromtimestamp(moment - OAUTH_CLIENT_GRACE_SECONDS).isoformat(
                        timespec="seconds"
                    ),
                ),
            )
            deleted += cur.rowcount
            await conn().commit()
            return deleted
        except Exception:
            await conn().rollback()
            raise


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

    Кандидаты с суффиксом ужимаются под MAX_PROGRAM_NAME_LENGTH: имя ровно в
    лимит получало « (2)» сверху, и такую программу потом нельзя было даже
    переименовать — ручной ввод длиннее лимита не принимается.

    По той же причине режется и имя без суффикса: раньше ветка «коллизии нет»
    возвращала что дали, и импорт чужой визитки заводил программу длиннее, чем
    разрешает переименование, — исправить её было нечем.
    """

    def with_suffix(tail: str) -> str:
        # Урезаем базу, а не хвост: хвост и есть то, что различает копии.
        room = config.MAX_PROGRAM_NAME_LENGTH - len(tail)
        return f"{name[:room].rstrip()}{tail}"

    name = name.strip()[: config.MAX_PROGRAM_NAME_LENGTH].rstrip()
    if await find_program_by_name(user_id, name) is None:
        return name
    if suffix:
        candidate = with_suffix(f" ({suffix})")
        if await find_program_by_name(user_id, candidate) is None:
            return candidate
    for n in range(2, 100):
        candidate = with_suffix(f" ({n})")
        if await find_program_by_name(user_id, candidate) is None:
            return candidate
    return with_suffix(f" ({secrets.token_hex(2)})")


async def move_routine_to_program(routine_id: int, program_id: Optional[int]) -> None:
    """Put a day into a program (appended last), or take it out of one.

    `program_id=None` makes it a standalone routine again — the "вынести день из
    программы" direction, which was impossible while a program was just a shared
    string.
    """
    async with _write_lock:
        db = conn()
        if program_id is None:
            await db.execute(
                "UPDATE routines SET program_id = NULL, day_order = 0 WHERE id = ?",
                (routine_id,),
            )
        else:
            # Порядок — подзапросом внутри UPDATE, по той же причине, что в
            # create_routine: между чтением MAX и записью успевает вклиниться
            # второй апдейт.
            await db.execute(
                "UPDATE routines SET program_id = ?, day_order = "
                "(SELECT COALESCE(MAX(day_order), -1) + 1 FROM routines WHERE program_id = ?) "
                "WHERE id = ?",
                (program_id, program_id, routine_id),
            )
        await db.commit()


async def reorder_program_day(routine_id: int, direction: str) -> None:
    """Переставить день программы относительно соседа — по кругу.

    Та же механика, что у reorder_routine_exercise этажом ниже, и по той же
    причине: в экране с одной стрелкой первый день был бы намертво приколочен к
    первому месту, а сдвинуть его можно было бы только подняв по очереди все
    остальные.
    """
    routine = await get_routine(routine_id)
    if routine is None or routine["program_id"] is None:
        return
    days = await list_program_days_by_id(routine["program_id"])
    ids = [d["id"] for d in days]
    if routine_id not in ids or len(ids) < 2:
        return
    idx = ids.index(routine_id)
    neighbour = (idx - 1 if direction == "up" else idx + 1) % len(ids)
    if neighbour == idx:
        return
    a = days[idx]
    if abs(neighbour - idx) == 1:
        b = days[neighbour]
        pairs = [(a["id"], b["day_order"]), (b["id"], a["day_order"])]
    else:
        # Перенос через край: остальные сдвигаются на одну позицию, иначе обмен
        # первого с последним перетасовал бы весь список, а не сдвинул на шаг.
        rest = [d for d in days if d["id"] != a["id"]]
        order = [*rest, a] if direction == "up" else [a, *rest]
        pairs = [(d["id"], i) for i, d in enumerate(order)]
    async with _write_lock:
        for day_id, day_order in pairs:
            await conn().execute(
                "UPDATE routines SET day_order = ? WHERE id = ?", (day_order, day_id)
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
    async with _write_lock:
        db = conn()
        if program_id is not None and day_order is None:
            # Порядок дня считается внутри INSERT, а не отдельным SELECT до него.
            # aiogram обрабатывает апдейты конкурентно, и два быстрых тапа
            # «➕ Добавить день» успевали прочитать один и тот же MAX(day_order)
            # раньше, чем первый вставил строку: оба дня получали один порядок, а
            # «поднять день» после этого переставлял не тот, потому что сортировка
            # становилась неоднозначной. Тот же приём, что в append_set и
            # create_block — там этот же race уже был найден и закрыт.
            cur = await db.execute(
                "INSERT INTO routines (user_id, name, created_at, program_id, day_order) "
                "SELECT ?, ?, ?, ?, COALESCE(MAX(day_order), -1) + 1 "
                "FROM routines WHERE program_id = ?",
                (user_id, name, now_iso(), program_id, program_id),
            )
        else:
            cur = await db.execute(
                "INSERT INTO routines (user_id, name, created_at, program_id, day_order) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, name, now_iso(), program_id, day_order or 0),
            )
        await db.commit()
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
    """Переставить упражнение дня относительно соседа: "up" — выше, "down" — ниже.

    По кругу: у первого «выше» отправляет его в конец, у последнего «ниже» — в
    начало. Иначе в редакторе с одной стрелкой (см. keyboards.routine_edit_keyboard)
    первое упражнение было бы намертво приколочено к первому месту, а сдвинуть
    его можно было бы только подняв по очереди все остальные.
    """
    entry = await get_routine_exercise(routine_exercise_id)
    if entry is None:
        return
    exercises = await list_routine_exercises(entry["routine_id"])
    ids = [ex["id"] for ex in exercises]
    if routine_exercise_id not in ids:
        return
    idx = ids.index(routine_exercise_id)
    if len(ids) < 2:
        return
    neighbor_idx = (idx - 1 if direction == "up" else idx + 1) % len(ids)
    if neighbor_idx == idx:
        return
    a, b = exercises[idx], exercises[neighbor_idx]
    if abs(neighbor_idx - idx) == 1:
        # Обычный шаг — меняем местами с соседом.
        pairs = [(a["id"], b["order_index"]), (b["id"], a["order_index"])]
    else:
        # Перенос через край: остальные сдвигаются на одну позицию, иначе обмен
        # первого с последним перетасовал бы весь список, а не сдвинул на шаг.
        rest = [ex for ex in exercises if ex["id"] != a["id"]]
        order = [*rest, a] if direction == "up" else [a, *rest]
        pairs = [(ex["id"], i) for i, ex in enumerate(order)]
    async with _write_lock:
        for ex_id, order_index in pairs:
            await conn().execute(
                "UPDATE routine_exercises SET order_index = ? WHERE id = ?", (order_index, ex_id)
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
    user_id: int, since: str, limit: int = 3, *, tz_offset: Optional[int] = None
) -> list[dict[str, Any]]:
    """Программы, по которым человек реально тренировался начиная с `since`.

    Считаем по `workouts.routine_id` — по факту проведённых тренировок, а не по
    тому, что лежит в списке программ: на экране «начать тренировку» нужны те
    два-три сплита, между которыми человек ходит сейчас.

    Дни одной программы схлопываются в одну строку (тренировался по «ноги» и по
    «верх» — это одна программа), одиночные шаблоны без программы идут сами по
    себе. `program_id` — чем открывается экран программы; `anchor_id` оставлен
    для старых клавиатур, он же routine последнего пройденного дня.

    `since` — местная дата (окно считается от «сегодня» пользователя), поэтому и
    день тренировки местный: программа, по которой человек тренировался вчера
    вечером, не должна выпадать из «последних» из-за UTC-границы суток.
    """
    day = _local_day("w.started_at", await _tz_offset_of(user_id, tz_offset))
    cur = await conn().execute(
        "SELECT r.id AS routine_id, r.name AS routine_name, r.program_id, "
        "p.name AS program_name, MAX(w.started_at) AS last_started "
        "FROM workouts w JOIN routines r ON r.id = w.routine_id "
        "LEFT JOIN programs p ON p.id = r.program_id "
        f"WHERE w.user_id = ? AND r.user_id = ? AND {day} >= date(?) "
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


async def list_programs_without_workout_history(user_id: int, limit: int) -> list[dict[str, Any]]:
    """Programs/standalone days добавлены, но по ним ещё ни разу не тренировались —
    list_recent_programs их не видит (считает по workouts.routine_id), а именно
    в момент "только что добавил" человек и хочет по ней пойти (находка 1).

    Sorted by created_at DESC — свежедобавленное выше. Shaped like
    list_recent_programs's rows (last_started always None here) so callers can
    concatenate the two lists without special-casing.
    """
    out: list[dict[str, Any]] = []
    cur = await conn().execute(
        "SELECT p.id AS program_id, p.name, p.created_at, "
        "(SELECT r.id FROM routines r WHERE r.program_id = p.id "
        " ORDER BY r.day_order ASC, r.id ASC LIMIT 1) AS anchor_id "
        "FROM programs p WHERE p.user_id = ? AND NOT EXISTS ("
        "  SELECT 1 FROM workouts w JOIN routines r ON r.id = w.routine_id WHERE r.program_id = p.id"
        ") ORDER BY p.created_at DESC",
        (user_id,),
    )
    for row in await cur.fetchall():
        if row["anchor_id"] is None:  # program with no days at all — nothing to open
            continue
        out.append(
            {
                "name": row["name"],
                "program_id": row["program_id"],
                "routine_id": row["anchor_id"],
                "anchor_id": row["anchor_id"],
                "last_started": None,
                "created_at": row["created_at"],
            }
        )

    cur = await conn().execute(
        "SELECT r.id AS routine_id, r.name, r.created_at FROM routines r "
        "WHERE r.user_id = ? AND r.program_id IS NULL AND NOT EXISTS ("
        "  SELECT 1 FROM workouts w WHERE w.routine_id = r.id"
        ") ORDER BY r.created_at DESC",
        (user_id,),
    )
    for row in await cur.fetchall():
        out.append(
            {
                "name": row["name"],
                "program_id": None,
                "routine_id": row["routine_id"],
                "anchor_id": row["routine_id"],
                "last_started": None,
                "created_at": row["created_at"],
            }
        )

    out.sort(key=lambda p: p["created_at"], reverse=True)
    return out[:limit]


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


def _unique_sibling_name(name: str, taken: set[str]) -> str:
    """Имя, не повторяющее уже занятые среди дней той же программы.

    Дни программы — это кнопки на одном экране (keyboards.program_days_keyboard
    и program_day_order_keyboard), и два «День 1» подряд там неразличимы.
    Разводим суффиксом, а не отбрасыванием — состав у дней разный, и терять день
    из-за совпавшего имени было бы куда хуже.
    """
    key = name.strip().lower()
    if key not in taken:
        taken.add(key)
        return name
    for suffix in range(2, 100):
        marker = f" ({suffix})"
        candidate = name[: config.MAX_PROGRAM_NAME_LENGTH - len(marker)].rstrip() + marker
        if candidate.strip().lower() not in taken:
            taken.add(candidate.strip().lower())
            return candidate
    taken.add(key)
    return name


async def merge_programs(user_id: int, source_id: int, target_id: int) -> None:
    """Fold every day of `source_id` into `target_id` and drop the empty shell.

    The explicit version of what renaming one program onto another used to do by
    accident — offered as a choice when rename_program_by_id reports a
    collision, so "actually yes, join them" stays possible.

    Дни из source могут называться так же, как дни, уже стоящие в target, —
    особенно часто, когда сливают программу с её же дубликатом («Дублировать»
    даёт копии те же имена дней). Без переименования итог — несколько дней с
    одинаковой подписью, из которых на экране программы не выбрать нужный.
    """
    source = await get_program(source_id)
    target = await get_program(target_id)
    if source is None or target is None:
        return
    if source["user_id"] != user_id or target["user_id"] != user_id:
        return
    taken = {d["name"].strip().lower() for d in await list_program_days_by_id(target_id)}
    for day in await list_program_days_by_id(source_id):
        name = _unique_sibling_name(day["name"], taken)
        async with _write_lock:
            await conn().execute(
                "UPDATE routines SET program_id = ?, name = ?, day_order = "
                "(SELECT COALESCE(MAX(day_order), -1) + 1 FROM routines WHERE program_id = ?) "
                "WHERE id = ?",
                (target_id, name, target_id, day["id"]),
            )
            await conn().commit()
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
    """Case-insensitive (Cyrillic-safe, ё=е) match of a global template by its bare name."""
    cur = await conn().execute(
        "SELECT * FROM exercises WHERE is_template = 1 AND user_id IS NULL"
    )
    rows = await cur.fetchall()
    needle = _fold_exercise_name(name)
    for r in rows:
        if _fold_exercise_name(r["name"] or "") == needle:
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


async def exercise_group_name(user_id: int, name: str) -> Optional[str]:
    """Группа мышц упражнения по имени — тем же порядком, что resolve_exercise_name.

    Нужна, чтобы посчитать недельный объём предложенной программы кодом: раньше
    это число называла сама модель по своему же черновику и ошибалась (обещала
    ~12 подходов на грудь в программе, где их 19).
    """
    row = await find_exercise_by_name(user_id, name) or await _find_global_template_by_name(name)
    if row is None or row["primary_group_id"] is None:
        return None
    cur = await conn().execute(
        "SELECT name FROM muscle_groups WHERE id = ?", (row["primary_group_id"],)
    )
    group = await cur.fetchone()
    return group["name"] if group else None


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
    day_word = formatting.plural_ru(existing, ("день", "дня", "дней"))
    return (
        f"У тебя уже {existing} {day_word} в программах — больше "
        f"{config.MAX_ROUTINES_PER_USER} не влезет. Удали лишние в «🗂 Программы» "
        "и попробуй ещё раз."
    )


async def set_routine_exercise_target(routine_exercise_id: int, target: Optional[str]) -> None:
    """Change the sets×reps scheme in place.

    Without this, changing «3×10» to «4×8» meant removing the exercise (behind a
    confirmation), adding it again — where it lands at the end — and walking it
    back up with the arrows.

    Сюда приходит только ручной редактор (AI-путь пишет target при вставке
    строки), и ручная правка схемы означает «теперь по-моему»: правило
    прогрессии из программы сбрасывается вместе со старой схемой — иначе
    человек, сменивший «3×8-10» на «5×5», продолжал бы получать подсказку
    «🎯 Цель — по программе» из правила, которое, по его мнению, стёр.
    """
    async with _write_lock:
        await conn().execute(
            "UPDATE routine_exercises SET target = ?, progression = NULL WHERE id = ?",
            (target, routine_exercise_id),
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

    Только завершённые тренировки: голый тап «▶️ День 1», брошенный в
    раздевалке, — не сессия. Без фильтра экран программы писал «сегодня»
    про день, который не сделан, и навсегда перепрыгивал его в «дальше по
    кругу», а инструмент adherence AI-тренера считал такие тапы посещениями.
    """
    cur = await conn().execute(
        "SELECT r.id AS routine_id, MAX(w.started_at) AS last_started, COUNT(*) AS times "
        "FROM routines r JOIN workouts w ON w.routine_id = r.id "
        "WHERE r.program_id = ? AND w.status = 'finished' GROUP BY r.id",
        (program_id,),
    )
    return {row["routine_id"]: (row["last_started"], row["times"]) for row in await cur.fetchall()}


async def program_total_workouts(program_id: int) -> int:
    """How many finished workouts were ever done by this program — across every
    day, including days since deleted (see workouts.program_id, находка 22).
    Unlike program_day_history, this doesn't need the routines row to still
    exist, so deleting a day never quietly shrinks it."""
    cur = await conn().execute(
        "SELECT COUNT(*) AS n FROM workouts WHERE program_id = ? AND status = 'finished'",
        (program_id,),
    )
    row = await cur.fetchone()
    return row["n"] if row else 0


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
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    source: str = "bot",
) -> None:
    """Строка в cost_events + строка в лог с ценой этого вызова.

    Лог живёт здесь, а не у вызывающих, потому что здесь сходятся все платные
    вызовы — Grok, Qwen на Novita, расшифровка голосовых. Пока каждый логировал
    сам, формат разъезжался: у видео не было имени модели и фраза была своя, и
    поиск по логам единым запросом его не находил. Новая модель, добавленная
    когда-нибудь ещё, попадёт в лог сама, без напоминания.

    cached_tokens — часть входа, приехавшая из кэша провайдера: считается по
    сниженной ставке (config.CACHED_INPUT_PRICE_MULTIPLIER). Пока множитель равен
    единице, цена — честный потолок, и в строке про это сказано прямо.

    reasoning_tokens — внутренние размышления модели, отдельный billable тип у
    xAI: в completion_tokens они не входят и тарифицируются как выход.
    """
    if event_type == "transcription":
        price = config.TRANSCRIPTION_PRICE_USD_PER_CALL
    elif event_type == "server_tool":
        price = config.SERVER_TOOL_PRICE_USD_PER_CALL
    else:
        price = config.call_price_usd(
            model or "", prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens
        )
    notes = []
    if cached_tokens:
        notes.append(f"из кэша {cached_tokens}")
    if reasoning_tokens:
        notes.append(f"размышления {reasoning_tokens}")
    logger.info(
        "cost %s %s user %s: %s+%s токенов%s, ~$%.4f%s",
        event_type, model, user_id, prompt_tokens, completion_tokens,
        f" ({', '.join(notes)})" if notes else "",
        price,
        f" [{source}]" if source != "bot" else "",
    )
    async with _write_lock:
        await conn().execute(
            "INSERT INTO cost_events "
            "(user_id, event_type, model, prompt_tokens, completion_tokens, "
            " cached_tokens, reasoning_tokens, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, event_type, model, prompt_tokens, completion_tokens,
             cached_tokens, reasoning_tokens, source, now_iso()),
        )
        await conn().commit()


async def get_llm_cost_breakdown(date_str: str) -> dict[str, dict[str, int]]:
    """Per-model токены за календарный день по событиям llm_call.

    Кэш и размышления идут отдельными суммами, потому что тарифицируются иначе:
    кэш дешевле входа, размышления считаются как выход. Без них отчёт считал бы
    не то же, что строка в логе.
    """
    cur = await conn().execute(
        "SELECT model, COUNT(*), COALESCE(SUM(prompt_tokens), 0), COALESCE(SUM(completion_tokens), 0), "
        "COALESCE(SUM(cached_tokens), 0), COALESCE(SUM(reasoning_tokens), 0) "
        "FROM cost_events WHERE event_type = 'llm_call' AND date(created_at) = ? "
        "GROUP BY model",
        (date_str,),
    )
    rows = await cur.fetchall()
    return {
        (model or "unknown"): {
            "calls": calls,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "cached_tokens": cached,
            "reasoning_tokens": reasoning,
        }
        for model, calls, pt, ct, cached, reasoning in rows
    }


async def get_transcription_count(date_str: str) -> int:
    """Voice-message transcription calls (config.OPENAI_TRANSCRIBE_MODEL) on a given calendar day."""
    cur = await conn().execute(
        "SELECT COUNT(*) FROM cost_events WHERE event_type = 'transcription' AND date(created_at) = ?",
        (date_str,),
    )
    row = await cur.fetchone()
    return row[0] if row else 0


async def get_server_tool_count(date_str: str) -> dict[str, int]:
    """Вызовы серверных инструментов xAI за день, по инструментам.

    Считаются строками (см. ai_trainer._log_server_tool_calls): у xAI это $5 за
    1000 вызовов СВЕРХ токенов, и в usage по токенам их нет вовсе — без этой
    выборки дневной отчёт занижал расход на всю эту статью.
    """
    cur = await conn().execute(
        "SELECT model, COUNT(*) FROM cost_events "
        "WHERE event_type = 'server_tool' AND date(created_at) = ? GROUP BY model",
        (date_str,),
    )
    return {(model or "unknown"): calls for model, calls in await cur.fetchall()}


async def get_cost_total_usd(date_str: Optional[str] = None) -> float:
    """Во сколько обошлись сутки — все платные вызовы, одной суммой.

    Без даты — за текущие сутки по UTC (так её спрашивает потолок в ai_limits),
    с датой — за конкретные (так её спрашивает ночной отчёт).

    Считается тем же `config.call_price_usd`, что и строка в логе на каждый
    вызов, и по тем же строкам, что читает отчёт: разойдись эти две цифры, и
    «бот выключил поиск» перестало бы сходиться с «в отчёте было $6».
    Агрегатом, а не построчно: зовётся перед каждым дорогим шагом.

    Цена линейна по токенам, поэтому сумма токенов, посчитанная по ставке
    модели, равна сумме цен по строкам — группировать нужно по модели И типу
    события, потому что у расшифровок и вызовов инструментов ставка плоская.
    """
    cur = await conn().execute(
        "SELECT event_type, model, COUNT(*), "
        "COALESCE(SUM(prompt_tokens), 0), COALESCE(SUM(completion_tokens), 0), "
        "COALESCE(SUM(cached_tokens), 0), COALESCE(SUM(reasoning_tokens), 0) "
        "FROM cost_events WHERE date(created_at) = ? GROUP BY event_type, model",
        (date_str or _utc_day(),),
    )
    total = 0.0
    for event_type, model, calls, prompt, completion, cached, reasoning in await cur.fetchall():
        if event_type == "transcription":
            total += calls * config.TRANSCRIPTION_PRICE_USD_PER_CALL
        elif event_type == "server_tool":
            total += calls * config.SERVER_TOOL_PRICE_USD_PER_CALL
        else:
            total += config.call_price_usd(model or "", prompt, completion, cached, reasoning)
    return total


async def prune_old_cost_events(retention_days: int) -> int:
    """Drop cost_events older than retention_days — only the daily report/backup job reads
    this table, and only ever one day back, so nothing needs it to grow forever."""
    cutoff = (dt.date.today() - dt.timedelta(days=retention_days)).isoformat()
    async with _write_lock:
        cur = await conn().execute("DELETE FROM cost_events WHERE date(created_at) < ?", (cutoff,))
        await conn().commit()
        return cur.rowcount


# ---------- лог действий пользователей ----------
#
# Пишется из activity_log.py на каждое входящее сообщение и нажатие, читается
# только админской панелью (/activity). Отсюда и форма запросов: всё «за
# пользователя», от свежего к старому.


async def log_user_event(
    telegram_id: int, kind: str, content: str, payload: Optional[str] = None
) -> None:
    async with _write_lock:
        await conn().execute(
            "INSERT INTO user_events (telegram_id, kind, content, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (telegram_id, kind, content, payload, now_iso()),
        )
        await conn().commit()


async def count_unhandled_callbacks_by_prefix(since_iso: str) -> list[aiosqlite.Row]:
    """Сколько раз каждый префикс callback_data долетал до fallback без
    обработчика с указанного момента — вспышка одного префикса на фоне
    остальных обычно значит регресс роутинга, а не просто протухшие кнопки."""
    cur = await conn().execute(
        "SELECT content AS prefix, COUNT(*) AS n FROM user_events "
        "WHERE kind = 'callback_unhandled' AND created_at >= ? "
        "GROUP BY content ORDER BY n DESC",
        (since_iso,),
    )
    return await cur.fetchall()


async def list_users_with_event_counts(limit: int = 10, offset: int = 0) -> list[aiosqlite.Row]:
    """Пользователи со счётчиком действий и временем последнего — самые активные сверху.

    LEFT JOIN, а не JOIN: тот, кто зашёл и не сделал ничего, — сам по себе
    ответ на вопрос «как пользуются», и пропадать из списка он не должен.
    """
    cur = await conn().execute(
        "SELECT u.telegram_id, u.username, COUNT(e.id) AS event_count, MAX(e.created_at) AS last_event_at "
        "FROM users u LEFT JOIN user_events e ON e.telegram_id = u.telegram_id "
        "GROUP BY u.telegram_id "
        "ORDER BY event_count DESC, u.telegram_id "
        "LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return await cur.fetchall()


async def count_user_events(telegram_id: int) -> int:
    cur = await conn().execute(
        "SELECT COUNT(*) FROM user_events WHERE telegram_id = ?", (telegram_id,)
    )
    (count,) = await cur.fetchone()
    return count


async def list_user_events(telegram_id: int, limit: int = 30, offset: int = 0) -> list[aiosqlite.Row]:
    """Действия одного пользователя, свежие сверху."""
    cur = await conn().execute(
        "SELECT id, kind, content, payload, created_at FROM user_events "
        "WHERE telegram_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
        (telegram_id, limit, offset),
    )
    return await cur.fetchall()


async def count_all_events() -> int:
    cur = await conn().execute("SELECT COUNT(*) FROM user_events")
    (count,) = await cur.fetchone()
    return count


async def list_all_events(limit: int = 30, offset: int = 0) -> list[aiosqlite.Row]:
    """Действия всех пользователей вперемешку, свежие сверху."""
    cur = await conn().execute(
        "SELECT e.id, e.telegram_id, u.username, e.kind, e.content, e.payload, e.created_at "
        "FROM user_events e LEFT JOIN users u ON u.telegram_id = e.telegram_id "
        "ORDER BY e.id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return await cur.fetchall()


async def prune_old_user_events(retention_days: int) -> int:
    """Выкинуть действия старше retention_days.

    Лог растёт быстрее всех остальных таблиц — по строке на каждое нажатие, — а
    смотрят в него всегда про недавнее: «что человек делал на этой неделе».
    """
    cutoff = (dt.date.today() - dt.timedelta(days=retention_days)).isoformat()
    async with _write_lock:
        cur = await conn().execute("DELETE FROM user_events WHERE date(created_at) < ?", (cutoff,))
        await conn().commit()
        return cur.rowcount


async def backup_to_file(dest_path: str) -> None:
    """Write a consistent snapshot of the live database to dest_path (must not already exist)."""
    async with _write_lock:
        await conn().execute("VACUUM INTO ?", (dest_path,))


# ---------- push notifications ----------

async def weekly_exercise_rollup(
    user_id: int, since_date: str, *, tz_offset: Optional[int] = None
) -> list[aiosqlite.Row]:
    """Per exercise since `since_date`: best set, its reps, total tonnage and
    set count — one row per exercise, heaviest tonnage first.

    Feeds the weekly summary table (see formatting.build_weekly_summary), so
    it's one query rather than a walk over every workout of the week.

    `since_date` — граница суток по местному времени (дата или дата с T00:00:00),
    поэтому сравнение идёт по местному дню тренировки: иначе тренировка вечера
    понедельника не попадала в таблицу той недели, которую сама же и открывала,
    хотя в счётчике тренировок рядом уже была.
    """
    day = _local_day("w.started_at", await _tz_offset_of(user_id, tz_offset))
    cur = await conn().execute(
        "SELECT e.display_name AS name, "
        f"       MAX({LOAD_WEIGHT_SQL}) AS top_weight, "
        "       SUM(COALESCE(s.load_weight, s.weight) * s.reps) AS tonnage, "
        "       COUNT(s.id) AS sets_count "
        "FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        "JOIN exercises e ON e.id = s.exercise_id "
        f"WHERE w.user_id = ? AND w.status = 'finished' AND {day} >= date(?) AND s.reps > 0 "
        "GROUP BY e.id ORDER BY tonnage DESC",
        (user_id, since_date),
    )
    return await cur.fetchall()


async def tonnage_since(
    user_id: int, since_date: str, *, tz_offset: Optional[int] = None
) -> float:
    """Total weight x reps across all finished-workout sets on/after since_date — for the weekly digest push.

    `since_date` приходит местной датой (дайджест считает окно от «сегодня»
    пользователя), поэтому и отсечка — по местному дню тренировки.
    """
    day = _local_day("w.started_at", await _tz_offset_of(user_id, tz_offset))
    cur = await conn().execute(
        "SELECT COALESCE(SUM(COALESCE(s.load_weight, s.weight) * s.reps), 0) FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id "
        f"WHERE w.user_id = ? AND w.status = 'finished' AND {day} >= date(?)",
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
#
# «Сегодня» здесь — сутки пользователя, а не сервера: дневная квота, которая
# обнуляется в 03:00 по местному времени, выглядит произвольной, а у кого-то
# сбрасывалась ровно посреди вечерней тренировки. Чтение и запись берут день
# одинаково, поэтому счётчик всегда попадает в ту же строку.


async def _quota_day(telegram_id: int) -> str:
    """Календарный день пользователя (YYYY-MM-DD) для дневных квот."""
    offset = await user_tz_offset(telegram_id)
    local_now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=offset)
    return local_now.date().isoformat()


async def get_ai_search_count_today(telegram_id: int) -> int:
    cur = await conn().execute(
        "SELECT count FROM ai_search_usage WHERE telegram_id = ? AND date = ?",
        (telegram_id, await _quota_day(telegram_id)),
    )
    row = await cur.fetchone()
    return row["count"] if row else 0


def _utc_day() -> str:
    """Сутки по UTC — для глобальных потолков, привязанных к счёту провайдера."""
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


async def get_ai_search_count_global(date_str: Optional[str] = None) -> int:
    """Сколько живых поисков сделали ВСЕ пользователи за сутки по UTC.

    Без даты — за текущие сутки (так её спрашивает потолок в ai_trainer), с
    датой — за конкретные (так её спрашивает дневной отчёт админу).
    """
    cur = await conn().execute(
        "SELECT count FROM ai_search_global_usage WHERE date = ?",
        (date_str or _utc_day(),),
    )
    row = await cur.fetchone()
    return row["count"] if row else 0


async def increment_ai_search_count(telegram_id: int) -> None:
    """Плюс один поиск — и в личный счётчик, и в общий, одной транзакцией.

    Оба счётчика двигаются вместе намеренно: разъедься они, и один из двух
    потолков начнёт врать, а какой именно — станет видно только по счёту от
    провайдера, то есть через месяц.
    """
    today = await _quota_day(telegram_id)
    async with _write_lock:
        await conn().execute(
            "INSERT INTO ai_search_usage (telegram_id, date, count) VALUES (?, ?, 1) "
            "ON CONFLICT (telegram_id, date) DO UPDATE SET count = count + 1",
            (telegram_id, today),
        )
        await conn().execute(
            "INSERT INTO ai_search_global_usage (date, count) VALUES (?, 1) "
            "ON CONFLICT (date) DO UPDATE SET count = count + 1",
            (_utc_day(),),
        )
        await conn().commit()


async def get_ai_question_count_today(telegram_id: int) -> int:
    cur = await conn().execute(
        "SELECT count FROM ai_question_usage WHERE telegram_id = ? AND date = ?",
        (telegram_id, await _quota_day(telegram_id)),
    )
    row = await cur.fetchone()
    return row["count"] if row else 0


async def increment_ai_question_count(telegram_id: int) -> None:
    today = await _quota_day(telegram_id)
    async with _write_lock:
        await conn().execute(
            "INSERT INTO ai_question_usage (telegram_id, date, count) VALUES (?, ?, 1) "
            "ON CONFLICT (telegram_id, date) DO UPDATE SET count = count + 1",
            (telegram_id, today),
        )
        await conn().commit()


async def get_ai_video_count_today(telegram_id: int) -> int:
    cur = await conn().execute(
        "SELECT count FROM ai_video_usage WHERE telegram_id = ? AND date = ?",
        (telegram_id, await _quota_day(telegram_id)),
    )
    row = await cur.fetchone()
    return row["count"] if row else 0


async def increment_ai_video_count(telegram_id: int) -> None:
    today = await _quota_day(telegram_id)
    async with _write_lock:
        await conn().execute(
            "INSERT INTO ai_video_usage (telegram_id, date, count) VALUES (?, ?, 1) "
            "ON CONFLICT (telegram_id, date) DO UPDATE SET count = count + 1",
            (telegram_id, today),
        )
        await conn().commit()


async def get_ai_question_count_month(telegram_id: int) -> int:
    """Сколько вопросов задано с первого числа текущего месяца пользователя.

    Месяц берётся из того же локального дня, что и дневная квота (_quota_day):
    иначе у человека в UTC+7 месячный счётчик обнулялся бы посреди дня, и
    «осталось вопросов» на экране расходилось бы с тем, что считает лимит.
    Суммируем по уже существующим дневным строкам — отдельного месячного
    счётчика нет намеренно: два счётчика одного и того же неизбежно разъезжаются.
    """
    month = (await _quota_day(telegram_id))[:7]
    cur = await conn().execute(
        "SELECT COALESCE(SUM(count), 0) FROM ai_question_usage "
        "WHERE telegram_id = ? AND date LIKE ?",
        (telegram_id, f"{month}-%"),
    )
    row = await cur.fetchone()
    return row[0] if row else 0


# ---------- Монетизация: звёзды, доступ, разовые паки ----------
#
# Витрина и правила — billing.py, экраны — handlers/billing.py. Здесь только
# факты: что оплачено, что потрачено и что вернули.


async def get_billing(telegram_id: int) -> dict:
    """Оплаченное состояние человека. Не платил — нули, а не None."""
    cur = await conn().execute(
        "SELECT pro_until, pack_questions FROM user_billing WHERE telegram_id = ?",
        (telegram_id,),
    )
    row = await cur.fetchone()
    if not row:
        return {"pro_until": None, "pack_questions": 0}
    return {"pro_until": row["pro_until"], "pack_questions": row["pack_questions"]}


async def record_star_payment(
    telegram_id: int, charge_id: str, product: str, stars: int, payload: str
) -> bool:
    """Записать оплату. False — такой charge_id уже есть, выдавать ничего не надо.

    Возврат — это и есть защита от двойной выдачи: Telegram может прислать
    successful_payment повторно, и решение «новый это платёж или тот же» должно
    приниматься в базе под тем же UNIQUE, а не сравнением в памяти.
    """
    async with _write_lock:
        cur = await conn().execute(
            "INSERT OR IGNORE INTO star_payments "
            "(telegram_id, charge_id, product, stars, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (telegram_id, charge_id, product, stars, payload, now_iso()),
        )
        await conn().commit()
        return cur.rowcount > 0


async def extend_pro(telegram_id: int, days: int) -> str:
    """Продлить доступ на days и вернуть новую дату окончания (UTC ISO).

    От максимума из «сейчас» и текущей даты: второй месяц, купленный до конца
    первого, добавляет срок, а не съедает остаток.
    """
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    current = await get_billing(telegram_id)
    base = now
    if current["pro_until"]:
        with suppress(ValueError):
            base = max(base, dt.datetime.fromisoformat(current["pro_until"]))
    until = (base + dt.timedelta(days=days)).isoformat(timespec="seconds")
    async with _write_lock:
        await conn().execute(
            "INSERT INTO user_billing (telegram_id, pro_until, pack_questions, updated_at) "
            "VALUES (?, ?, 0, ?) "
            "ON CONFLICT (telegram_id) DO UPDATE SET pro_until = excluded.pro_until, "
            "updated_at = excluded.updated_at",
            (telegram_id, until, now_iso()),
        )
        await conn().commit()
    return until


async def add_pack_questions(telegram_id: int, count: int) -> int:
    """Добавить разовые вопросы (отрицательное count — забрать) и вернуть остаток.

    Ниже нуля остаток не уходит: возврат пака, из которого уже отвечено,
    не должен оставлять человека в долгу.
    """
    async with _write_lock:
        await conn().execute(
            "INSERT INTO user_billing (telegram_id, pro_until, pack_questions, updated_at) "
            "VALUES (?, NULL, MAX(0, ?), ?) "
            "ON CONFLICT (telegram_id) DO UPDATE SET "
            "pack_questions = MAX(0, pack_questions + ?), updated_at = excluded.updated_at",
            (telegram_id, count, now_iso(), count),
        )
        await conn().commit()
    return (await get_billing(telegram_id))["pack_questions"]


async def consume_pack_question(telegram_id: int) -> bool:
    """Списать один разовый вопрос. False — списывать было нечего.

    Проверка и списание одним UPDATE ... WHERE pack_questions > 0: два вопроса,
    отправленные одновременно с последним оставшимся, иначе списали бы его
    дважды и увели остаток в минус.
    """
    async with _write_lock:
        cur = await conn().execute(
            "UPDATE user_billing SET pack_questions = pack_questions - 1, updated_at = ? "
            "WHERE telegram_id = ? AND pack_questions > 0",
            (now_iso(), telegram_id),
        )
        await conn().commit()
        return cur.rowcount > 0


async def get_star_payment(charge_id: str) -> Optional[dict]:
    cur = await conn().execute(
        "SELECT telegram_id, charge_id, product, stars, payload, refunded_at, created_at "
        "FROM star_payments WHERE charge_id = ?",
        (charge_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def mark_payment_refunded(charge_id: str) -> bool:
    """Отметить платёж возвращённым. False — платежа нет или он уже возвращён."""
    async with _write_lock:
        cur = await conn().execute(
            "UPDATE star_payments SET refunded_at = ? WHERE charge_id = ? AND refunded_at IS NULL",
            (now_iso(), charge_id),
        )
        await conn().commit()
        return cur.rowcount > 0


async def star_revenue(days: int) -> dict:
    """Звёзды и платежи за окно, без возвращённых — то, что реально осталось.

    plus сколько всего людей когда-либо платили: конверсия считается от них, а
    не от числа платежей (один человек продлевается много раз).
    """
    since = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=days)).isoformat()
    cur = await conn().execute(
        "SELECT COUNT(*), COALESCE(SUM(stars), 0), COUNT(DISTINCT telegram_id) "
        "FROM star_payments WHERE refunded_at IS NULL AND created_at >= ?",
        (since,),
    )
    payments, stars, buyers = await cur.fetchone()
    cur = await conn().execute(
        "SELECT COUNT(DISTINCT telegram_id) FROM star_payments WHERE refunded_at IS NULL"
    )
    (buyers_total,) = await cur.fetchone()
    return {
        "days": days,
        "payments": payments,
        "stars": stars,
        "buyers": buyers,
        "buyers_total": buyers_total,
    }


async def star_revenue_by_product(days: int) -> list[tuple[str, int, int]]:
    """(товар, число покупок, звёзды) за окно — что именно покупают."""
    since = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=days)).isoformat()
    cur = await conn().execute(
        "SELECT product, COUNT(*), COALESCE(SUM(stars), 0) FROM star_payments "
        "WHERE refunded_at IS NULL AND created_at >= ? GROUP BY product ORDER BY SUM(stars) DESC",
        (since,),
    )
    return [(r[0], r[1], r[2]) for r in await cur.fetchall()]

async def get_ai_food_count_today(telegram_id: int) -> int:
    cur = await conn().execute(
        "SELECT count FROM ai_food_usage WHERE telegram_id = ? AND date = ?",
        (telegram_id, await _quota_day(telegram_id)),
    )
    row = await cur.fetchone()
    return row["count"] if row else 0


async def increment_ai_food_count(telegram_id: int) -> None:
    today = await _quota_day(telegram_id)
    async with _write_lock:
        await conn().execute(
            "INSERT INTO ai_food_usage (telegram_id, date, count) VALUES (?, ?, 1) "
            "ON CONFLICT (telegram_id, date) DO UPDATE SET count = count + 1",
            (telegram_id, today),
        )
        await conn().commit()


# ---------- «Понятно» на предупреждении о лимите (см. ai_limits.py) ----------


async def has_limit_ack(telegram_id: int, kind: str, date_str: str) -> bool:
    cur = await conn().execute(
        "SELECT 1 FROM ai_limit_ack WHERE telegram_id = ? AND kind = ? AND date = ?",
        (telegram_id, kind, date_str),
    )
    return await cur.fetchone() is not None


async def record_limit_ack(telegram_id: int, kind: str, date_str: str) -> None:
    async with _write_lock:
        await conn().execute(
            "INSERT OR IGNORE INTO ai_limit_ack (telegram_id, kind, date) VALUES (?, ?, ?)",
            (telegram_id, kind, date_str),
        )
        await conn().commit()


async def prune_old_limit_acks(keep_days: int = 7) -> int:
    """Расписки старше недели. Читаются они только за сегодня, но таблица иначе
    растёт вечно — как и cost_events, чистим в том же ночном джобе."""
    cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
    async with _write_lock:
        cur = await conn().execute("DELETE FROM ai_limit_ack WHERE date < ?", (cutoff,))
        await conn().commit()
        return cur.rowcount


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


async def get_bodyweight_log(log_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute("SELECT * FROM bodyweight_logs WHERE id = ?", (log_id,))
    return await cur.fetchone()


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


async def delete_bodyweight_log(log_id: int, telegram_id: int) -> bool:
    """Убрать одну конкретную запись веса — ту, а не последнюю.

    delete_last_bodyweight хватает экрану 🏋️ Вес, где отменяют только что
    набранное. Откату записи, сделанной AI-тренером, — нет: между записью и
    тапом по «↩️ Отменить» человек мог взвеситься ещё раз руками, и снос
    последней утащил бы не то. Отсюда id — и проверка владельца при нём: id
    приезжает из callback_data, то есть от клиента.
    """
    async with _write_lock:
        cur = await conn().execute(
            "DELETE FROM bodyweight_logs WHERE id = ? AND telegram_id = ?",
            (log_id, telegram_id),
        )
        await conn().commit()
        return cur.rowcount > 0


async def delete_exercise_if_unused(exercise_id: int, user_id: int) -> bool:
    """Снести упражнение целиком — только если по нему ещё ничего не записано.

    Откат «создай упражнение»: раз его завели секунду назад, сносим по-честному,
    а не архивируем — архив копил бы мусор, которого пользователь не заводил.
    Но если между созданием и откатом по нему уже успели сделать подход или
    поставить его в программу, удаление утащило бы за собой чужие данные:
    тогда отказываемся, и вызывающая сторона говорит об этом вслух.
    """
    exercise = await get_exercise(exercise_id)
    if exercise is None or exercise["user_id"] != user_id or exercise["is_template"]:
        return False
    if await list_sets_for_exercise(exercise_id):
        return False
    cur = await conn().execute(
        "SELECT 1 FROM routine_exercises WHERE exercise_id = ? LIMIT 1", (exercise_id,)
    )
    if await cur.fetchone():
        return False
    async with _write_lock:
        db = conn()
        await db.execute("DELETE FROM exercises WHERE id = ?", (exercise_id,))
        await db.commit()
    return True


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
        # Снимок собственного веса живёт в тех же единицах, что и вес подхода,
        # поэтому конвертируется вместе с ним — иначе после смены кг↔lb
        # подтягивания разом «потяжелели» бы вдвое.
        await conn().execute(
            "UPDATE sets SET load_weight = ROUND(load_weight * ?, 1) "
            "WHERE load_weight IS NOT NULL AND block_id IN ("
            "  SELECT b.id FROM workout_blocks b JOIN workouts w ON w.id = b.workout_id "
            "  WHERE w.user_id = ?)",
            (factor, telegram_id),
        )
        await conn().commit()


async def scale_progression_steps(user_id: int, factor: float) -> None:
    """Пересчитать `step` в правилах прогрессии программ при смене кг↔lb.

    step хранится внутри JSON `routine_exercises.progression` в единицах
    пользователя. scale_user_set_weights конвертирует историю подходов, а без
    этого прохода сохранённое «+2.5 кг» после перехода на фунты молча
    превращалось бы в «+2.5 lb» — на порядок меньший шаг.
    """
    cur = await conn().execute(
        "SELECT re.id, re.progression FROM routine_exercises re "
        "JOIN routines r ON r.id = re.routine_id "
        "WHERE r.user_id = ? AND re.progression IS NOT NULL",
        (user_id,),
    )
    rows = await cur.fetchall()
    updates: list[tuple[str, int]] = []
    for row in rows:
        try:
            rule = json.loads(row["progression"])
        except (TypeError, ValueError):
            # Правило пишет модель — мусор в нём не должен ронять смену единиц;
            # подсказка веса такой JSON и так игнорирует (progression_rule_for_workout).
            continue
        step = rule.get("step") if isinstance(rule, dict) else None
        if not isinstance(step, (int, float)) or isinstance(step, bool):
            continue
        rule["step"] = round(step * factor, 2)
        updates.append((json.dumps(rule, ensure_ascii=False), row["id"]))
    if not updates:
        return
    async with _write_lock:
        await conn().executemany(
            "UPDATE routine_exercises SET progression = ? WHERE id = ?", updates
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


async def get_announcement_text_hash(key: str) -> str | None:
    """Отпечаток текста, показанного админу на превью, или None."""
    cur = await conn().execute("SELECT text_hash FROM announcement_state WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row["text_hash"] if row else None


async def get_announcement_status(key: str) -> str | None:
    """Где сейчас разовая рассылка: None (ещё не показывали админу), 'preview',
    'approved' или 'declined'. См. announcements.py."""
    cur = await conn().execute("SELECT status FROM announcement_state WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row["status"] if row else None


async def set_announcement_status(key: str, status: str, text_hash: str | None = None) -> None:
    async with _write_lock:
        await conn().execute(
            "INSERT INTO announcement_state (key, status, updated_at, text_hash) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET status = excluded.status, "
            "updated_at = excluded.updated_at, "
            # Отпечаток обновляет только показ превью; «одобрено»/«отклонено»
            # приходят без него и затирать уже записанный не должны.
            "text_hash = COALESCE(excluded.text_hash, announcement_state.text_hash)",
            (key, status, now_iso(), text_hash),
        )
        await conn().commit()


async def has_announcement_push(telegram_id: int, category: str) -> bool:
    """Уходила ли этому человеку разовая рассылка `category`."""
    cur = await conn().execute(
        "SELECT 1 FROM pushes WHERE telegram_id = ? AND category = ? LIMIT 1",
        (telegram_id, category),
    )
    return await cur.fetchone() is not None


async def count_announcement_recipients(category: str) -> int:
    cur = await conn().execute(
        "SELECT COUNT(*) FROM users "
        "WHERE pushes_enabled = 1 "
        "AND telegram_id NOT IN (SELECT telegram_id FROM pushes WHERE category = ?) "
        "AND created_at <= COALESCE("
        "  (SELECT updated_at FROM announcement_state WHERE key = ? AND status = 'approved'),"
        "  created_at)",
        (category, category),
    )
    (count,) = await cur.fetchone()
    return count


async def list_announcement_recipients(category: str) -> list[int]:
    """Кому ещё не уходила разовая рассылка `category` (см. announcements.py).

    Отметка о доставке — строка в `pushes`, то есть та же таблица, что и у
    ежедневных пушей: рассылка переживает перезапуск контейнера, докатку и
    повторный деплой, а человек получает релизное сообщение ровно один раз.
    Выключивший пуши в настройках сюда не попадает: разовая рассылка — это
    тоже пуш, и тумблер должен значить то, что обещает.

    Живой прогон: рассылку одобрили давно, а спустя недели релиз продолжал
    капать новым атлетам — тем, для кого это не новость, а то, с чем они
    впервые встретились. Cutoff по `created_at <= (момент одобрения)`:
    зарегистрировался раньше — застал релиз, зарегистрировался позже —
    просто увидел готовую фичу, рассказывать ему о ней как о новинке нечего.
    Пока рассылка ещё не одобрена (approved-строки нет), фильтр не режет
    никого — это ровно то число, что видит админ на превью.
    """
    cur = await conn().execute(
        "SELECT telegram_id FROM users "
        "WHERE pushes_enabled = 1 "
        "AND telegram_id NOT IN (SELECT telegram_id FROM pushes WHERE category = ?) "
        "AND created_at <= COALESCE("
        "  (SELECT updated_at FROM announcement_state WHERE key = ? AND status = 'approved'),"
        "  created_at) "
        "ORDER BY telegram_id",
        (category, category),
    )
    return [row["telegram_id"] for row in await cur.fetchall()]


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


# ---------- мини-игра «Кач-Раннер» ----------


async def save_game_result(telegram_id: int, distance: int, score: int, fighter: str) -> None:
    async with _write_lock:
        await conn().execute(
            "INSERT INTO game_results (telegram_id, distance, score, fighter, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (telegram_id, distance, score, fighter, now_iso()),
        )
        await conn().commit()


async def get_game_best_distance(telegram_id: int) -> int:
    cur = await conn().execute(
        "SELECT MAX(distance) FROM game_results WHERE telegram_id = ?", (telegram_id,)
    )
    (best,) = await cur.fetchone()
    return best or 0
