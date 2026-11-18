"""/data — вопросы по данным у модели с доступом к базе (только админ).

Владелец: «сделаем админ функцию, когда можно спрашивать вопросы по данным у
модели с доступом ко всей базе». Модель (та же Luna, главный цикл через
Responses API) получает схему базы и один инструмент — `run_sql`: SELECT по
отдельному соединению, открытому только на чтение. Считает она сама, сколько
нужно раундов, и отвечает цифрами из результатов запросов.

Что держит запись и секреты — не промпт, а сама SQLite:
- соединение `mode=ro` (URI) — писать нечем даже при обходе авторизатора;
- `set_authorizer` пускает только SELECT/READ/функции и рекурсию CTE, всё
  остальное (INSERT, PRAGMA, ATTACH, …) — отказ;
- таблицы с ключами входа и токенами (`SECRET_TABLES`) — отказ на чтение,
  столбцы-секреты и почта (`SECRET_COLUMNS`) читаются как NULL: в OpenAI такие
  значения не уезжают;
- `set_progress_handler` обрывает запрос дольше `QUERY_TIMEOUT_SECONDS`;
- строк в ответ модели — не больше `MAX_ROWS`, символов — `MAX_RESULT_CHARS`.

Цена каждого раунда пишется тем же `_log_llm_cost` от лица админа, HARD-стоп
по деньгам (`ai_limits.hard_stop_block`) держит и этот вход. Аудитория — один
человек (ADMIN_ID), тексты по-русски, как вся админка.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from typing import Any, Optional

import ai_limits
import ai_trainer
import db

logger = logging.getLogger(__name__)

MAX_ROUNDS = 8
MAX_ROWS = 200
MAX_RESULT_CHARS = 20_000
QUERY_TIMEOUT_SECONDS = 15.0

# Ключи входа, токены, коды OAuth — ни строки отсюда модели не нужно.
SECRET_TABLES = frozenset({
    "mcp_tokens", "api_tokens", "auth_identities", "push_tokens",
    "oauth_clients", "oauth_consent_requests", "oauth_auth_codes", "oauth_tokens",
    "oauth_link_codes", "oauth_consent_failures",
})
# Столбцы с секретами и личными контактами в остальных таблицах.
SECRET_COLUMNS = frozenset({
    "token", "access_token", "refresh_token", "client_secret", "device_token",
    "email", "code", "code_challenge",
})

_ALLOWED_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
})

SQL_TOOL = {
    "type": "function",
    "function": {
        "name": "run_sql",
        "description": (
            "Выполнить один SELECT (SQLite) по базе дневника, только чтение. "
            f"Вернёт столбцы и до {MAX_ROWS} строк. Агрегируй в SQL, а не строками."
        ),
        "parameters": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "Один SELECT или WITH … SELECT."}},
            "required": ["sql"],
        },
    },
}


def _authorizer(action: int, arg1: Optional[str], arg2: Optional[str], _db: Any, _trigger: Any) -> int:
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ:
        if arg1 in SECRET_TABLES:
            return sqlite3.SQLITE_DENY
        if arg2 in SECRET_COLUMNS:
            # NULL вместо значения: `SELECT * FROM users` работает, секрет не едет.
            return sqlite3.SQLITE_IGNORE
    return sqlite3.SQLITE_OK


def _connect_ro(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    con.set_authorizer(_authorizer)
    return con


def run_sql(db_path: str, sql: str) -> str:
    """Выполнить запрос модели и вернуть ей результат строкой (JSON).

    Ошибка — тоже ответ модели, а не исключение: увидев «not authorized» или
    опечатку в имени столбца, она переписывает запрос сама."""
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    con = _connect_ro(db_path)
    con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cur = con.execute(sql)
        columns = [d[0] for d in cur.description or ()]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except sqlite3.Error as exc:
        if time.monotonic() > deadline:
            return json.dumps({"error": f"запрос дольше {QUERY_TIMEOUT_SECONDS:.0f} с — упрости или сузь"},
                              ensure_ascii=False)
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    finally:
        con.close()
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    out = {"columns": columns, "rows": [list(r) for r in rows], "row_count": len(rows), "truncated": truncated}
    text = json.dumps(out, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + ' …"обрезано по длине — агрегируй или возьми меньше столбцов"'
    return text


_schema_cache: dict[str, str] = {}


def schema_text(db_path: str) -> str:
    """CREATE TABLE всех таблиц, кроме секретных; секретные столбцы помечены.
    Схема за жизнь процесса не меняется (миграции — на старте), кэш по пути."""
    cached = _schema_cache.get(db_path)
    if cached is not None:
        return cached
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        con.close()
    parts = [sql for name, sql in rows if name not in SECRET_TABLES]
    text = "\n\n".join(parts)
    _schema_cache[db_path] = text
    return text


async def _db_path() -> str:
    """Путь к файлу той же базы, что открыта у бота (тесты — свой временный)."""
    async with db.conn().execute("PRAGMA database_list") as cur:
        for row in await cur.fetchall():
            if row[1] == "main":
                return row[2]
    raise RuntimeError("main database path not found")


def _system_prompt(schema: str, own_ids: set[int]) -> str:
    own = ", ".join(str(i) for i in sorted(own_ids)) or "нет"
    return (
        "Ты аналитик данных Telegram-бота и iOS-приложения «Дневник атлета» (дневник тренировок с "
        "AI-тренером). Отвечаешь владельцу на вопросы по базе SQLite. Данные бери только из "
        "инструмента run_sql — никаких цифр из головы. Сначала пойми схему, потом запрашивай.\n\n"
        "Правила:\n"
        "- Агрегируй в SQL (COUNT, GROUP BY, date()), не тащи сырые строки.\n"
        "- Даты в базе — ISO-строки (UTC, если не сказано иное); сутки — date(столбец).\n"
        "- users.telegram_id — id атлета; у app-only атлетов он тоже есть. Свои аккаунты "
        f"(админ, тест, демо) исключай из поведенческих выводов, если не спросили иначе: {own}.\n"
        "- Продуктовые события — analytics_events; суточная сводка — daily_metrics; лента "
        "действий для чтения глазами — user_events.\n"
        "- Таблицы с токенами закрыты (отказ «prohibited»/«not authorized» — не баг), столбцы с секретами и "
        "почтой всегда NULL. Обойдись без них.\n"
        "- Ответ по-русски, коротко, обычным текстом без Markdown: сначала прямой ответ с "
        "цифрами, потом 1–3 строки, как считал (и чем ограничен результат). Если данных для "
        "ответа нет — так и скажи.\n\n"
        f"Схема базы:\n{schema}"
    )


async def ask(admin_id: int, question: str) -> str:
    """Ответ на вопрос по данным. Ошибки модели летят наверх — их ловит хендлер."""
    block = await ai_limits.hard_stop_block()
    if block is not None:
        return "Суточный потолок по деньгам достигнут — модель сегодня молчит, и этот вопрос тоже."
    path = await _db_path()
    schema = await asyncio.to_thread(schema_text, path)
    own_ids = await db.own_account_ids()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt(schema, own_ids)},
        {"role": "user", "content": question},
    ]
    backend = ai_trainer.luna_backend()
    for _ in range(MAX_ROUNDS):
        text, calls, items = await ai_trainer._responses_round(
            backend, messages, admin_id, None,
            include_tools=True, tools=[SQL_TOOL], cache_scope="admin-data",
        )
        if not calls:
            return text.strip() or "Модель ответила пустотой — переформулируй вопрос."
        messages.append({
            "role": "assistant", "content": text or None,
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in calls
            ],
            **ai_trainer._reasoning_fields(items),
        })
        for call in calls:
            try:
                sql = json.loads(call.function.arguments or "{}").get("sql") or ""
            except (TypeError, ValueError):
                sql = ""
            if call.function.name != "run_sql" or not sql.strip():
                result = json.dumps({"error": "нужен run_sql с полем sql"}, ensure_ascii=False)
            else:
                logger.info("admin /data SQL: %s", sql[:500])
                result = await asyncio.to_thread(run_sql, path, sql)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    return "Не уложился в раунды запросов — сузь вопрос."
