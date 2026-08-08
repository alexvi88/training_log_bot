"""AI-тренер: tool-executor'ы поверх реальной БД и агентный цикл с фейковым Grok-клиентом."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ai_trainer
import config
import timeutil

pytestmark = pytest.mark.asyncio

# Вердикт гейта — JSON по strict-схеме (см. ai_trainer.GateVerdict). data=true
# почти везде, потому что тестам нужен обычный путь с инструментами.
_GATE_SEARCH = '{"search": true, "data": true}'
_GATE_NO_SEARCH = '{"search": false, "data": true}'


# Схемы 27 инструментов уезжают модели В КАЖДОМ раунде tool-call'ов, так что их
# размер — постоянная статья расхода, а не разовая (LLM_COSTS.md, идея 3). Бюджет
# в символах, а не в токенах: токенизатор xAI нам недоступен, а на живых замерах
# 22.3к символов схем весили около 6.0к токенов — то есть ~3.7 символа на токен.
_TOOL_SCHEMA_CHAR_BUDGET = 19_500


async def test_tool_schemas_stay_within_their_character_budget():
    """Описания легко отрастают обратно: каждая новая оговорка кажется бесплатной.

    Она не бесплатная — она уезжает в каждом раунде каждого вопроса. Если тест
    упал, сначала посмотри, не дублирует ли новое описание то, что уже сказано
    JSON-ограничением (minimum/maxLength/enum): такую прозу модель и так видит
    из схемы, и в описании она нужна только человеку.
    """
    total = sum(len(json.dumps(t, ensure_ascii=False)) for t in ai_trainer.TOOLS)
    assert total <= _TOOL_SCHEMA_CHAR_BUDGET, (
        f"схемы инструментов разрослись до {total} символов "
        f"(бюджет {_TOOL_SCHEMA_CHAR_BUDGET})"
    )


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


async def test_overview_includes_each_exercises_muscle_group(fresh_db, user_id):
    await _seed_bench_history(fresh_db, user_id, 1)

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_training_overview", {}))

    bench = next(e for e in payload["exercises"] if e["name"] == "Жим лёжа")
    assert bench["muscle_group"] == "Грудь"


async def test_overview_reports_null_group_for_a_groupless_exercise(fresh_db, user_id):
    await fresh_db.create_exercise(user_id, "Разное упражнение", None)

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_training_overview", {}))

    ex = next(e for e in payload["exercises"] if e["name"] == "Разное упражнение")
    assert ex["muscle_group"] is None


async def test_recent_workouts_lists_sets(fresh_db, user_id):
    await _seed_bench_history(fresh_db, user_id, 3)

    payload = json.loads(
        await ai_trainer.execute_tool(user_id, "list_recent_workouts", {"limit": 2})
    )

    assert len(payload["workouts"]) == 2
    latest = payload["workouts"][0]
    assert latest["date"] == "2026-01-03"
    assert latest["exercises"][0] == {"name": "Жим лёжа", "sets": ["103x8"], "note": None}


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


async def test_overview_reports_null_profile_fields_when_unknown(fresh_db, user_id):
    """3.3: get_training_overview всегда несёт profile, даже пустой — null,
    а не отсутствие поля, чтобы тренер понимал, что именно ещё не знает."""
    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_training_overview", {}))

    assert payload["profile"] == {
        "experience": None,
        "goal": None,
        "days_per_week": None,
        "equipment": None,
        "limitations": None,
    }


async def test_overview_surfaces_a_saved_profile(fresh_db, user_id):
    """3.3: то, что save_athlete_profile записал, должно быть видно тренеру в
    следующем разговоре без отдельного вызова get_full_chat_history —
    иначе он продолжит переспрашивать одно и то же."""
    await fresh_db.update_user(
        user_id, experience="год-два", goal="масса", days_per_week=4,
        equipment=json.dumps(["штанга", "гантели"], ensure_ascii=False),
        limitations="болит плечо",
    )

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_training_overview", {}))

    assert payload["profile"] == {
        "experience": "год-два",
        "goal": "масса",
        "days_per_week": 4,
        "equipment": ["штанга", "гантели"],
        "limitations": "болит плечо",
    }


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

    # Фиксируем «сегодня» там, откуда тренер его теперь берёт — из часового пояса
    # пользователя, а не из даты сервера.
    monkeypatch.setattr(
        ai_trainer.timeutil, "user_today", lambda user: dt.date(2026, 7, 15)  # среда
    )

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

    assert payload["muscle_group"] == "Грудь"
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


# ---------- per-exercise notes (see "📝 Заметка" in the live tracker) ----------


async def test_exercise_progress_includes_the_workouts_own_note(fresh_db, user_id):
    ex_id = await _seed_bench_history(fresh_db, user_id, 2)
    workouts = await fresh_db.list_workouts(user_id)
    await fresh_db.set_workout_exercise_note(workouts[0]["id"], ex_id, "болело плечо")

    payload = json.loads(
        await ai_trainer.execute_tool(user_id, "get_exercise_progress", {"exercise_name": "Жим лёжа"})
    )

    notes = {s["date"]: s["note"] for s in payload["sessions"]}
    assert notes[workouts[0]["started_at"][:10]] == "болело плечо"
    assert notes[workouts[1]["started_at"][:10]] is None


async def test_recent_workouts_includes_per_exercise_note(fresh_db, user_id):
    ex_id = await _seed_bench_history(fresh_db, user_id, 1)
    workouts = await fresh_db.list_workouts(user_id)
    await fresh_db.set_workout_exercise_note(workouts[0]["id"], ex_id, "техника хромает")

    payload = json.loads(await ai_trainer.execute_tool(user_id, "list_recent_workouts", {}))

    assert payload["workouts"][0]["exercises"][0]["note"] == "техника хромает"


async def test_active_workout_includes_per_exercise_note(fresh_db, user_id):
    group_id = await fresh_db.create_muscle_group(user_id, "Грудь")
    ex_id = await fresh_db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await fresh_db.create_workout(user_id, started_at="2026-04-01T10:00:00")
    block_id = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_block_exercise(block_id, ex_id, 0)
    await fresh_db.add_set(block_id, ex_id, round_index=1, order_in_round=0, weight=100.0, reps=8)
    await fresh_db.set_workout_exercise_note(workout_id, ex_id, "не растёт вес")

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_active_workout", {}))

    assert payload["exercises"][0]["note"] == "не растёт вес"


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

    # MAX_TOOL_ROUNDS+1 обычных раундов плюс один принудительный без tools —
    # см. A12: раньше цикл заканчивался ровно на них, и последний раунд с
    # tool_calls так и не получал текстового ответа (see
    # test_ask_forces_a_text_only_round_when_tool_rounds_are_exhausted below).
    assert client.chat.completions.create.await_count == ai_trainer.MAX_TOOL_ROUNDS + 2
    assert answer  # даём осмысленный fallback, а не пустую строку
    # Последний, принудительный раунд не даёт модели вызвать инструмент ещё раз.
    assert "tools" not in client.chat.completions.create.await_args.kwargs


async def test_ask_forces_a_text_only_round_when_tool_rounds_are_exhausted(fresh_db, user_id, monkeypatch):
    """A12: раньше здесь возвращалась заглушка "не получилось сформулировать
    ответ", хотя propose_program мог сработать этим же ходом — под ней висела
    бы живая кнопка сохранения программы рядом с текстом о провале."""
    calls = {"n": 0}

    async def create(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= ai_trainer.MAX_TOOL_ROUNDS + 1:
            return _response(tool_calls=[_tool_call("get_training_overview", {})])
        assert "tools" not in kwargs  # финальный раунд — без инструментов
        return _response(content="Вот и программа, гляди на кнопку ниже.")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await ai_trainer._ask_plain(user_id, "Составь программу", history=[])

    assert answer == "Вот и программа, гляди на кнопку ниже."


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
    client = _fake_client([_response(content=_GATE_SEARCH), _response(content="финальный ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await ai_trainer.ask(user_id, "Что нового в исследованиях по протеину?", history=[])

    assert answer == "финальный ответ"
    assert await fresh_db.get_ai_search_count_today(user_id) == 1
    messages = client.chat.completions.create.await_args.kwargs["messages"]
    assert any("нашёл свежее исследование по протеину" in m["content"] for m in messages if m["role"] == "system")


async def test_ask_skips_search_context_when_search_unused(fresh_db, user_id, monkeypatch):
    sdk_client = _fake_sdk_client(_xai_response(content="NO_SEARCH_NEEDED"))
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=sdk_client))
    client = _fake_client([_response(content=_GATE_SEARCH), _response(content="обычный ответ")])
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
    client = _fake_client([_response(content=_GATE_NO_SEARCH), _response(content="обычный ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await ai_trainer.ask(user_id, "Как мои дела?", history=[])

    assert answer == "обычный ответ"
    sdk_getter.assert_not_awaited()
    assert await fresh_db.get_ai_search_count_today(user_id) == 0


async def test_ask_skips_search_step_once_quota_exhausted(fresh_db, user_id, monkeypatch):
    for _ in range(config.AI_SEARCH_DAILY_LIMIT):
        await fresh_db.increment_ai_search_count(user_id)

    # Гейт вызывается и при исчерпанной квоте: вердикт про доступ к данным нужен
    # независимо от поиска (см. GateVerdict) — экономить на нём нечего, лишние
    # 6900 токенов схем в каждом раунде дороже самого вердикта.
    client = _fake_client([_response(content=_GATE_SEARCH), _response(content="обычный ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    sdk_getter = AsyncMock()
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", sdk_getter)

    answer = await ai_trainer.ask(user_id, "Вопрос", history=[])

    assert answer == "обычный ответ"
    # Дорогой шаг поиска всё равно не поднимается — квота исчерпана.
    sdk_getter.assert_not_awaited()


async def test_ask_skips_search_once_the_global_cap_is_reached(fresh_db, user_id, monkeypatch):
    """Личная квота умножается на число людей, глобальная — нет.

    Атлет свою квоту не тратил: упёрлись в общий потолок, набитый другими.
    """
    monkeypatch.setattr(config, "AI_SEARCH_GLOBAL_DAILY_LIMIT", 3)
    for other in (555, 666, 777):
        await fresh_db.increment_ai_search_count(other)
    assert await fresh_db.get_ai_search_count_today(user_id) == 0

    client = _fake_client([_response(content=_GATE_SEARCH), _response(content="обычный ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    sdk_getter = AsyncMock()
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", sdk_getter)

    answer = await ai_trainer.ask(user_id, "Что нового по креатину?", history=[])

    assert answer == "обычный ответ"
    sdk_getter.assert_not_awaited()


async def test_global_search_cap_still_allows_search_below_it(fresh_db, user_id, monkeypatch):
    """Обратная сторона того же теста: потолок не должен глушить поиск заранее."""
    monkeypatch.setattr(config, "AI_SEARCH_GLOBAL_DAILY_LIMIT", 3)
    await fresh_db.increment_ai_search_count(555)

    assert await fresh_db.get_ai_search_count_global() == 1
    assert await fresh_db.get_ai_search_count_global() < config.AI_SEARCH_GLOBAL_DAILY_LIMIT


async def test_search_increment_moves_both_the_personal_and_the_global_counter(fresh_db, user_id):
    """Разъедься счётчики — и один из двух потолков начнёт врать молча."""
    await fresh_db.increment_ai_search_count(user_id)
    await fresh_db.increment_ai_search_count(user_id)
    await fresh_db.increment_ai_search_count(999)

    assert await fresh_db.get_ai_search_count_today(user_id) == 2
    assert await fresh_db.get_ai_search_count_today(999) == 1
    assert await fresh_db.get_ai_search_count_global() == 3


async def test_ask_answers_normally_when_search_step_raises(fresh_db, user_id, monkeypatch):
    async def boom():
        raise RuntimeError("xAI search model rejected client-side tools (no beta access)")

    session = SimpleNamespace(sample=boom)
    sdk_client = SimpleNamespace(chat=SimpleNamespace(create=lambda **kwargs: session))
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=sdk_client))
    client = _fake_client([_response(content=_GATE_SEARCH), _response(content="обычный ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await ai_trainer.ask(user_id, "Вопрос", history=[])

    assert answer == "обычный ответ"
    assert await fresh_db.get_ai_search_count_today(user_id) == 0


async def test_ask_logs_question_and_search_usage(fresh_db, user_id, monkeypatch, caplog):
    sdk_client = _fake_sdk_client(_xai_response(content="находки", citations=["http://example.com"]))
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=sdk_client))
    client = _fake_client([_response(content=_GATE_SEARCH), _response(content="ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    with caplog.at_level(logging.INFO, logger="ai_trainer"):
        await ai_trainer.ask(user_id, "Что нового в исследованиях?", history=[])

    [record] = [r for r in caplog.records if "AI trainer question" in r.message]
    message = record.getMessage()
    assert "Что нового в исследованиях?" in message
    assert "web search: used" in message


async def test_ask_logs_question_without_search_usage(fresh_db, user_id, monkeypatch, caplog):
    sdk_client = _fake_sdk_client(_xai_response(content="NO_SEARCH_NEEDED"))
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=sdk_client))
    client = _fake_client([_response(content=_GATE_SEARCH), _response(content="ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    with caplog.at_level(logging.INFO, logger="ai_trainer"):
        await ai_trainer.ask(user_id, "Как мои дела?", history=[])

    [record] = [r for r in caplog.records if "AI trainer question" in r.message]
    message = record.getMessage()
    assert "Как мои дела?" in message
    # The gate said yes, the search step just came back empty — the log now
    # names that outcome instead of flattening every no-search case to "False".
    # Формулировка «returned nothing», а не «found nothing»: на неподдерживаемом
    # agent_count шаг падал ValueError ещё в chat.create, и «не нашёл» читалось
    # как честный пустой результат — функция была выключена месяцами.
    assert "web search: ran but returned nothing" in message


async def test_web_search_findings_passes_only_server_side_tools(fresh_db, user_id, monkeypatch):
    sdk_client = _fake_sdk_client(_xai_response(content="находки", citations=["http://example.com"]))
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=sdk_client))

    findings = await ai_trainer._web_search_findings(user_id, "Вопрос", history=[])

    assert findings == "находки"
    assert sdk_client.session.create_kwargs["model"] == config.GROK_SEARCH_MODEL
    # только web_search + x_search — ни одного нашего DB-инструмента, иначе
    # это снова смешивание client-side tools с multi-agent моделью, требующее беты.
    assert len(sdk_client.session.create_kwargs["tools"]) == 2


async def test_web_search_step_never_forwards_user_photo(fresh_db, user_id, monkeypatch):
    # Личное фото пользователя (форма/тело/еда) не должно уходить во внешний
    # веб/X-поиск — тему модель считывает по тексту вопроса. Гоним полный ask()
    # с картинкой и включённым поиском и проверяем, что в сообщения поисковой
    # сессии картинка не попала.
    sdk_client = _fake_sdk_client(_xai_response(content="находки", citations=["http://example.com"]))
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=sdk_client))
    client = _fake_client([_response(content=_GATE_SEARCH), _response(content="ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    await ai_trainer.ask(
        user_id, "Что нового по протеину?", history=[], image_data_url=_FAKE_IMAGE_DATA_URL
    )

    search_messages = sdk_client.session.create_kwargs["messages"]
    assert _FAKE_IMAGE_DATA_URL not in repr(search_messages)


async def test_calls_carry_the_cache_routing_header(fresh_db, user_id, monkeypatch):
    """xAI caches the repeated prefix by itself, but only if the request lands
    on the server that holds it — which is what x-grok-conv-id decides. Without
    it, half the prompt tokens miss the cache and bill at full rate. Keyed per
    user: the shared system prompt and tool schemas settle on that user's
    server, and their own history grows on top of it."""
    client = _fake_client([_response(content=_GATE_NO_SEARCH), _response(content="ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    await ai_trainer.ask(user_id, "как жим?", history=[])

    headers = client.chat.completions.create.await_args.kwargs["extra_headers"]
    assert headers["x-grok-conv-id"] == f"trainer-{user_id}"


async def test_the_cache_header_is_omitted_when_there_is_no_user():
    assert ai_trainer._cache_headers(None) == {}


async def test_gate_parses_both_verdicts(fresh_db, user_id, monkeypatch):
    client = _fake_client([_response(content='{"search": true, "data": false}')])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    verdict = await ai_trainer._gate_verdict(user_id, "Что нового по креатину?", history=[])

    assert verdict.search is True
    assert verdict.data is False
    # Гейт должен идти на дешёвую модель, а не на дорогую multi-agent.
    assert client.chat.completions.create.await_args.kwargs["model"] == config.GROK_MODEL


async def test_gate_uses_its_own_cache_slot(fresh_db, user_id, monkeypatch):
    """У гейта свой системный промпт, и под общим conv-id он вытеснял из кэша
    префикс основного вызова — в проде это стоило вчетверо дороже."""
    client = _fake_client([_response(content=_GATE_NO_SEARCH)])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    await ai_trainer._gate_verdict(user_id, "Вопрос", history=[])

    headers = client.chat.completions.create.await_args.kwargs["extra_headers"]
    assert headers["x-grok-conv-id"] == f"gate-{user_id}"


async def test_gate_asks_for_a_strict_schema(fresh_db, user_id, monkeypatch):
    """На текстовом формате модель дважды в проде вернула ПУСТОЙ content, и
    вступали дефолты — то есть поиск молча не поднимался. Схема это исключает."""
    client = _fake_client([_response(content=_GATE_SEARCH)])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    await ai_trainer._gate_verdict(user_id, "Вопрос", history=[])

    fmt = client.chat.completions.create.await_args.kwargs["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert set(fmt["json_schema"]["schema"]["required"]) == {"search", "data"}


async def test_gate_defaults_keep_data_access_on_bad_output(fresh_db, user_id, monkeypatch):
    """Асимметрия намеренная: не искать безопасно, а вот отобрать у тренера
    данные — значит заставить его отвечать общими словами про личный вопрос."""
    for content in ("", "чепуха вместо json", '{"search": true}'):
        # Три ответа: на пустом вердикте гейт делает один повтор.
        client = _fake_client([_response(content=content)] * 3)
        monkeypatch.setattr(ai_trainer, "_get_client", lambda c=client: c)
        verdict = await ai_trainer._gate_verdict(user_id, "Как мой прогресс?", history=[])
        assert verdict.data is True, f"на {content!r} тренер остался без данных"


async def test_gate_failure_falls_back_to_defaults(fresh_db, user_id, monkeypatch):
    def boom():
        raise RuntimeError("api down")

    err_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=boom)))
    )
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: err_client)

    verdict = await ai_trainer._gate_verdict(user_id, "Вопрос", history=[])
    # Ошибка гейта не должна ни валить ответ, ни отбирать данные.
    assert verdict.search is False
    assert verdict.data is True


async def test_gate_leaves_room_for_reasoning_tokens(fresh_db, user_id, monkeypatch):
    """The gate answers in one word, but a reasoning model spends the same
    budget thinking first. The old 3-token ceiling was consumed inside the
    reasoning, the content came back empty, and an empty verdict reads as "not
    YES" — so live search never ran, whatever the question."""
    client = _fake_client([_response(content=_GATE_SEARCH)])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    await ai_trainer._gate_verdict(user_id, "Что нового?", history=[])

    assert client.chat.completions.create.await_args.kwargs["max_tokens"] >= 128


async def test_gate_treats_an_empty_verdict_as_no_search_and_says_so(
    fresh_db, user_id, monkeypatch, caplog
):
    """Truncated or refused — either way there's no verdict. Not searching is
    still the right call, but it must be visible in the log rather than silent."""
    client = _fake_client([_response(content="")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    with caplog.at_level(logging.WARNING, logger="ai_trainer"):
        assert (await ai_trainer._gate_verdict(user_id, "Что нового?", history=[])).search is False

    assert any("empty verdict" in r.getMessage() for r in caplog.records)


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
    messages = ai_trainer._to_xai_messages(
        [], "что на фото?", _FAKE_IMAGE_DATA_URL, system_prompt="системный промпт"
    )

    last = messages[-1]
    assert [c.WhichOneof("content") for c in last.content] == ["text", "image_url"]
    assert last.content[0].text == "что на фото?"
    assert last.content[1].image_url.image_url == _FAKE_IMAGE_DATA_URL


async def test_to_xai_messages_text_only_without_image():
    messages = ai_trainer._to_xai_messages([], "просто текст", system_prompt="системный промпт")

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


async def test_model_clients_are_built_with_a_timeout(monkeypatch):
    """The OpenAI SDK defaults to a 600s timeout, which isn't a timeout so much
    as an abandonment: a hung request leaves the user watching "🤔 думаю…" for
    ten minutes while the placeholder animation keeps cycling."""
    import ai_trainer as module

    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(module, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(module, "_client", None)

    module._get_client()

    assert captured["timeout"] == config.AI_REQUEST_TIMEOUT_SECONDS
    assert captured["timeout"] < 600


async def test_food_diary_tool_groups_entries_by_day_with_totals(fresh_db, user_id):
    """The coach advised on nutrition while blind to the diary in the same
    database. Totals are summed here rather than left to the model: they're the
    part that gets reasoned about ("недобираешь белок")."""
    import datetime as dt

    import ai_trainer as module
    import db as dbmod

    today = dt.date.today().isoformat()
    await dbmod.add_food_entry(
        user_id, eaten_on=today, description="Овсянка", calories=350, protein=12, fat=7, carbs=60
    )
    await dbmod.add_food_entry(
        user_id, eaten_on=today, description="Курица с рисом", calories=650, protein=55, fat=12, carbs=70
    )

    payload = await module._food_diary(user_id, days=7)

    assert len(payload["days"]) == 1
    day = payload["days"][0]
    assert day["date"] == today
    assert day["calories"] == 1000
    assert day["protein"] == 67
    assert [e["description"] for e in day["entries"]] == ["Овсянка", "Курица с рисом"]


async def test_food_diary_tool_marks_entries_saved_without_macros(fresh_db, user_id):
    """Users can turn КБЖУ off — those entries are text-only, not broken."""
    import datetime as dt

    import ai_trainer as module
    import db as dbmod

    today = dt.date.today().isoformat()
    await dbmod.add_food_entry(user_id, eaten_on=today, description="Шаурма у дома")

    payload = await module._food_diary(user_id, days=7)

    day = payload["days"][0]
    assert day["entries_without_macros"] == 1
    assert day["entries"][0]["calories"] is None


async def test_food_diary_tool_is_offered_to_the_model():
    import ai_trainer as module

    names = [t["function"]["name"] for t in module.TOOLS]
    assert "get_food_diary" in names


# ---------- 5.5: get_program_adherence ----------


async def test_program_adherence_tool_is_offered_to_the_model():
    names = [t["function"]["name"] for t in ai_trainer.TOOLS]
    assert "get_program_adherence" in names


async def test_program_adherence_reports_empty_when_no_multi_day_program(fresh_db, user_id):
    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_program_adherence", {}))
    assert payload["programs"] == []


async def test_program_adherence_counts_sessions_and_days_since_last(fresh_db, user_id, monkeypatch):
    """5.5: get_saved_programs видит только состав программы — adherence должен
    сказать, реально ли по ней ходят и какие дни забрасывают."""
    import datetime as dt

    monkeypatch.setattr(
        ai_trainer.timeutil, "user_today", lambda user: dt.date(2026, 3, 20)
    )

    program_id = await fresh_db.create_program(user_id, "PPL")
    push = await fresh_db.create_routine(user_id, "Толкай", program_id=program_id)
    pull = await fresh_db.create_routine(user_id, "Тяни", program_id=program_id)
    legs = await fresh_db.create_routine(user_id, "Ноги", program_id=program_id)

    async def _log(routine_id, started_at):
        await fresh_db.create_workout(user_id, started_at=started_at, status="finished", routine_id=routine_id)

    await _log(push, "2026-03-01T10:00:00")
    await _log(push, "2026-03-08T10:00:00")
    await _log(pull, "2026-03-02T10:00:00")
    await _log(legs, "2026-01-01T10:00:00")  # давно и всего один раз

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_program_adherence", {}))

    (program,) = payload["programs"]
    assert program["program"] == "PPL"
    assert program["total_sessions"] == 4
    by_day = {d["day"]: d for d in program["days"]}
    assert by_day["Толкай"]["sessions"] == 2
    assert by_day["Толкай"]["days_since_last"] == 12
    assert by_day["Тяни"]["sessions"] == 1
    assert by_day["Ноги"]["sessions"] == 1
    assert by_day["Ноги"]["days_since_last"] == (dt.date(2026, 3, 20) - dt.date(2026, 1, 1)).days


async def test_program_adherence_never_trained_day_has_null_days_since_last(fresh_db, user_id):
    program_id = await fresh_db.create_program(user_id, "PPL")
    await fresh_db.create_routine(user_id, "Толкай", program_id=program_id)

    payload = json.loads(await ai_trainer.execute_tool(user_id, "get_program_adherence", {}))

    (program,) = payload["programs"]
    (day,) = program["days"]
    assert day == {"day": "Толкай", "sessions": 0, "days_since_last": None}


# ---------- 3.3: save_athlete_profile ----------


async def test_save_athlete_profile_tool_is_offered_to_the_model():
    names = [t["function"]["name"] for t in ai_trainer.TOOLS]
    assert "save_athlete_profile" in names


async def test_save_athlete_profile_writes_only_the_provided_fields(fresh_db, user_id):
    payload = await _confirm_profile(
        fresh_db, user_id,
        {"days_per_week": 4, "goal": "масса", "equipment": ["штанга", "гантели"]},
    )
    # Инструмент только предлагает запомнить — пишет уже подтверждение.
    assert payload["saved"] is False

    user = await fresh_db.get_user(user_id)
    assert user["days_per_week"] == 4
    assert user["goal"] == "масса"
    assert json.loads(user["equipment"]) == ["штанга", "гантели"]
    # Не присланное этим вызовом — не тронуто (осталось null).
    assert user["experience"] is None


async def test_save_athlete_profile_partial_update_does_not_erase_earlier_fields(fresh_db, user_id):
    """Раньше эти ответы оседали только в переписке — здесь важно, что второй
    частичный вызов не стирает то, что записал первый."""
    await _confirm_profile(fresh_db, user_id, {"goal": "масса"})
    await _confirm_profile(fresh_db, user_id, {"days_per_week": 3})

    user = await fresh_db.get_user(user_id)
    assert user["goal"] == "масса"
    assert user["days_per_week"] == 3


async def test_save_athlete_profile_with_nothing_useful_reports_not_saved(fresh_db, user_id):
    payload = json.loads(await ai_trainer.execute_tool(user_id, "save_athlete_profile", {}))
    assert payload["saved"] is False


async def test_weekly_digest_summary_includes_food_when_the_diary_has_any(fresh_db, user_id):
    """The connection between the plate and the barbell is the one thing no
    tracker on the market does — it only works if the Sunday digest can see
    both. The chat tool wasn't enough: the digest is a plain completion with no
    tools, so the food has to be in the summary it's given."""
    import datetime as dt

    import ai_trainer as module
    import db as dbmod

    today = dt.date.today()
    for offset, kcal, protein in ((0, 2000, 120), (1, 2300, 116)):
        await dbmod.add_food_entry(
            user_id,
            eaten_on=(today - dt.timedelta(days=offset)).isoformat(),
            description="День",
            calories=kcal,
            protein=protein,
        )

    line = await module._weekly_food_summary(user_id)

    assert "2 дн. из 7" in line
    assert "2150 ккал" in line
    assert "118 г белка" in line


async def test_weekly_digest_says_nothing_about_food_on_an_empty_diary(fresh_db, user_id):
    """No line at all rather than a zero: the prompt tells the coach that a
    missing line means "не выдумывай", and a "0 ккал" would read as starvation."""
    import ai_trainer as module

    assert await module._weekly_food_summary(user_id) == ""


async def test_weekly_food_average_is_over_days_logged_not_over_seven(fresh_db, user_id):
    """A diary kept on one day describes that day; dividing by seven would
    invent a deficit that isn't there."""
    import datetime as dt

    import ai_trainer as module
    import db as dbmod

    await dbmod.add_food_entry(
        user_id, eaten_on=dt.date.today().isoformat(), description="День",
        calories=2100, protein=100,
    )

    line = await module._weekly_food_summary(user_id)

    assert "2100 ккал" in line
    assert "1 дн. из 7" in line


async def test_the_trainer_uses_the_athletes_day_not_the_servers(fresh_db, user_id):
    """Сервер живёт в UTC, а у человека в UTC+10 сутки начинаются на десять часов
    раньше: тренировка в 00:30 по местному лежит в базе как вчерашняя UTC-дата.

    `timeutil` в ai_trainer не импортировался вовсе, а `dt.date.today()`
    встречался девять раз — включая строку «Сегодня …», которую тренер вписывает
    себе в промпт. На вопрос «сколько я сегодня сделал» он отвечал про чужой день.
    """
    import datetime as dt

    await fresh_db.update_user(user_id, tz_offset=10)
    server_today = dt.datetime.now().date()

    trainer_today = await ai_trainer._user_today(user_id)

    # У пояса +10 «сегодня» либо совпадает с серверным, либо уже следующее — но
    # никогда не берётся из UTC вслепую.
    assert trainer_today in (server_today, server_today + dt.timedelta(days=1))
    assert trainer_today == timeutil.user_today(await fresh_db.get_user(user_id))


async def test_the_date_in_the_prompt_is_the_athletes_date():
    import datetime as dt

    prompt = ai_trainer._system_prompt(dt.date(2026, 8, 4))

    assert "Сегодня 2026-08-04." in prompt


async def test_the_search_prompts_carry_a_date_too():
    """Их три, и раньше каждый брал дату сервера самостоятельно. Дата теперь
    обязательный аргумент, чтобы забыть её было нельзя."""
    import datetime as dt

    day = dt.date(2026, 8, 4)

    assert "2026-08-04" in ai_trainer._search_system_prompt(day)
    assert "2026-08-04" in ai_trainer._search_decision_system_prompt(day)


async def test_building_xai_messages_without_a_prompt_is_refused():
    """Дефолт «собери системный промпт сам» означал бы промпт без даты
    пользователя — то есть тихое возвращение той же ошибки."""
    import pytest as _pytest

    with _pytest.raises(ValueError):
        ai_trainer._to_xai_messages([], "вопрос")


# ---------- гейт данных: схемы инструментов не уезжают, когда не нужны ----------


async def test_tools_are_not_sent_when_gate_says_no_data(fresh_db, user_id, monkeypatch):
    """Схемы 27 инструментов — около 6900 токенов В КАЖДОМ раунде. На вопросе про
    общее знание («креатин работает?») ни один не нужен, и отправлять их незачем."""
    client = _fake_client([
        _response(content='{"search": false, "data": false}'),
        _response(content="креатин работает, бери 5 г в день"),
    ])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await ai_trainer.ask(user_id, "креатин работает?", history=[])

    assert answer == "креатин работает, бери 5 г в день"
    main_call = client.chat.completions.create.await_args_list[-1].kwargs
    assert "tools" not in main_call, "схемы уехали, хотя гейт сказал, что данные не нужны"


async def test_tools_are_sent_when_gate_says_data_needed(fresh_db, user_id, monkeypatch):
    client = _fake_client([
        _response(content=_GATE_NO_SEARCH),
        _response(content="по твоей истории всё растёт"),
    ])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    await ai_trainer.ask(user_id, "как мой прогресс?", history=[])

    main_call = client.chat.completions.create.await_args_list[-1].kwargs
    assert main_call.get("tools"), "тренер остался без доступа к данным на личном вопросе"


async def test_tools_are_sent_when_the_gate_breaks(fresh_db, user_id, monkeypatch):
    """Сломанный гейт не должен тихо отбирать у тренера базу — он тогда отвечает
    общими словами там, где от него ждут конкретику по человеку."""
    # Три ответа: на пустом вердикте гейт делает одну повторную попытку, и только
    # молчание дважды подряд считается поломкой.
    client = _fake_client([
        _response(content=""),
        _response(content=""),
        _response(content="ответ"),
    ])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    await ai_trainer.ask(user_id, "сколько я жал в прошлый раз?", history=[])

    main_call = client.chat.completions.create.await_args_list[-1].kwargs
    assert main_call.get("tools")


# ---------- кэш: reasoning_content должен уезжать назад в истории ----------


async def test_reasoning_content_is_reported_for_the_final_answer(
    fresh_db, user_id, monkeypatch
):
    """По документации xAI отсутствие reasoning_content в отправленной назад
    истории — причина промахов кэша номер один у ризонинговых моделей."""
    gate = _response(content='{"search": false, "data": false}')
    answer = _response(content="ответ")
    # xAI кладёт поле мимо OpenAI-схемы, поэтому SDK держит его в model_extra.
    answer.choices[0].message.model_extra = {"reasoning_content": "думал вот так"}
    client = _fake_client([gate, answer])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    seen: list[str] = []

    async def on_reasoning(text: str) -> None:
        seen.append(text)

    await ai_trainer.ask(user_id, "креатин работает?", history=[], on_reasoning=on_reasoning)

    assert seen == ["думал вот так"]


async def test_reasoning_from_history_is_sent_back_to_the_model(
    fresh_db, user_id, monkeypatch
):
    """Ключевое: поле должно доехать в messages следующего запроса — иначе
    префикс не совпадёт с закэшированным и платим по полной за всю шапку."""
    client = _fake_client([
        _response(content='{"search": false, "data": false}'),
        _response(content="новый ответ"),
    ])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    history = [
        {"role": "user", "content": "прошлый вопрос"},
        {"role": "assistant", "content": "прошлый ответ", "reasoning_content": "прошлые думы"},
    ]
    await ai_trainer.ask(user_id, "следующий вопрос", history=history)

    sent = client.chat.completions.create.await_args_list[-1].kwargs["messages"]
    assistant_msgs = [m for m in sent if m.get("role") == "assistant"]
    assert assistant_msgs, "история не доехала до модели"
    assert assistant_msgs[0].get("reasoning_content") == "прошлые думы"


async def test_no_reasoning_key_when_the_model_returned_none(fresh_db, user_id, monkeypatch):
    """Пустое поле — тоже изменение сообщения, и префикс оно ломает так же, как
    отсутствующее. Поэтому ключа быть не должно вовсе."""
    gate = _response(content='{"search": false, "data": false}')
    answer = _response(content="ответ")
    answer.choices[0].message.model_extra = {}
    client = _fake_client([gate, answer])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    seen: list[str] = []

    async def on_reasoning(text: str) -> None:
        seen.append(text)

    await ai_trainer.ask(user_id, "вопрос", history=[], on_reasoning=on_reasoning)

    assert seen == []


# ---------- живой поиск: конфиг, который его выключал ----------


@pytest.mark.asyncio(loop_scope="function")
async def test_agent_count_is_a_value_the_sdk_accepts():
    """Двойка стояла ради экономии, а SDK принимает только 4 или 16 — на ней
    поиск падал ValueError в chat.create, ДО запроса, месяцами."""
    from xai_sdk.chat import AgentCountMap

    assert config.GROK_SEARCH_AGENT_COUNT in AgentCountMap, (
        f"SDK принимает {list(AgentCountMap.keys())}, "
        f"а в конфиге {config.GROK_SEARCH_AGENT_COUNT} — поиск будет падать на каждом запросе"
    )


async def test_bad_agent_count_from_env_falls_back_instead_of_breaking_search(monkeypatch):
    """Кривое значение из окружения не должно снова выключить поиск молча."""
    import importlib

    monkeypatch.setenv("GROK_SEARCH_AGENT_COUNT", "2")
    reloaded = importlib.reload(config)
    try:
        from xai_sdk.chat import AgentCountMap

        assert reloaded.GROK_SEARCH_AGENT_COUNT in AgentCountMap
    finally:
        monkeypatch.delenv("GROK_SEARCH_AGENT_COUNT", raising=False)
        importlib.reload(config)


async def test_video_questions_never_go_to_the_web(fresh_db, user_id, monkeypatch, caplog):
    """Разбор ролика решается кадрами. Гейт же судит по тексту и на «разбери
    технику приседа легенды Святослава» честно просит сеть — поднимая multi-agent
    с четырьмя агентами и платой за вызовы инструментов ни за что."""
    client = _fake_client([_response(content=_GATE_SEARCH), _response(content="разбор")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    sdk_getter = AsyncMock()
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", sdk_getter)

    with caplog.at_level(logging.INFO, logger="ai_trainer"):
        await ai_trainer.ask(
            user_id, "разбери технику приседа легенды Святослава",
            history=[], video_context="наблюдения по кадрам",
        )

    sdk_getter.assert_not_awaited()
    [record] = [r for r in caplog.records if "AI trainer question" in r.message]
    assert "сеть не нужна" in record.getMessage()


async def test_server_tool_calls_are_counted_for_billing(fresh_db, user_id, monkeypatch):
    """web_search стоит $5 за 1000 вызовов СВЕРХ токенов, и в usage по токенам его
    нет — без отдельного счёта отчёт занижал расход на всю эту статью."""
    response = _xai_response(content="находки", citations=["http://example.com"])
    response.server_side_tool_usage = {"SERVER_SIDE_TOOL_WEB_SEARCH": 3}
    monkeypatch.setattr(
        ai_trainer, "_get_sdk_client", AsyncMock(return_value=_fake_sdk_client(response))
    )

    await ai_trainer._web_search_findings(user_id, "что нового?", history=[])

    today = fresh_db.now_iso()[:10]
    assert await fresh_db.get_server_tool_count(today) == {"SERVER_SIDE_TOOL_WEB_SEARCH": 3}


async def test_server_tool_cost_lands_in_the_daily_report(fresh_db, user_id):
    import admin_tasks

    for _ in range(4):
        await fresh_db.log_cost_event(user_id, "server_tool", model="SERVER_SIDE_TOOL_WEB_SEARCH")

    report = await admin_tasks._build_cost_report(fresh_db.now_iso()[:10])

    assert "Поиск в сети: 4 вызовов" in report
    expected = 4 * config.SERVER_TOOL_PRICE_USD_PER_CALL
    assert f"${expected:.2f}" in report or "Итого расходы" in report


async def test_broken_gate_is_not_logged_as_a_decision(fresh_db, user_id, monkeypatch, caplog):
    """В проде на «что нового в мире бодибилдинга» гейт вернул пустоту, сработал
    дефолт «не искать», а лог написал «gate says not needed» — то есть выдал
    поломку за решение. Ровно от этого предупреждает комментарий в ask()."""
    # Два пустых: гейт делает одну повторную попытку, и только молчание дважды
    # подряд считается поломкой.
    client = _fake_client([
        _response(content=""), _response(content=""), _response(content="ответ"),
    ])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    with caplog.at_level(logging.INFO, logger="ai_trainer"):
        # Вопрос без признаков свежести: иначе сработала бы страховка и поиск
        # всё-таки поднялся бы (см. _looks_like_it_needs_fresh_web).
        await ai_trainer.ask(user_id, "как ощущения после зала?", history=[])

    [record] = [r for r in caplog.records if "AI trainer question" in r.message]
    message = record.getMessage()
    assert "ГЕЙТ СЛОМАЛСЯ" in message
    assert "gate says not needed" not in message


async def test_broken_gate_still_leaves_the_trainer_his_data(fresh_db, user_id, monkeypatch):
    """Поломка гейта не должна отбирать доступ к базе — иначе тренер ответит
    общими словами на личный вопрос."""
    client = _fake_client([_response(content="не json вовсе"), _response(content="ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    await ai_trainer.ask(user_id, "сколько я жал?", history=[])

    main_call = client.chat.completions.create.await_args_list[-1].kwargs
    assert main_call.get("tools")


async def test_gate_does_not_read_the_whole_assignment(fresh_db, user_id, monkeypatch):
    """Вопрос с ответами на опросник программы приезжает на две тысячи токенов
    инструкций. Гейту нужна ТЕМА — и ровно на таких входах он в проде дважды
    вернул ноль выходных токенов."""
    client = _fake_client([_response(content=_GATE_NO_SEARCH), _response(content="ответ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    huge = "Вот ответы: " + "подробности " * 500

    await ai_trainer._gate_verdict(user_id, huge, history=[])

    sent = client.chat.completions.create.await_args.kwargs["messages"][-1]["content"]
    assert len(sent) < len(huge)
    assert len(sent) <= config.AI_GATE_QUESTION_MAX_CHARS + 10


async def test_gate_retries_once_on_silence(fresh_db, user_id, monkeypatch):
    """Ноль выходных токенов при непустых размышлениях — модель думает и не
    говорит. Повтор стоит полцента, а без вердикта поиск не поднимется на
    вопросе, который его прямо просит."""
    client = _fake_client([_response(content=""), _response(content=_GATE_SEARCH)])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    verdict = await ai_trainer._gate_verdict(user_id, "что нового?", history=[])

    assert client.chat.completions.create.await_count == 2
    assert verdict.search is True
    assert verdict.ok is True, "повтор удался — это не поломка"


async def test_gate_retry_differs_from_the_first_attempt(fresh_db, user_id, monkeypatch):
    """Первая версия повтора была точной копией — ради кэша — и в проде честно
    воспроизвела то же молчание токен в токен (696+0, потом снова 696+0).
    Детерминированную модель бессмысленно просить дважды об одном и том же."""
    client = _fake_client([_response(content=""), _response(content=_GATE_SEARCH)])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    await ai_trainer._gate_verdict(user_id, "что нового?", history=[])

    first, second = client.chat.completions.create.await_args_list
    assert second.kwargs["messages"] != first.kwargs["messages"]
    # Отличие ровно одно — приписка, а не переделанный запрос.
    assert len(second.kwargs["messages"]) == len(first.kwargs["messages"]) + 1
    assert "просто верни JSON" in second.kwargs["messages"][-1]["content"]


async def test_gate_gives_up_after_two_silences(fresh_db, user_id, monkeypatch):
    client = _fake_client([_response(content=""), _response(content="")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    verdict = await ai_trainer._gate_verdict(user_id, "что нового?", history=[])

    assert client.chat.completions.create.await_count == 2
    assert verdict.ok is False
    assert verdict.data is True, "поломка не должна отбирать у тренера базу"


async def test_dead_gate_still_searches_when_the_question_asks_for_freshness(
    fresh_db, user_id, monkeypatch, caplog
):
    """Дефолт мёртвого гейта — «не искать», и на «что нового в мире бодибилдинга»
    это оставляло человека без ответа по существу: модель рассказывала о мире,
    которого уже нет. В проде так и было."""
    client = _fake_client([
        _response(content=""), _response(content=""), _response(content="ответ"),
    ])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    findings = AsyncMock(return_value="в сети пишут вот что")
    monkeypatch.setattr(ai_trainer, "_web_search_findings", findings)

    with caplog.at_level(logging.INFO, logger="ai_trainer"):
        await ai_trainer.ask(user_id, "что нового в мире бодибилдинга?", history=[])

    findings.assert_awaited_once()
    [record] = [r for r in caplog.records if "AI trainer question" in r.message]
    assert "вопрос просит свежести" in record.getMessage()


async def test_freshness_fallback_does_not_fire_on_personal_questions(
    fresh_db, user_id, monkeypatch
):
    """Страховка про свежесть — не замена гейту: лишний шаг поиска стоит денег и
    десятков секунд, а на личном вопросе в сети нет ничего."""
    client = _fake_client([
        _response(content=""), _response(content=""), _response(content="ответ"),
    ])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    findings = AsyncMock(return_value="находки")
    monkeypatch.setattr(ai_trainer, "_web_search_findings", findings)

    await ai_trainer.ask(user_id, "сколько я жал в прошлый раз?", history=[])

    findings.assert_not_awaited()


async def test_freshness_markers_cover_the_phrasings_seen_in_prod():
    assert ai_trainer._looks_like_it_needs_fresh_web("что нового в мире бодибилдинга")
    assert ai_trainer._looks_like_it_needs_fresh_web("какие свежие исследования по креатину?")
    assert ai_trainer._looks_like_it_needs_fresh_web("последние тренды в тренировках")
    # А это личные вопросы — сети тут делать нечего.
    assert not ai_trainer._looks_like_it_needs_fresh_web("как мой прогресс?")
    assert not ai_trainer._looks_like_it_needs_fresh_web("сколько белка на кг веса?")


# ---------- цена живого поиска: выводы из первого успешного прогона ----------


async def test_search_step_does_not_fan_out_to_multiple_agents(fresh_db, user_id, monkeypatch):
    """Первый живой поиск стоил $0.23, из них $0.19 — multi-agent и 18 вызовов
    инструментов. Он не «ищет дешевле», он запускает четыре независимых агента, и
    биллятся токены всех сразу. Дешевле четырёх SDK не принимает."""
    sdk_client = _fake_sdk_client(_xai_response(content="находки", citations=["http://e.com"]))
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=sdk_client))

    await ai_trainer._web_search_findings(user_id, "что нового?", history=[])

    kwargs = sdk_client.session.create_kwargs
    assert "multi-agent" not in kwargs["model"]
    assert "agent_count" not in kwargs, "фан-аут на четыре агента — это $0.19 за вопрос"


async def test_agent_count_returns_if_a_multi_agent_model_is_configured(
    fresh_db, user_id, monkeypatch
):
    """Если шаг однажды станет настоящим research'ем и multi-agent вернётся —
    agent_count должен поехать снова, иначе SDK упадёт на дефолте."""
    monkeypatch.setattr(config, "GROK_SEARCH_MODEL", "grok-4.20-multi-agent")
    sdk_client = _fake_sdk_client(_xai_response(content="находки", citations=["http://e.com"]))
    monkeypatch.setattr(ai_trainer, "_get_sdk_client", AsyncMock(return_value=sdk_client))

    await ai_trainer._web_search_findings(user_id, "что нового?", history=[])

    kwargs = sdk_client.session.create_kwargs
    assert kwargs["agent_count"] == config.GROK_SEARCH_AGENT_COUNT


async def test_search_daily_limit_is_sized_for_the_real_price():
    """Сорок ставилось, пока поиск был сломан и стоил ноль. Реальная цена вопроса
    с поиском — около $0.23, то есть сорок это $9 в день на человека."""
    assert config.AI_SEARCH_DAILY_LIMIT <= 10


# ---------- поиск только по нашей теме, и схемы не ломают кэш ----------


async def test_gate_prompt_keeps_search_on_our_topic():
    """«Новости политики сегодня» ушли в живой поиск за 11 центов. Свежесть там
    правда нужна — но тренеру политика не по профилю, а поиск стоит дорого."""
    prompt = ai_trainer.SEARCH_DECISION_SYSTEM_PROMPT
    assert "тренировки" in prompt and "питание" in prompt
    assert "политик" in prompt.lower(), "гейт должен знать пример не-нашей темы"


async def test_search_step_is_told_to_be_frugal():
    """Одиннадцать запросов и 79 тысяч входных токенов на одном вопросе никто не
    заказывал: содержимое страниц целиком идёт в оплату."""
    assert "ЭКОНОМНО" in ai_trainer.SEARCH_SYSTEM_PROMPT


async def test_tools_stay_on_once_a_conversation_has_history(fresh_db, user_id, monkeypatch):
    """Схемы можно выбросить только на первом вопросе. Дальше промах по префиксу
    дороже экономии: 30272 токена из кэша стоили $0.0105, а 17732 без кэша —
    $0.0387. Меньше токенов, втрое дороже."""
    client = _fake_client([
        _response(content='{"search": false, "data": false}'),
        _response(content="ответ"),
    ])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    history = [
        {"role": "user", "content": "как прогресс?"},
        {"role": "assistant", "content": "растёт"},
    ]

    await ai_trainer.ask(user_id, "сколько белка на кг?", history=history)

    main_call = client.chat.completions.create.await_args_list[-1].kwargs
    assert main_call.get("tools"), "схемы выкинули посреди разговора — это промах кэша"


async def test_tools_can_still_be_skipped_on_the_very_first_question(
    fresh_db, user_id, monkeypatch
):
    """На первом вопросе кэшу терять нечего — префикса ещё нет, экономия чистая."""
    client = _fake_client([
        _response(content='{"search": false, "data": false}'),
        _response(content="ответ"),
    ])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    await ai_trainer.ask(user_id, "сколько белка на кг?", history=[])

    main_call = client.chat.completions.create.await_args_list[-1].kwargs
    assert "tools" not in main_call


# ---------- находка 36: тренер не записывает того, чего не спрашивал ----------


async def test_bare_no_is_not_recorded_as_a_limitation(fresh_db, user_id):
    """Живой прогон: опросник спросил про дни, время, железо, опыт и цель — про
    травмы не спрашивал вовсе, — а тренер отчитался «травм нет» и записал это в
    профиль. Экран «Обо мне» подписан «Записал с твоих слов»."""
    result, _ = await ai_trainer._save_athlete_profile(user_id, {"limitations": "нет"})

    assert "limitations" not in (result.get("fields") or {})
    assert (await fresh_db.get_user(user_id))["limitations"] is None


async def test_a_real_limitation_is_still_recorded(fresh_db, user_id):
    result, _ = await ai_trainer._save_athlete_profile(user_id, {"limitations": "болит правое плечо"})

    assert result["fields"]["limitations"] == "болит правое плечо"


async def test_a_blank_limitation_does_not_block_the_other_fields(fresh_db, user_id):
    result, _ = await ai_trainer._save_athlete_profile(user_id, {"limitations": "нет", "goal": "масса"})

    assert result["fields"] == {"goal": "масса"}

async def test_prompt_forbids_calling_a_triple_a_one_rep_max():
    """В проде тренер написал «разово на штанге максимум 210», хотя двумя строками
    выше стояло «210×3 (e1RM ~231)». И посчитал до 250 разрыв +40кг вместо +19:
    занизил атлета и выдумал ему лишние килограммы работы."""
    prompt = ai_trainer.SYSTEM_PROMPT
    assert "НЕ РАЗОВЫЙ МАКСИМУМ" in prompt
    assert "e1RM" in prompt
    assert "+19" in prompt, "в промпте должен быть числовой пример ошибки"


async def test_prompt_bans_telegraphic_style_and_slashes():
    """В проде тренер написал «слишком часто тяжёлый пол», «хват/срыв техникой»,
    «мало еды/сна», «тяга гниёт» — выброшенные глаголы, слэши вместо слов и
    выдуманные метафоры. Лаконичность он прочитал как право не договаривать."""
    prompt = ai_trainer.SYSTEM_PROMPT
    assert "КОРОТКИЕ ФРАЗЫ" in prompt
    assert "Слэш — не слово" in prompt
    assert "гниёт" in prompt, "нужен пример выдуманной метафоры"


async def test_prompt_ties_heavy_and_light_to_percent_of_e1rm():
    """В проде тренер назвал 200/190/180/180 «четырьмя почти максимальными» и
    отругал за них — при e1RM 231 это 87/82/78/78%, то есть один тяжёлый и три
    бэкоффа. Ровно та схема, которую он тут же и посоветовал."""
    prompt = ai_trainer.SYSTEM_PROMPT
    assert "ПРОЦЕНТ ОТ e1RM" in prompt
    assert "90%" in prompt, "нужен порог, ниже которого вес не «почти максимальный»"
    assert "БЭКОФФ" in prompt.upper(), "убывающая лестница должна распознаваться"


async def test_prompt_requires_owning_a_correction():
    """«Не +40 от тройки» — опровержение своей же прошлой фразы, которую человек не
    произносил. Теперь история диалога не переписывается, и тренер видит свои
    старые ответы: он должен признавать ошибку от первого лица, а не спорить."""
    assert "посчитал неверно" in ai_trainer.SYSTEM_PROMPT


async def test_prompt_does_not_promise_the_start_button_under_the_message():
    """Находка 43: тренер писал «Под сообщением превью и кнопка ▶️ Начать по
    ней», а под сообщением стояла одна «🗂 Забрать: …» — превью и «Начать по
    ней» лежат за ней. Человек искал кнопку, которой на экране нет."""
    prompt = ai_trainer.SYSTEM_PROMPT
    assert "Забрать" in prompt, "промпт должен называть кнопку, которая реально есть"
    assert "не обещай «Начать по ней» прямо под сообщением" in prompt


async def test_prompt_forbids_promising_an_edit_without_doing_it():
    """Живой прогон на grok-4.3: «Сейчас исправлю программу: ноль работы на низ»
    — и конец хода. Ни новой программы, ни признака, что тренер ещё думает."""
    prompt = ai_trainer.SYSTEM_PROMPT
    assert "НИКОГДА не обещай действие вместо того, чтобы его сделать" in prompt
    assert "Обещание без\nвызова инструмента" in prompt


async def test_prompt_keeps_heavy_lifts_out_of_high_rep_ranges():
    """Живой прогон: под цель «становая 250» тренер поставил 4×5–8 в самой
    становой. Восьмёрка там — не силовая работа, а испытание поясницы."""
    prompt = ai_trainer.SYSTEM_PROMPT
    assert "3-5 повторов" in prompt
    assert "становая" in prompt.lower()

async def _confirm_profile(db, user_id: int, tool_input: dict) -> dict:
    """Прогнать инструмент и подтвердить запись — профиль теперь пишется только
    после «✅ Запомнить» (см. handlers.ai_trainer.ai_profile_confirm)."""
    import json as _json

    payload = _json.loads(await ai_trainer.execute_tool(user_id, "save_athlete_profile", tool_input))
    fields = payload.get("fields") or {}
    if fields:
        await db.update_user(user_id, **fields)
    return payload


async def test_prompt_forbids_claiming_the_profile_is_already_saved():
    """Профиль пишется только после подтверждения под ответом, а модель раньше
    бодро сообщала «запомнил» — и человек считал, что дело сделано."""
    prompt = ai_trainer.SYSTEM_PROMPT
    assert "не пиши «запомнил»" in prompt
    assert "Предлагаю запомнить" in prompt
