"""AI-тренер: tool-executor'ы поверх реальной БД и агентный цикл с фейковым Grok-клиентом."""

import json
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


# ---------- agentic loop (no-search path, _ask_plain — REST/OpenAI-compatible) ----------
#
# ask() dispatches to _ask_with_search whenever the daily search quota isn't
# exhausted (see the "search dispatch" section below); these tests exercise
# the underlying REST tool-calling loop directly so they don't depend on that
# quota state.

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


# ---------- search dispatch (ask() choosing between _ask_plain / _ask_with_search) ----------

def _xai_response(content=None, tool_calls=None, citations=None, server_side_tool_usage=None):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
        citations=citations or [],
        server_side_tool_usage=server_side_tool_usage or {},
    )


class _FakeXaiSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.appended: list = []

    async def sample(self):
        return self._responses.pop(0)

    def append(self, message):
        self.appended.append(message)


def _fake_sdk_client(session):
    return SimpleNamespace(chat=SimpleNamespace(create=lambda **kwargs: session))


async def test_ask_uses_search_path_and_counts_quota_when_search_used(fresh_db, user_id, monkeypatch):
    session = _FakeXaiSession([_xai_response(content="ответ с поиском", citations=["http://example.com"])])
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=_fake_sdk_client(session)))

    answer = await ai_trainer.ask(user_id, "Что нового в исследованиях по протеину?", history=[])

    assert answer == "ответ с поиском"
    assert await fresh_db.get_ai_search_count_today(user_id) == 1


async def test_ask_search_path_does_not_count_quota_when_search_unused(fresh_db, user_id, monkeypatch):
    session = _FakeXaiSession([_xai_response(content="ответ без поиска")])
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=_fake_sdk_client(session)))

    await ai_trainer.ask(user_id, "Как мои дела?", history=[])

    assert await fresh_db.get_ai_search_count_today(user_id) == 0


async def test_ask_falls_back_to_plain_once_quota_exhausted(fresh_db, user_id, monkeypatch):
    for _ in range(config.AI_SEARCH_DAILY_LIMIT):
        await fresh_db.increment_ai_search_count(user_id)

    client = _fake_client([_response(content="обычный ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    sdk_getter = AsyncMock()
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", sdk_getter)

    answer = await ai_trainer.ask(user_id, "Вопрос", history=[])

    assert answer == "обычный ответ"
    sdk_getter.assert_not_awaited()


async def test_ask_with_search_runs_custom_tool_round(fresh_db, user_id, monkeypatch):
    await _seed_bench_history(fresh_db, user_id, 1)
    session = _FakeXaiSession([
        _xai_response(tool_calls=[_tool_call("get_training_overview", {})]),
        _xai_response(content="Погнали дальше", citations=["http://example.com"]),
    ])
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=_fake_sdk_client(session)))

    answer = await ai_trainer.ask(user_id, "Как мои дела?", history=[])

    assert answer == "Погнали дальше"
    # assistant(tool_calls) + tool_result + final assistant
    assert len(session.appended) == 3
    assert await fresh_db.get_ai_search_count_today(user_id) == 1


async def test_ask_with_search_reports_tool_failure_to_model(fresh_db, user_id, monkeypatch):
    session = _FakeXaiSession([
        _xai_response(tool_calls=[_tool_call("get_exercise_progress", {"exercise_name": "Жим"})]),
        _xai_response(content="ответ"),
    ])
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=_fake_sdk_client(session)))

    async def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(ai_trainer, "execute_tool", boom)

    answer = await ai_trainer.ask(user_id, "Прогресс?", history=[])

    assert answer == "ответ"
