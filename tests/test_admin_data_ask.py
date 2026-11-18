"""/data — вопросы по данным: запись и секреты держит сама SQLite, цикл с моделью."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import admin_data_ask
import ai_trainer
import config
import db as dbmod
from handlers import admin
from tests.luna_fakes import ResponsesScript


@pytest.fixture
async def file_db(tmp_path):
    """База в файле: /data читает её отдельным соединением только на чтение,
    а :memory: другому соединению не видна."""
    dbmod._write_lock = asyncio.Lock()
    path = str(tmp_path / "data.db")
    await dbmod.init_db(path)
    admin_data_ask._schema_cache.clear()
    try:
        yield path
    finally:
        await dbmod.close_db()


def _result(path, sql):
    return json.loads(admin_data_ask.run_sql(path, sql))


async def test_select_works_and_rows_are_capped(file_db, monkeypatch):
    monkeypatch.setattr(admin_data_ask, "MAX_ROWS", 3)
    for uid in range(1, 6):
        await dbmod.get_or_create_user(uid, f"u{uid}", "ru")
    out = _result(file_db, "SELECT telegram_id FROM users ORDER BY telegram_id")
    assert out["columns"] == ["telegram_id"]
    assert out["rows"] == [[1], [2], [3]]
    assert out["truncated"] is True

    agg = _result(file_db, "WITH t AS (SELECT 1 AS x UNION ALL SELECT 2) SELECT sum(x) FROM t")
    assert agg["rows"] == [[3]]


@pytest.mark.parametrize("sql", [
    "INSERT INTO users (telegram_id) VALUES (42)",
    "UPDATE users SET username = 'x'",
    "DELETE FROM users",
    "DROP TABLE users",
    "PRAGMA table_info(users)",
    "ATTACH DATABASE ':memory:' AS other",
    "CREATE TABLE t (x)",
])
async def test_anything_but_select_is_refused(file_db, sql):
    await dbmod.get_or_create_user(7, "u7", "ru")
    out = _result(file_db, sql)
    assert "error" in out
    rows = _result(file_db, "SELECT count(*) FROM users")["rows"]
    assert rows == [[1]]


async def test_secret_tables_refused_and_secret_columns_read_as_null(file_db):
    for table in sorted(admin_data_ask.SECRET_TABLES):
        out = _result(file_db, f"SELECT * FROM {table}")
        assert "prohibited" in out.get("error", "") or "not authorized" in out.get("error", ""), table

    await dbmod.conn().execute(
        "CREATE TABLE IF NOT EXISTS probe (id INTEGER, email TEXT, token TEXT, note TEXT)"
    )
    await dbmod.conn().execute("INSERT INTO probe VALUES (1, 'a@b.c', 'sekret', 'ok')")
    await dbmod.conn().commit()
    out = _result(file_db, "SELECT * FROM probe")
    assert out["rows"] == [[1, None, None, "ok"]]
    assert "sekret" not in admin_data_ask.run_sql(file_db, "SELECT token || '' FROM probe")


async def test_slow_query_is_cut(file_db, monkeypatch):
    monkeypatch.setattr(admin_data_ask, "QUERY_TIMEOUT_SECONDS", 0.05)
    out = _result(
        file_db,
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c",
    )
    assert "дольше" in out["error"]


def test_schema_hides_secret_tables(tmp_path):
    import sqlite3

    path = str(tmp_path / "s.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (telegram_id INTEGER)")
    con.execute("CREATE TABLE api_tokens (token TEXT)")
    con.commit()
    con.close()
    admin_data_ask._schema_cache.clear()
    schema = admin_data_ask.schema_text(path)
    assert "users" in schema and "api_tokens" not in schema


def _call_body(sql: str) -> dict:
    return {
        "status": "completed",
        "output": [{"type": "function_call", "call_id": "c1", "name": "run_sql",
                    "arguments": json.dumps({"sql": sql})}],
        "usage": {"input_tokens": 9000, "output_tokens": 80},
    }


def _text_body(text: str) -> dict:
    return {
        "status": "completed",
        "output": [{"type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}],
        "usage": {"input_tokens": 9500, "output_tokens": 120},
    }


async def test_ask_runs_the_models_sql_and_returns_its_answer(file_db, monkeypatch):
    await dbmod.get_or_create_user(11, "a", "ru")
    await dbmod.get_or_create_user(12, "b", "en")
    client = ResponsesScript([
        _call_body("SELECT lang, count(*) FROM users GROUP BY lang ORDER BY lang"),
        _text_body("Атлетов 2: en — 1, ru — 1."),
    ])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await admin_data_ask.ask(1, "сколько атлетов по языкам?")

    assert answer == "Атлетов 2: en — 1, ru — 1."
    first, second = client.requests
    assert first["tools"][0]["name"] == "run_sql"
    assert "CREATE TABLE" in first["input"][0]["content"]
    assert "api_tokens" not in first["input"][0]["content"]
    tool_out = next(i for i in second["input"] if i.get("type") == "function_call_output")
    assert json.loads(tool_out["output"])["rows"] == [["en", 1], ["ru", 1]]
    async with dbmod.conn().execute("SELECT count(*) FROM cost_events WHERE user_id = 1") as cur:
        assert (await cur.fetchone())[0] == 2


async def test_ask_respects_the_money_hard_stop(file_db, monkeypatch):
    async def _blocked():
        return SimpleNamespace(preview=False)

    monkeypatch.setattr(admin_data_ask.ai_limits, "hard_stop_block", _blocked)
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: pytest.fail("model must not be called"))
    assert "потолок" in await admin_data_ask.ask(1, "что угодно")


def _message(user_id: int):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    status = MagicMock()
    status.delete = AsyncMock()
    message.answer = AsyncMock(return_value=status)
    return message


async def test_data_command_is_admin_only_and_needs_a_question(file_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 1)
    ask = AsyncMock(return_value="42")
    monkeypatch.setattr(admin_data_ask, "ask", ask)

    stranger = _message(2)
    await admin.cmd_data(stranger, SimpleNamespace(args="сколько?"))
    stranger.answer.assert_not_awaited()

    empty = _message(1)
    await admin.cmd_data(empty, SimpleNamespace(args=None))
    assert "/data" in empty.answer.await_args.args[0]
    ask.assert_not_awaited()

    msg = _message(1)
    await admin.cmd_data(msg, SimpleNamespace(args="сколько атлетов?"))
    ask.assert_awaited_once_with(1, "сколько атлетов?")
    assert msg.answer.await_args.args[0] == "42"
