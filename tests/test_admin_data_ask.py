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

    answer, history = await admin_data_ask.ask(1, "сколько атлетов по языкам?")

    assert answer == "Атлетов 2: en — 1, ru — 1."
    assert history[0] == {"role": "user", "content": "сколько атлетов по языкам?"}
    assert history[-1]["content"] == answer
    first, second = client.requests
    assert first["tools"][0]["name"] == "run_sql"
    assert "CREATE TABLE" in first["input"][0]["content"]
    assert "api_tokens" not in first["input"][0]["content"]
    tool_out = next(i for i in second["input"] if i.get("type") == "function_call_output")
    assert json.loads(tool_out["output"])["rows"] == [["en", 1], ["ru", 1]]
    async with dbmod.conn().execute("SELECT count(*) FROM cost_events WHERE user_id = 1") as cur:
        assert (await cur.fetchone())[0] == 2
    # Отвечает старшая модель, а не модель тренера.
    assert first["model"] == config.OPENAI_ADMIN_DATA_MODEL != config.OPENAI_CHAT_MODEL


async def test_ask_falls_back_to_luna_when_the_senior_model_fails(file_db, monkeypatch):
    client = ResponsesScript([RuntimeError("model not available"), _text_body("Ответила Luna.")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer, _history = await admin_data_ask.ask(1, "сколько атлетов?")

    assert answer == "Ответила Luna."
    assert [r["model"] for r in client.requests] == [config.OPENAI_ADMIN_DATA_MODEL, config.OPENAI_CHAT_MODEL]


async def test_ask_respects_the_money_hard_stop(file_db, monkeypatch):
    async def _blocked():
        return SimpleNamespace(preview=False)

    monkeypatch.setattr(admin_data_ask.ai_limits, "hard_stop_block", _blocked)
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: pytest.fail("model must not be called"))
    answer, history = await admin_data_ask.ask(1, "что угодно", [{"role": "user", "content": "x"}])
    assert "потолок" in answer
    assert history == [{"role": "user", "content": "x"}]


async def test_follow_up_question_sees_the_previous_turn(file_db, monkeypatch):
    await dbmod.get_or_create_user(11, "a", "ru")
    client = ResponsesScript([
        _call_body("SELECT count(*) FROM users"),
        _text_body("Атлетов: 1."),
        _text_body("Из них на iOS: 0."),
    ])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    _, history = await admin_data_ask.ask(1, "сколько атлетов?")
    answer, history = await admin_data_ask.ask(1, "а на iOS?", history)

    assert answer == "Из них на iOS: 0."
    third = client.requests[2]["input"]
    texts = [i.get("content") for i in third if i.get("role") in ("user", "assistant")]
    assert texts[0] == "сколько атлетов?" and texts[-1] == "а на iOS?"
    assert "Атлетов: 1." in json.dumps(third, ensure_ascii=False)
    assert any(i.get("type") == "function_call_output" for i in third)


def test_trim_drops_whole_old_questions(monkeypatch):
    monkeypatch.setattr(admin_data_ask, "HISTORY_MAX_CHARS", 300)
    history = []
    for n in range(5):
        history += [
            {"role": "user", "content": f"вопрос {n}"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{n}"}]},
            {"role": "tool", "tool_call_id": f"c{n}", "content": "x" * 60},
            {"role": "assistant", "content": f"ответ {n}"},
        ]
    trimmed = admin_data_ask._trim_history(history)
    assert trimmed[0]["role"] == "user"
    assert trimmed[-1] == {"role": "assistant", "content": "ответ 4"}
    assert len(trimmed) < len(history)


async def test_round_cap_does_not_keep_dangling_calls(file_db, monkeypatch):
    monkeypatch.setattr(admin_data_ask, "MAX_ROUNDS", 2)
    client = ResponsesScript([_call_body("SELECT 1"), _call_body("SELECT 2")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    answer, history = await admin_data_ask.ask(1, "?", [])
    assert "раунды" in answer and history == []


def _message(user_id: int):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    status = MagicMock()
    status.delete = AsyncMock()
    message.answer = AsyncMock(return_value=status)
    return message


async def _state():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


async def test_data_command_opens_a_dialog_with_memory(file_db, monkeypatch):
    from fsm import AdminFlow

    monkeypatch.setattr(config, "ADMIN_ID", 1)
    seen = []

    async def _ask(admin_id, question, history):
        seen.append((question, list(history)))
        return f"ответ на «{question}»", [*history, {"role": "user", "content": question},
                                          {"role": "assistant", "content": "ok"}]

    monkeypatch.setattr(admin_data_ask, "ask", _ask)

    stranger = _message(2)
    state = await _state()
    await admin.cmd_data(stranger, SimpleNamespace(args="сколько?"), state)
    stranger.answer.assert_not_awaited()
    assert await state.get_state() is None

    opened = _message(1)
    await admin.cmd_data(opened, SimpleNamespace(args=None), state)
    assert await state.get_state() == AdminFlow.data_dialog.state
    assert "Диалог по данным открыт" in opened.answer.await_args.args[0]
    assert seen == []

    first = _message(1)
    first.text = "сколько атлетов?"
    await admin.data_dialog_question(first, state)
    second = _message(1)
    second.text = "а на iOS?"
    await admin.data_dialog_question(second, state)

    assert seen[0] == ("сколько атлетов?", [])
    assert seen[1][0] == "а на iOS?" and seen[1][1][0]["content"] == "сколько атлетов?"
    assert second.answer.await_args.args[0] == "ответ на «а на iOS?»"

    cb = MagicMock()
    cb.from_user = SimpleNamespace(id=1)
    cb.message = _message(1)
    cb.answer = AsyncMock()
    await admin.data_dialog_new(cb, state)
    assert not (await state.get_data()).get("admin_data_history")
    await admin.data_dialog_stop(cb, state)
    assert await state.get_state() is None


def test_system_prompt_carries_the_data_map():
    """19.11: модель ответила «iOS-пуши не выделить» — iOS-пуши лежат в
    analytics_events (push_sent/push_open), а pushes с channel NULL — не пуши."""
    prompt = admin_data_ask._system_prompt("CREATE TABLE x (y)", {1})
    assert "push_sent" in prompt and "push_open" in prompt
    assert "channel IS NULL" in prompt
    assert prompt.index("Карта данных") < prompt.index("Схема базы")
