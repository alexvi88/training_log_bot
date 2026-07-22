"""AI-тренер: tool-executor'ы поверх реальной БД и агентный цикл с фейковым Grok-клиентом."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ai_trainer
import config

pytestmark = pytest.mark.asyncio


async def _seed_bench_history(db, user_id: int, n_sessions: int = 3, exercise: str = "Жим лёжа") -> int:
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, exercise, group_id)
    for i in range(1, n_sessions + 1):
        workout_id = await db.create_finished_workout(
            user_id, started_at=f"2026-01-{i:02d}T10:00:00", finished_at=f"2026-01-{i:02d}T10:30:00"
        )
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.add_set(block_id, ex_id, round_index=1, order_in_round=0, weight=100.0 + i, reps=8)
    return ex_id


# ---------- tool executors ----------

async def test_overview_reports_stats_and_exercises(fresh_db, user_id):
    await _seed_bench_history(fresh_db, user_id, 3)

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_training_overview", {}))

    assert payload["unit"] == "kg"
    assert payload["stats"]["total_workouts"] == 3
    names = [e["name"] for e in payload["exercises"]]
    assert "Жим лёжа" in names


async def test_recent_workouts_lists_sets(fresh_db, user_id):
    await _seed_bench_history(fresh_db, user_id, 3)

    payload = json.loads(
        await ai_trainer.execute_tool(user_id, "list_recent_workouts", {"limit": 2})
    )

    assert len(payload["workouts"]) == 2
    latest = payload["workouts"][0]
    assert latest["date"] == "2026-01-03"
    assert latest["exercises"][0] == {"name": "Жим лёжа", "sets": ["103x8"]}


async def test_recent_workouts_includes_rpe_in_set_string(fresh_db, user_id):
    group_id = await fresh_db.create_muscle_group(user_id, "Грудь")
    ex_id = await fresh_db.create_exercise(user_id, "Жим", group_id)
    wid = await fresh_db.create_finished_workout(user_id, "2026-02-01T10:00:00", "2026-02-01T10:30:00")
    block_id = await fresh_db.create_block(wid, "single")
    await fresh_db.add_block_exercise(block_id, ex_id, 0)
    await fresh_db.add_set(block_id, ex_id, 1, 0, 100.0, 8, rpe=9.0)
    await fresh_db.add_set(block_id, ex_id, 2, 0, 100.0, 7)  # no rpe

    payload = json.loads(await ai_trainer.execute_tool(user_id, "list_recent_workouts", {}))
    assert payload["workouts"][0]["exercises"][0]["sets"] == ["100x8@9", "100x7"]


async def test_overview_includes_latest_bodyweight(fresh_db, user_id):
    await fresh_db.add_bodyweight_log(user_id, 81.5, logged_at="2026-03-01T08:00:00")
    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_training_overview", {}))
    assert payload["latest_bodyweight"] == {"weight": 81.5, "date": "2026-03-01"}


async def test_bodyweight_history_returns_full_log(fresh_db, user_id):
    await fresh_db.add_bodyweight_log(user_id, 82.0, logged_at="2026-01-01T08:00:00")
    await fresh_db.add_bodyweight_log(user_id, 81.5, logged_at="2026-02-01T08:00:00")

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_bodyweight_history", {}))

    assert payload["entries"] == [
        {"weight": 82.0, "date": "2026-01-01"},
        {"weight": 81.5, "date": "2026-02-01"},
    ]


async def test_bodyweight_history_does_not_leak_other_users_data(fresh_db, user_id):
    other = await fresh_db.get_or_create_user(telegram_id=222, username="other")
    await fresh_db.add_bodyweight_log(other["telegram_id"], 90.0, logged_at="2026-01-01T08:00:00")

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_bodyweight_history", {}))

    assert payload["entries"] == []


async def test_weekly_volume_tool_counts_and_classifies(fresh_db, user_id, monkeypatch):
    import datetime as dt

    # Freeze "today" so the seeded workout lands in the current week.
    class _FixedDate(dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 15)  # Wednesday

    monkeypatch.setattr(ai_trainer.dt, "date", _FixedDate)

    group_id = (await fresh_db.list_muscle_groups(None, global_only=True))[0]["id"]
    ex_id = await fresh_db.create_exercise(user_id, "Жим", group_id)
    wid = await fresh_db.create_finished_workout(user_id, "2026-07-14T10:00:00", "2026-07-14T11:00:00")
    block_id = await fresh_db.create_block(wid, "single")
    await fresh_db.add_block_exercise(block_id, ex_id, 0)
    for i in range(6):
        await fresh_db.add_set(block_id, ex_id, i + 1, 0, 100.0, 8)

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_weekly_volume_by_group", {}))
    by_group = {g["group"]: g for g in payload["groups"]}
    target_group = (await fresh_db.get_muscle_group(group_id))["name"]
    assert by_group[target_group]["sets"] == 6
    assert by_group[target_group]["status"] == "in_range"


async def test_recent_workouts_clamps_limit(fresh_db, user_id):
    await _seed_bench_history(fresh_db, user_id, 2)

    payload = json.loads(
        await ai_trainer.execute_tool(user_id, "list_recent_workouts", {"limit": 999})
    )

    assert len(payload["workouts"]) == 2


async def test_full_workout_history_is_not_capped_at_ten(fresh_db, user_id):
    """Unlike list_recent_workouts, this tool must not clip at the recent-window size."""
    await _seed_bench_history(fresh_db, user_id, 12)

    recent = json.loads(await ai_trainer.execute_tool(user_id, "list_recent_workouts", {}))
    full = json.loads(await ai_trainer.execute_tool(user_id, "get_full_workout_history", {}))

    assert len(recent["workouts"]) == 5  # default limit, unaffected by this change
    assert len(full["workouts"]) == 12
    assert full["workouts"][-1]["date"] == "2026-01-01"  # oldest last, same ordering as list_recent_workouts


async def test_exercise_progress_returns_sessions_and_records(fresh_db, user_id):
    await _seed_bench_history(fresh_db, user_id, 3)

    payload = json.loads(
        await ai_trainer.execute_tool(user_id, "get_exercise_progress", {"exercise_name": "Жим лёжа"})
    )

    assert payload["total_sessions"] == 3
    assert payload["sessions"][-1]["sets"] == ["103x8"]
    assert payload["records"]["max_weight"] == 103.0
    assert payload["e1rm_trend_per_week"] > 0


async def test_exercise_progress_unknown_name_suggests_candidates(fresh_db, user_id):
    await _seed_bench_history(fresh_db, user_id, 1)

    payload = json.loads(
        await ai_trainer.execute_tool(user_id, "get_exercise_progress", {"exercise_name": "жим"})
    )

    assert "error" in payload
    assert "Жим лёжа" in payload["did_you_mean"]


async def test_unknown_tool_returns_error(fresh_db, user_id):
    payload = json.loads(await ai_trainer.execute_tool(user_id, "drop_tables", {}))
    assert "error" in payload


async def test_full_chat_history_returns_own_messages_chronologically(fresh_db, user_id):
    await fresh_db.add_ai_chat_message(user_id, "user", "первый вопрос")
    await fresh_db.add_ai_chat_message(user_id, "assistant", "первый ответ")
    await fresh_db.add_ai_chat_message(user_id, "user", "второй вопрос")

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_full_chat_history", {}))

    assert [m["content"] for m in payload["messages"]] == [
        "первый вопрос",
        "первый ответ",
        "второй вопрос",
    ]
    assert payload["messages"][0]["role"] == "user"


async def test_full_chat_history_empty_when_no_messages(fresh_db, user_id):
    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_full_chat_history", {}))
    assert payload["messages"] == []


# ---------- изоляция данных между пользователями ----------
#
# Единственный идентификатор пользователя в ask()/execute_tool() приходит из
# Telegram (message.from_user.id) — ни один инструмент не принимает user_id
# параметром, так что модель (и пользователь через промпт-инъекцию) не может
# запросить чужие данные. Тесты ниже фиксируют это поведение на каждом
# инструменте.

async def test_overview_does_not_leak_other_users_data(fresh_db, user_id):
    other = await fresh_db.get_or_create_user(telegram_id=222, username="other")
    await _seed_bench_history(fresh_db, other["telegram_id"], 2)

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_training_overview", {}))

    assert payload["stats"]["total_workouts"] == 0
    assert payload["exercises"] == []


async def test_recent_workouts_does_not_leak_other_users_data(fresh_db, user_id):
    other = await fresh_db.get_or_create_user(telegram_id=222, username="other")
    await _seed_bench_history(fresh_db, other["telegram_id"], 3)

    payload = json.loads(await ai_trainer.execute_tool(user_id, "list_recent_workouts", {}))

    assert payload["workouts"] == []


async def test_full_workout_history_does_not_leak_other_users_data(fresh_db, user_id):
    other = await fresh_db.get_or_create_user(telegram_id=222, username="other")
    await _seed_bench_history(fresh_db, other["telegram_id"], 12)

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_full_workout_history", {}))

    assert payload["workouts"] == []


async def test_exercise_progress_cannot_read_other_users_exercise(fresh_db, user_id):
    """Даже зная точное название чужого упражнения, получить его историю нельзя."""
    other = await fresh_db.get_or_create_user(telegram_id=222, username="other")
    await _seed_bench_history(fresh_db, other["telegram_id"], 3, exercise="Секретный жим")

    payload = json.loads(
        await ai_trainer.execute_tool(user_id, "get_exercise_progress", {"exercise_name": "Секретный жим"})
    )

    assert "error" in payload
    assert payload["did_you_mean"] == []  # и в подсказках чужого тоже нет


async def test_full_chat_history_does_not_leak_other_users_data(fresh_db, user_id):
    other = await fresh_db.get_or_create_user(telegram_id=222, username="other")
    await fresh_db.add_ai_chat_message(other["telegram_id"], "user", "у меня травма плеча")
    await fresh_db.add_ai_chat_message(other["telegram_id"], "assistant", "сочувствую, к врачу")

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_full_chat_history", {}))

    assert payload["messages"] == []


# ---------- agentic loop (_ask_plain — REST/OpenAI-compatible) ----------
#
# ask() always answers through _ask_plain now, optionally preceded by a
# _web_search_findings step (see the "search step" section below); these
# tests exercise the REST tool-calling loop directly so they don't depend on
# quota state or the search step.

def _tool_call(name, arguments, call_id="call_1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _response(content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _fake_client(responses):
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(side_effect=responses))
        )
    )


async def test_ask_runs_tool_round_and_returns_text(fresh_db, user_id, monkeypatch):
    await _seed_bench_history(fresh_db, user_id, 1)

    client = _fake_client([
        _response(tool_calls=[_tool_call("get_training_overview", {})]),
        _response(content="Ты молодец, продолжай!"),
    ])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await ai_trainer._ask_plain(user_id, "Как мои дела?", history=[])

    assert answer == "Ты молодец, продолжай!"
    create = client.chat.completions.create
    assert create.await_count == 2
    # Второй запрос несёт результат инструмента обратно модели.
    second_messages = create.await_args_list[1].kwargs["messages"]
    tool_msg = second_messages[-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_1"
    assert "total_workouts" in tool_msg["content"]


async def test_ask_tool_failure_is_reported_to_model(fresh_db, user_id, monkeypatch):
    client = _fake_client([
        _response(tool_calls=[_tool_call("get_exercise_progress", {"exercise_name": "Жим"})]),
        _response(content="ответ"),
    ])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    async def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(ai_trainer, "execute_tool", boom)

    answer = await ai_trainer._ask_plain(user_id, "Прогресс?", history=[])

    assert answer == "ответ"
    second_messages = client.chat.completions.create.await_args_list[1].kwargs["messages"]
    tool_msg = second_messages[-1]
    assert tool_msg["role"] == "tool"
    assert "error" in tool_msg["content"]


async def test_ask_stops_after_max_tool_rounds(fresh_db, user_id, monkeypatch):
    endless = _response(tool_calls=[_tool_call("get_training_overview", {})])
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=endless))
        )
    )
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await ai_trainer._ask_plain(user_id, "Как дела?", history=[])

    assert client.chat.completions.create.await_count == ai_trainer.MAX_TOOL_ROUNDS + 1
    assert answer  # даём осмысленный fallback, а не пустую строку


async def test_ask_passes_user_question_and_history(fresh_db, user_id, monkeypatch):
    client = _fake_client([_response(content="ок")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    history = [
        {"role": "user", "content": "прошлый вопрос"},
        {"role": "assistant", "content": "прошлый ответ"},
    ]
    await ai_trainer._ask_plain(user_id, "новый вопрос", history=history)

    messages = client.chat.completions.create.await_args.kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1:] == [*history, {"role": "user", "content": "новый вопрос"}]


# ---------- search step (_web_search_findings, server-side-only web/X search) ----------
#
# _web_search_findings never mixes our DB function-tools with the multi-agent
# search model (that combination needs xAI beta access this account doesn't
# have — see the module docstring) — it only ever passes web_search/x_search,
# so there's no client-side tool round trip to simulate here, unlike the
# _ask_plain tests above.

def _xai_response(content=None, citations=None, server_side_tool_usage=None):
    return SimpleNamespace(
        content=content,
        citations=citations or [],
        server_side_tool_usage=server_side_tool_usage or {},
    )


def _fake_sdk_client(response):
    session = SimpleNamespace(sample=AsyncMock(return_value=response), create_kwargs=None)

    def create(**kwargs):
        session.create_kwargs = kwargs
        return session

    client = SimpleNamespace(chat=SimpleNamespace(create=create))
    client.session = session
    return client


async def test_ask_uses_search_context_and_counts_quota_when_search_used(fresh_db, user_id, monkeypatch):
    sdk_client = _fake_sdk_client(
        _xai_response(content="нашёл свежее исследование по протеину", citations=["http://example.com"])
    )
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=sdk_client))
    client = _fake_client([_response(content="YES"), _response(content="финальный ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await ai_trainer.ask(user_id, "Что нового в исследованиях по протеину?", history=[])

    assert answer == "финальный ответ"
    assert await fresh_db.get_ai_search_count_today(user_id) == 1
    messages = client.chat.completions.create.await_args.kwargs["messages"]
    assert any("нашёл свежее исследование по протеину" in m["content"] for m in messages if m["role"] == "system")


async def test_ask_skips_search_context_when_search_unused(fresh_db, user_id, monkeypatch):
    sdk_client = _fake_sdk_client(_xai_response(content="NO_SEARCH_NEEDED"))
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=sdk_client))
    client = _fake_client([_response(content="YES"), _response(content="обычный ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await ai_trainer.ask(user_id, "Как мои дела?", history=[])

    assert answer == "обычный ответ"
    assert await fresh_db.get_ai_search_count_today(user_id) == 0
    messages = client.chat.completions.create.await_args.kwargs["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]


async def test_ask_skips_expensive_search_when_gate_says_no(fresh_db, user_id, monkeypatch):
    # Дешёвый гейт сказал NO → дорогая multi-agent модель не поднимается вовсе.
    sdk_getter = AsyncMock()
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", sdk_getter)
    client = _fake_client([_response(content="NO"), _response(content="обычный ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await ai_trainer.ask(user_id, "Как мои дела?", history=[])

    assert answer == "обычный ответ"
    sdk_getter.assert_not_awaited()
    assert await fresh_db.get_ai_search_count_today(user_id) == 0


async def test_ask_skips_search_step_once_quota_exhausted(fresh_db, user_id, monkeypatch):
    for _ in range(config.AI_SEARCH_DAILY_LIMIT):
        await fresh_db.increment_ai_search_count(user_id)

    client = _fake_client([_response(content="обычный ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    sdk_getter = AsyncMock()
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", sdk_getter)

    answer = await ai_trainer.ask(user_id, "Вопрос", history=[])

    assert answer == "обычный ответ"
    sdk_getter.assert_not_awaited()


async def test_ask_answers_normally_when_search_step_raises(fresh_db, user_id, monkeypatch):
    async def boom():
        raise RuntimeError("xAI search model rejected client-side tools (no beta access)")

    session = SimpleNamespace(sample=boom)
    sdk_client = SimpleNamespace(chat=SimpleNamespace(create=lambda **kwargs: session))
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=sdk_client))
    client = _fake_client([_response(content="YES"), _response(content="обычный ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await ai_trainer.ask(user_id, "Вопрос", history=[])

    assert answer == "обычный ответ"
    assert await fresh_db.get_ai_search_count_today(user_id) == 0


async def test_ask_logs_question_and_search_usage(fresh_db, user_id, monkeypatch, caplog):
    sdk_client = _fake_sdk_client(_xai_response(content="находки", citations=["http://example.com"]))
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=sdk_client))
    client = _fake_client([_response(content="YES"), _response(content="ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    with caplog.at_level(logging.INFO, logger="ai_trainer"):
        await ai_trainer.ask(user_id, "Что нового в исследованиях?", history=[])

    [record] = [r for r in caplog.records if "AI trainer question" in r.message]
    message = record.getMessage()
    assert "Что нового в исследованиях?" in message
    assert "web search used: True" in message


async def test_ask_logs_question_without_search_usage(fresh_db, user_id, monkeypatch, caplog):
    sdk_client = _fake_sdk_client(_xai_response(content="NO_SEARCH_NEEDED"))
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=sdk_client))
    client = _fake_client([_response(content="YES"), _response(content="ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    with caplog.at_level(logging.INFO, logger="ai_trainer"):
        await ai_trainer.ask(user_id, "Как мои дела?", history=[])

    [record] = [r for r in caplog.records if "AI trainer question" in r.message]
    message = record.getMessage()
    assert "Как мои дела?" in message
    assert "web search used: False" in message


async def test_web_search_findings_passes_only_server_side_tools(fresh_db, user_id, monkeypatch):
    sdk_client = _fake_sdk_client(_xai_response(content="находки", citations=["http://example.com"]))
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=sdk_client))

    findings = await ai_trainer._web_search_findings(user_id, "Вопрос", history=[])

    assert findings == "находки"
    assert sdk_client.session.create_kwargs["model"] == config.GROK_SEARCH_MODEL
    # только web_search + x_search — ни одного нашего DB-инструмента, иначе
    # это снова смешивание client-side tools с multi-agent моделью, требующее беты.
    assert len(sdk_client.session.create_kwargs["tools"]) == 2


async def test_search_worth_it_uses_fast_model_and_parses_verdict(fresh_db, user_id, monkeypatch):
    client = _fake_client([_response(content="YES")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    assert await ai_trainer._search_worth_it(user_id, "Что нового?", history=[]) is True
    # Гейт должен идти на дешёвую модель, а не на дорогую multi-agent.
    assert client.chat.completions.create.await_args.kwargs["model"] == config.GROK_MODEL


async def test_search_worth_it_returns_false_on_no_and_on_error(fresh_db, user_id, monkeypatch):
    no_client = _fake_client([_response(content="NO")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: no_client)
    assert await ai_trainer._search_worth_it(user_id, "Как мои дела?", history=[]) is False

    def boom():
        raise RuntimeError("api down")

    err_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=boom)))
    )
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: err_client)
    # Ошибка гейта не должна валить ответ — просто не ищем.
    assert await ai_trainer._search_worth_it(user_id, "Вопрос", history=[]) is False


# ---------- image input (text+img and img-only questions) ----------

_FAKE_IMAGE_DATA_URL = "data:image/jpeg;base64,Zm9vYmFy"


async def test_ask_plain_sends_multimodal_content_when_image_present(fresh_db, user_id, monkeypatch):
    client = _fake_client([_response(content="вижу фото")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await ai_trainer._ask_plain(
        user_id, "что на фото?", history=[], image_data_url=_FAKE_IMAGE_DATA_URL
    )

    assert answer == "вижу фото"
    messages = client.chat.completions.create.await_args.kwargs["messages"]
    user_content = messages[-1]["content"]
    assert user_content == [
        {"type": "text", "text": "что на фото?"},
        {"type": "image_url", "image_url": {"url": _FAKE_IMAGE_DATA_URL}},
    ]


async def test_ask_plain_sends_plain_text_content_without_image(fresh_db, user_id, monkeypatch):
    client = _fake_client([_response(content="ок")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    await ai_trainer._ask_plain(user_id, "просто текст", history=[])

    messages = client.chat.completions.create.await_args.kwargs["messages"]
    assert messages[-1]["content"] == "просто текст"


async def test_to_xai_messages_includes_image_content():
    messages = ai_trainer._to_xai_messages([], "что на фото?", _FAKE_IMAGE_DATA_URL)

    last = messages[-1]
    assert [c.WhichOneof("content") for c in last.content] == ["text", "image_url"]
    assert last.content[0].text == "что на фото?"
    assert last.content[1].image_url.image_url == _FAKE_IMAGE_DATA_URL


async def test_to_xai_messages_text_only_without_image():
    messages = ai_trainer._to_xai_messages([], "просто текст")

    last = messages[-1]
    assert [c.WhichOneof("content") for c in last.content] == ["text"]


# ---------- voice input (transcribe_voice / is_voice_configured) ----------


async def test_is_voice_configured_reflects_openai_key(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    assert ai_trainer.is_voice_configured() is False

    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    assert ai_trainer.is_voice_configured() is True


async def test_transcribe_voice_returns_stripped_text(monkeypatch):
    response = SimpleNamespace(text="  привет тренер  ")
    client = SimpleNamespace(
        audio=SimpleNamespace(transcriptions=SimpleNamespace(create=AsyncMock(return_value=response)))
    )
    monkeypatch.setattr(ai_trainer, "_get_audio_client", lambda: client)

    text = await ai_trainer.transcribe_voice(SimpleNamespace(name="voice.ogg"))

    assert text == "привет тренер"
    kwargs = client.audio.transcriptions.create.await_args.kwargs
    assert kwargs["model"] == config.OPENAI_TRANSCRIBE_MODEL


async def test_transcribe_voice_returns_empty_string_when_blank(monkeypatch):
    response = SimpleNamespace(text=None)
    client = SimpleNamespace(
        audio=SimpleNamespace(transcriptions=SimpleNamespace(create=AsyncMock(return_value=response)))
    )
    monkeypatch.setattr(ai_trainer, "_get_audio_client", lambda: client)

    text = await ai_trainer.transcribe_voice(SimpleNamespace(name="voice.ogg"))

    assert text == ""
