"""AI-тренер: tool-executor'ы поверх реальной БД и агентный цикл с фейковым клиентом."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ai_trainer

pytestmark = pytest.mark.asyncio


async def _seed_bench_history(db, user_id: int, n_sessions: int = 3) -> int:
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
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


async def test_tools_do_not_leak_other_users_data(fresh_db, user_id):
    other = await fresh_db.get_or_create_user(telegram_id=222, username="other")
    await _seed_bench_history(fresh_db, other["telegram_id"], 2)

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_training_overview", {}))

    assert payload["stats"]["total_workouts"] == 0
    assert payload["exercises"] == []


# ---------- agentic loop ----------

def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_block(name, tool_input, block_id="toolu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id)


def _response(content, stop_reason):
    return SimpleNamespace(content=content, stop_reason=stop_reason)


async def test_ask_runs_tool_round_and_returns_text(fresh_db, user_id, monkeypatch):
    await _seed_bench_history(fresh_db, user_id, 1)

    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=[
        _response([_tool_block("get_training_overview", {})], "tool_use"),
        _response([_text_block("Ты молодец, продолжай!")], "end_turn"),
    ])))
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await ai_trainer.ask(user_id, "Как мои дела?", history=[])

    assert answer == "Ты молодец, продолжай!"
    assert client.messages.create.await_count == 2
    # Второй запрос несёт результат инструмента обратно модели.
    second_messages = client.messages.create.await_args_list[1].kwargs["messages"]
    tool_result = second_messages[-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "toolu_1"
    assert "total_workouts" in tool_result["content"]


async def test_ask_tool_failure_is_reported_as_error_result(fresh_db, user_id, monkeypatch):
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=[
        _response([_tool_block("get_exercise_progress", {"exercise_name": "Жим"})], "tool_use"),
        _response([_text_block("ответ")], "end_turn"),
    ])))
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    async def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(ai_trainer, "execute_tool", boom)

    answer = await ai_trainer.ask(user_id, "Прогресс?", history=[])

    assert answer == "ответ"
    second_messages = client.messages.create.await_args_list[1].kwargs["messages"]
    tool_result = second_messages[-1]["content"][0]
    assert tool_result["is_error"] is True


async def test_ask_refusal_returns_safe_text(fresh_db, user_id, monkeypatch):
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=
        _response([], "refusal")
    )))
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await ai_trainer.ask(user_id, "…", history=[])

    assert "тренировки" in answer


async def test_ask_stops_after_max_tool_rounds(fresh_db, user_id, monkeypatch):
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=
        _response([_tool_block("get_training_overview", {})], "tool_use")
    )))
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await ai_trainer.ask(user_id, "Как дела?", history=[])

    assert client.messages.create.await_count == ai_trainer.MAX_TOOL_ROUNDS + 1
    assert answer  # даём осмысленный fallback, а не пустую строку
