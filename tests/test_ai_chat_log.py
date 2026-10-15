"""Журнал ходов AI-тренера для проверки качества (ai_chat_log.py, db.ai_turn_log)
и админская /aichats (handlers/admin.py).

Что держим: ход бота и ход приложения ложатся одной строкой с вопросом,
ответом, моделью, инструментами и ценой; разговор с моделью от журнала не
зависит; журнал виден только админу, листается, выгружается файлом, живёт
AI_CONVERSATION_RETENTION_DAYS и сносится вместе с аккаунтом.
"""

import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import ai_chat_log
import ai_trainer
import api_v1
import config
import db
from handlers import admin
from handlers import ai_trainer as ai_chat

pytestmark = pytest.mark.asyncio

ADMIN = 900
ATHLETE = 111

_WIRE = [
    {"role": "user", "content": "старый вопрос"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "old_tool", "arguments": "{}"}},
    ]},
    {"role": "tool", "tool_call_id": "a", "content": "{}"},
    {"role": "assistant", "content": "старый ответ"},
    {"role": "user", "content": "как мой жим?"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "b", "type": "function", "function": {"name": "get_workouts", "arguments": "{}"}},
        {"id": "c", "type": "function", "function": {"name": "get_records", "arguments": "{}"}},
    ]},
    {"role": "tool", "tool_call_id": "b", "content": "[]"},
    {"role": "tool", "tool_call_id": "c", "content": "[]"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "d", "type": "function", "function": {"name": "get_workouts", "arguments": "{}"}},
    ]},
    {"role": "tool", "tool_call_id": "d", "content": "[]"},
    {"role": "assistant", "content": "Жим растёт."},
]


# ---------- ai_chat_log ----------


async def test_tool_names_are_only_from_the_last_turn_and_unique():
    assert ai_chat_log.tool_names(_WIRE) == ["get_workouts", "get_records"]
    assert ai_chat_log.tool_names(None) == []
    assert ai_chat_log.tool_names([{"role": "user", "content": "x"}]) == []


async def test_record_turn_takes_model_and_cost_from_this_turn_only(fresh_db, user_id):
    await db.log_cost_event(user_id, "llm_call", model="old-model", prompt_tokens=10)
    started = db.now_iso()
    await db.log_cost_event(user_id, "llm_call", model=config.OPENAI_CHAT_MODEL, prompt_tokens=1000, completion_tokens=200)
    await db.log_cost_event(user_id, "llm_call", model=config.OPENAI_CHAT_MODEL, prompt_tokens=1200, completion_tokens=50)
    await db.log_cost_event(user_id, "server_tool", model="web_search")
    # Ход другого человека в ту же секунду — не наш.
    await db.log_cost_event(222, "llm_call", model="someone-else", prompt_tokens=10)
    # Предыдущий вызов был в ту же секунду — отодвигаем его назад.
    await db.conn().execute(
        "UPDATE cost_events SET created_at = '2020-01-01T00:00:00' WHERE model = 'old-model'"
    )

    await ai_chat_log.record_turn(
        user_id, source=ai_chat_log.SOURCE_BOT, question="как мой жим?",
        answer="Жим растёт.", wire=_WIRE, started_at=started,
    )

    (row,) = await db.get_ai_turn_log(user_id)
    assert row["source"] == "bot"
    assert row["question"] == "как мой жим?"
    assert row["answer"] == "Жим растёт."
    assert row["model"] == config.OPENAI_CHAT_MODEL
    assert json.loads(row["tools"]) == ["get_workouts", "get_records", "web_search"]
    expected = (
        config.call_price_usd(config.OPENAI_CHAT_MODEL, 1000, 200)
        + config.call_price_usd(config.OPENAI_CHAT_MODEL, 1200, 50)
        + config.server_tool_price_usd("web_search")
    )
    assert row["cost_usd"] == pytest.approx(expected, rel=1e-4)


async def test_record_turn_never_breaks_the_turn(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(db, "add_ai_turn_log", AsyncMock(side_effect=RuntimeError("disk full")))
    await ai_chat_log.record_turn(
        user_id, source="bot", question="q", answer="a", wire=None, started_at=db.now_iso(),
    )


# ---------- бот: ход → строка ----------


def _chat_message(user_id: int, text: str):
    message = MagicMock()
    message.text = text
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.reply = AsyncMock()
    placeholder = MagicMock()
    placeholder.edit_text = AsyncMock()
    placeholder.chat = SimpleNamespace(id=user_id)
    placeholder.message_id = 9
    message.answer = AsyncMock(return_value=placeholder)
    message.bot = AsyncMock()
    return message


async def _chat_state(user_id: int) -> FSMContext:
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state("AITrainerFlow:chatting")
    return state


async def test_bot_turn_lands_in_the_log_and_history_still_feeds_the_model(fresh_db, user_id, monkeypatch):
    seen_histories: list = []

    async def fake_ask(uid, question, history, **kwargs):
        seen_histories.append(list(history))
        await db.log_cost_event(uid, "llm_call", model=config.OPENAI_CHAT_MODEL, prompt_tokens=500, completion_tokens=100)
        wire = [*history, {"role": "user", "content": question}]
        if question == "как мой жим?":
            wire += _WIRE[5:]
        else:
            wire += [{"role": "assistant", "content": "Тяга тоже."}]
        await kwargs["on_wire"](wire)
        return wire[-1]["content"]

    monkeypatch.setattr(ai_chat.ai_trainer, "ask", fake_ask)
    state = await _chat_state(user_id)

    await ai_chat.ai_question(_chat_message(user_id, "как мой жим?"), state)
    await ai_chat.ai_question(_chat_message(user_id, "а тяга?"), state)

    newest, first = await db.get_ai_turn_log(user_id)
    assert (first["source"], first["question"], first["answer"]) == ("bot", "как мой жим?", "Жим растёт.")
    assert first["model"] == config.OPENAI_CHAT_MODEL
    assert json.loads(first["tools"]) == ["get_workouts", "get_records"]
    assert first["cost_usd"] == pytest.approx(config.call_price_usd(config.OPENAI_CHAT_MODEL, 500, 100), rel=1e-4)
    assert (newest["question"], newest["answer"], json.loads(newest["tools"])) == ("а тяга?", "Тяга тоже.", [])
    # Модель по-прежнему получает историю из FSM — wire прошлого хода целиком.
    assert seen_histories[0] == []
    assert seen_histories[1][-1] == {"role": "assistant", "content": "Жим растёт."}
    assert (await state.get_data())["ai_history"][-1] == {"role": "assistant", "content": "Тяга тоже."}


async def test_failed_bot_turn_is_not_logged(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_chat.ai_trainer, "ask", AsyncMock(side_effect=RuntimeError("down")))
    await ai_chat.ai_question(_chat_message(user_id, "как жим?"), await _chat_state(user_id))
    assert await db.count_ai_turn_log(user_id) == 0


# ---------- приложение: ход → строка ----------


async def test_app_turn_lands_in_the_same_log(fresh_db, monkeypatch):
    await fresh_db.get_or_create_user(telegram_id=ATHLETE, username="tester")
    code = await fresh_db.issue_oauth_link_code(ATHLETE, ttl_seconds=600, digits=8)

    async def fake_ask(uid, question, history, on_wire=None, **kwargs):
        await db.log_cost_event(uid, "llm_call", model=config.OPENAI_CHAT_MODEL, prompt_tokens=300)
        await on_wire([{"role": "user", "content": question}, *_WIRE[5:]])
        return "Жим растёт."

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    transport = httpx.ASGITransport(app=api_v1.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token = (await client.post("/auth/link", json={"code": code})).json()["token"]
        client.headers["Authorization"] = f"Bearer {token}"
        resp = await client.post("/ai/ask", json={"question": "как мой жим?"})
        assert resp.status_code == 200, resp.text

    (row,) = await db.get_ai_turn_log(ATHLETE)
    assert (row["source"], row["question"], row["answer"]) == ("app", "как мой жим?", "Жим растёт.")
    assert row["model"] == config.OPENAI_CHAT_MODEL
    assert json.loads(row["tools"]) == ["get_workouts", "get_records"]
    # Разговор приложения живёт в своей таблице, как и раньше.
    assert [t["question"] for t in await db.get_ai_conversation_history(ATHLETE)] == ["как мой жим?"]


# ---------- срок и снос ----------


async def test_retention_drops_only_old_turns(fresh_db, user_id):
    await db.add_ai_turn_log(user_id, "bot", "старый", "ответ")
    await db.add_ai_turn_log(user_id, "app", "свежий", "ответ")
    old = (dt.date.today() - dt.timedelta(days=config.AI_CONVERSATION_RETENTION_DAYS + 1)).isoformat()
    await db.conn().execute("UPDATE ai_turn_log SET created_at = ? WHERE question = 'старый'", (old + "T12:00:00",))
    await db.conn().commit()

    assert await db.prune_old_ai_turn_log(config.AI_CONVERSATION_RETENTION_DAYS) == 1
    assert [r["question"] for r in await db.get_ai_turn_log(user_id)] == ["свежий"]


async def test_nightly_cleanup_runs_the_log_retention(fresh_db, monkeypatch, tmp_path):
    import admin_tasks

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "training_log.db"))
    spy = AsyncMock(return_value=0)
    monkeypatch.setattr(db, "prune_old_ai_turn_log", spy)
    await admin_tasks._run_retention_cleanup()
    spy.assert_awaited_once_with(config.AI_CONVERSATION_RETENTION_DAYS)


async def test_account_deletion_wipes_the_log(fresh_db, user_id):
    await fresh_db.get_or_create_user(telegram_id=222, username="other")
    await db.add_ai_turn_log(user_id, "bot", "мой вопрос", "ответ")
    await db.add_ai_turn_log(222, "app", "чужой вопрос", "ответ")

    await db.wipe_user_account(user_id)

    assert await db.count_ai_turn_log(user_id) == 0
    assert await db.count_ai_turn_log(222) == 1


# ---------- /aichats ----------


def _admin_message(user_id: int):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.answer = AsyncMock()
    return message


def _callback(user_id: int, data: str):
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id)
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer_document = AsyncMock()
    return callback


def _buttons(markup) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


@pytest.fixture
def as_admin(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN)
    monkeypatch.setattr(config, "ADMIN_TZ_OFFSET", 0)
    edits = AsyncMock()
    monkeypatch.setattr(admin.ui, "safe_edit", edits)
    return edits


async def _seed_turns(user_id: int, count: int, source: str = "bot") -> None:
    for i in range(count):
        await db.add_ai_turn_log(
            user_id, source, f"вопрос {i}", f"ответ {i}",
            model=config.OPENAI_CHAT_MODEL, tools=["get_workouts"], cost_usd=0.0012,
        )


async def test_non_admin_sees_nothing(fresh_db, user_id, as_admin):
    await _seed_turns(user_id, 3)
    message = _admin_message(user_id)
    await admin.cmd_aichats(message, state=MagicMock())
    message.answer.assert_not_awaited()

    for data in ("admin:aic:p:0", f"admin:aic:u:{user_id}:0:0", f"admin:aic:x:{user_id}"):
        callback = _callback(user_id, data)
        handler = {
            "p": admin.admin_aichats_page, "u": admin.admin_aichats_user, "x": admin.admin_aichats_export,
        }[data.split(":")[2]]
        await handler(callback, state=MagicMock())
        callback.message.answer_document.assert_not_awaited()
    as_admin.assert_not_awaited()


async def test_list_shows_users_with_counts_source_and_model(fresh_db, as_admin):
    await fresh_db.get_or_create_user(telegram_id=ATHLETE, username="athlete")
    await fresh_db.get_or_create_user(telegram_id=222, username=None)
    # Ход старше недели считается во «всего», но не в «за 7 дн.».
    await db.add_ai_turn_log(ATHLETE, "bot", "давний", "ответ")
    await db.conn().execute("UPDATE ai_turn_log SET created_at = '2020-01-01T00:00:00' WHERE question = 'давний'")
    await db.conn().commit()
    await _seed_turns(ATHLETE, 2, "bot")
    await _seed_turns(ATHLETE, 1, "app")
    await _seed_turns(222, 1, "app")

    message = _admin_message(ADMIN)
    await admin.cmd_aichats(message, state=MagicMock())

    text = message.answer.await_args.args[0]
    assert "@athlete — 3 за 7 дн. (всего 4)" in text
    assert "приложение+бот" in text and config.OPENAI_CHAT_MODEL in text
    assert "222 — 1 за 7 дн." in text
    buttons = _buttons(message.answer.await_args.kwargs["reply_markup"])
    assert ("@athlete (3/7д)", f"admin:aic:u:{ATHLETE}:0:0") in buttons


async def test_list_paginates(fresh_db, as_admin):
    for uid in range(1000, 1000 + admin.AICHATS_USERS_PAGE_SIZE + 2):
        await fresh_db.get_or_create_user(telegram_id=uid, username=f"u{uid}")
        await _seed_turns(uid, 1)

    message = _admin_message(ADMIN)
    await admin.cmd_aichats(message, state=MagicMock())
    first = _buttons(message.answer.await_args.kwargs["reply_markup"])
    assert ("Дальше ▶️", "admin:aic:p:1") in first
    assert sum(data.startswith("admin:aic:u:") for _, data in first) == admin.AICHATS_USERS_PAGE_SIZE

    await admin.admin_aichats_page(_callback(ADMIN, "admin:aic:p:1"), state=MagicMock())
    second = _buttons(as_admin.await_args.kwargs["reply_markup"])
    assert sum(data.startswith("admin:aic:u:") for _, data in second) == 2
    assert ("◀️ Назад", "admin:aic:p:0") in second


async def test_user_turns_page_newest_first_with_older_newer(fresh_db, user_id, as_admin):
    await _seed_turns(user_id, admin.AICHATS_TURNS_PAGE_SIZE + 1)

    await admin.admin_aichats_user(_callback(ADMIN, f"admin:aic:u:{user_id}:0:3"), state=MagicMock())
    text = as_admin.await_args.args[1]
    buttons = _buttons(as_admin.await_args.kwargs["reply_markup"])
    newest = admin.AICHATS_TURNS_PAGE_SIZE
    assert f"👤 вопрос {newest}" in text and "👤 вопрос 0\n" not in text
    # По порядку разговора: раньше заданный — выше.
    assert text.index(f"вопрос {newest - 1}") < text.index(f"вопрос {newest}")
    assert "бот" in text and "🛠 get_workouts" in text and "$0.0012" in text and config.OPENAI_CHAT_MODEL in text
    assert ("◀️ Старше", f"admin:aic:u:{user_id}:1:3") in buttons
    assert ("↩️ К списку", "admin:aic:p:3") in buttons
    assert not any(label == "Новее ▶️" for label, _ in buttons)

    await admin.admin_aichats_user(_callback(ADMIN, f"admin:aic:u:{user_id}:1:3"), state=MagicMock())
    text = as_admin.await_args.args[1]
    buttons = _buttons(as_admin.await_args.kwargs["reply_markup"])
    assert "👤 вопрос 0" in text and f"вопрос {newest}" not in text
    assert ("Новее ▶️", f"admin:aic:u:{user_id}:0:3") in buttons
    assert not any(label == "◀️ Старше" for label, _ in buttons)


async def test_long_turns_fit_in_one_telegram_message(fresh_db, user_id, as_admin):
    for _ in range(admin.AICHATS_TURNS_PAGE_SIZE):
        await db.add_ai_turn_log(user_id, "bot", "в" * 3000, "о" * 6000, model="m", tools=["t"] * 1)

    await admin.admin_aichats_user(_callback(ADMIN, f"admin:aic:u:{user_id}:0:0"), state=MagicMock())
    text = as_admin.await_args.args[1]
    assert len(text) <= 4096
    assert text.count("👤") == admin.AICHATS_TURNS_PAGE_SIZE
    assert text.count("🤖") == admin.AICHATS_TURNS_PAGE_SIZE + 1  # + заголовок


async def test_export_sends_a_file_with_full_turns(fresh_db, user_id, as_admin):
    await _seed_turns(user_id, 3)
    await db.add_ai_turn_log(user_id, "app", "длинный вопрос", "х" * 9000, model="m")

    callback = _callback(ADMIN, f"admin:aic:x:{user_id}")
    await admin.admin_aichats_export(callback, state=MagicMock())

    document = callback.message.answer_document.await_args.args[0]
    assert document.filename.startswith(f"aichats_{user_id}_") and document.filename.endswith(".md")
    body = document.data.decode("utf-8")
    assert "@tester" in body
    assert body.index("вопрос 0") < body.index("вопрос 2") < body.index("длинный вопрос")
    assert "х" * 9000 in body  # без обрезки
    assert "приложение" in body and "get_workouts" in body


async def test_export_is_capped(fresh_db, user_id, as_admin, monkeypatch):
    monkeypatch.setattr(admin, "AICHATS_EXPORT_TURNS", 3)
    await _seed_turns(user_id, 5)

    callback = _callback(ADMIN, f"admin:aic:x:{user_id}")
    await admin.admin_aichats_export(callback, state=MagicMock())

    body = callback.message.answer_document.await_args.args[0].data.decode("utf-8")
    assert body.count("**Вопрос:**") == 3
    assert "вопрос 4" in body and "вопрос 1\n" not in body


async def test_export_without_turns_says_so(fresh_db, user_id, as_admin):
    callback = _callback(ADMIN, f"admin:aic:x:{user_id}")
    await admin.admin_aichats_export(callback, state=MagicMock())
    callback.message.answer_document.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get("show_alert") is True
