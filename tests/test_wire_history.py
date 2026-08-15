"""История для кэша: храним то, что реально уехало модели.

По документации xAI кэш живёт на НЕИЗМЕННОМ префиксе: «Any change to earlier
messages breaks the cache. Only append new messages at the end». Раньше история
между вопросами пересобиралась из голого текста — tool-сообщения выбрасывались, —
и в логах прода это выглядело так: внутри одного вопроса раунды дописываются и
кэш растёт (10752 → 11776 → 14080 токенов), а каждый следующий вопрос начинается
с 128.
"""

import json

import pytest

import ai_trainer
import config

pytestmark = pytest.mark.asyncio


def _turn(user_text: str, answer: str, with_tool: bool = True) -> list[dict]:
    """Один ход целиком, как он уезжает модели."""
    msgs: list[dict] = [{"role": "user", "content": user_text}]
    if with_tool:
        msgs += [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "get_training_overview", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": '{"stats": {}}'},
        ]
    msgs.append({"role": "assistant", "content": answer, "reasoning_content": "думал"})
    return msgs


# ---------- лёгкая история для гейта и поиска ----------


async def test_light_history_drops_tool_traffic():
    """Гейту и поиску tool-сообщения не нужны и стоили бы токенов на каждый вопрос."""
    light = ai_trainer._light_history(_turn("как дела?", "нормально"))

    assert [m["role"] for m in light] == ["user", "assistant"]
    assert light[1]["content"] == "нормально"


async def test_light_history_keeps_a_plain_dialogue_as_is():
    plain = [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "здоров"},
    ]
    assert ai_trainer._light_history(plain) == plain


async def test_light_history_drops_injected_system_context():
    """Наблюдения по видео и находки поиска — системные вставки, не реплики."""
    history = [
        {"role": "system", "content": "разбор видео"},
        {"role": "user", "content": "норм?"},
        {"role": "assistant", "content": "норм"},
    ]
    assert [m["role"] for m in ai_trainer._light_history(history)] == ["user", "assistant"]


# ---------- обрезка: главное, чтобы не сломать структуру запроса ----------


async def test_trim_never_orphans_a_tool_message():
    """tool без своего assistant(tool_calls) выше — невалидный запрос, API
    отвергнет его целиком. Поэтому режем только по границам ходов."""
    history = _turn("первый", "ответ 1") + _turn("второй", "ответ 2") + _turn("третий", "ответ 3")

    # Лимит такой, что влезает примерно один ход.
    trimmed = ai_trainer._trim_wire_history(history, max_chars=len(json.dumps(_turn("x", "y"), ensure_ascii=False)) + 50)

    assert trimmed[0]["role"] == "user", "обрезали не по границе хода"
    for i, m in enumerate(trimmed):
        if m.get("role") == "tool":
            prev = trimmed[i - 1]
            assert prev.get("tool_calls"), "tool-сообщение осталось без своего вызова"


async def test_trim_keeps_everything_under_the_limit():
    history = _turn("вопрос", "ответ")
    assert ai_trainer._trim_wire_history(history, max_chars=100_000) == history


async def test_trim_drops_oldest_turns_first():
    history = _turn("старый", "ответ старый") + _turn("новый", "ответ новый")
    one_turn = len(json.dumps(_turn("новый", "ответ новый"), ensure_ascii=False))

    trimmed = ai_trainer._trim_wire_history(history, max_chars=one_turn + 20)

    texts = [m.get("content") for m in trimmed]
    assert "новый" in texts
    assert "старый" not in texts


async def test_trim_keeps_the_last_turn_even_if_it_alone_exceeds_the_limit():
    """Резать ВНУТРЬ хода нельзя, а пустая история потеряла бы разговор совсем."""
    history = _turn("старый", "ответ") + _turn("огромный", "х" * 5000)

    trimmed = ai_trainer._trim_wire_history(history, max_chars=100)

    assert trimmed
    assert trimmed[0]["role"] == "user"
    assert trimmed[0]["content"] == "огромный"


async def test_configured_limit_is_generous_enough_for_a_real_conversation():
    """Каждая обрезка — промах кэша, поэтому порог должен быть высоким."""
    five_turns = sum((_turn(f"в{i}", f"о{i}") for i in range(5)), [])
    assert len(json.dumps(five_turns, ensure_ascii=False)) < config.AI_WIRE_HISTORY_MAX_CHARS


# ---------- сквозное: ask отдаёт то, что уехало модели ----------


def _response(content=None, tool_calls=None, reasoning=None):
    from types import SimpleNamespace

    message = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _fake_client(responses):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=responses)))
    )


async def _store(sink: list, messages: list) -> None:
    sink.append(messages)


async def test_wire_carries_the_answer_so_the_next_question_appends(
    fresh_db, user_id, monkeypatch
):
    """Финальный ответ в messages не попадал — цикл на нём заканчивается. Без него
    история отдаёт вопрос без ответа, то есть снова не тот префикс."""
    client = _fake_client([_response(content="жми дальше", reasoning="думал")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    seen: list = []

    await ai_trainer.ask(
        user_id, "как прогресс?", history=[], on_wire=lambda msgs: _store(seen, msgs)
    )

    wire = seen[0]
    assert [m["role"] for m in wire] == ["user", "assistant"]
    today = await ai_trainer._user_today(user_id)
    assert wire[0]["content"] == f"как прогресс?\n\nСегодня {today.isoformat()}."
    assert wire[-1]["content"] == "жми дальше"
    # Размышления обязаны уехать назад — это причина промахов номер один по докам xAI.
    assert wire[-1]["reasoning_content"] == "думал"
    # Системного промпта в истории нет: он не входит в историю и пересобирается
    # заново на каждый запрос (и теперь не зависит от даты).
    assert all(m["role"] != "system" for m in wire)


async def test_photo_is_not_stored_in_history(fresh_db, user_id, monkeypatch):
    """База64 картинки в FSM — это мегабайты в файле, который читается на каждый
    апдейт, плюс повторная плата за image-токены на каждом следующем вопросе."""
    client = _fake_client([_response(content="вижу еду")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    seen: list = []

    await ai_trainer.ask(
        user_id, "что тут по калориям?", history=[],
        image_data_url="data:image/jpeg;base64," + "A" * 5000,
        on_wire=lambda msgs: _store(seen, msgs),
    )

    dumped = json.dumps(seen[0], ensure_ascii=False)
    assert "base64" not in dumped
    assert "AAAA" not in dumped
    # Вместо картинки — текст вопроса (с датой, как и уехало модели), чтобы
    # сообщение осталось валидным.
    today = await ai_trainer._user_today(user_id)
    assert seen[0][0]["content"] == f"что тут по калориям?\n\nСегодня {today.isoformat()}."


async def test_video_observations_stay_in_history(fresh_db, user_id, monkeypatch):
    """Чтобы на «точно нет ошибок?» тренер помнил, что именно видел на ролике —
    раньше этот контекст терялся сразу после ответа."""
    client = _fake_client([_response(content="норм")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    seen: list = []

    await ai_trainer.ask(
        user_id, "оцени технику", history=[],
        video_context="Отклонения: круглая спина на 0:05",
        on_wire=lambda msgs: _store(seen, msgs),
    )

    dumped = json.dumps(seen[0], ensure_ascii=False)
    assert "круглая спина" in dumped

