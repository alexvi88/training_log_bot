"""Готовые вопросы интро отвечаются за один раунд модели.

На «слабые места» и «как прогресс» модель первым раундом всегда только просит
данные (живой лог: 16к входа, 64 выхода — один tool_call). Эти данные бот берёт
сам до первого вызова и дописывает в разговор как assistant(tool_calls) +
tool-результаты. Префикс (системный промпт, схемы) не меняется.
"""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ai_trainer
import i18n

pytestmark = pytest.mark.asyncio

WEAK = "ai.screen.preset.weak.question"
PROGRESS = "ai.screen.preset.progress.question"
EXPECTED = {
    WEAK: ["get_training_overview", "get_weekly_volume_by_group"],
    PROGRESS: ["get_training_overview", "get_stalled_lifts"],
}


def _response(content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _tool_call(call_id: str, name: str, arguments: str = "{}"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


class _FakeClient:
    """Отвечает по очереди и запоминает КОПИЮ каждого запроса: messages
    дописывается и после вызова, а проверять надо то, что уехало тогда."""

    def __init__(self, responses):
        self.calls: list[dict] = []
        self._responses = list(responses)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=self._create)))

    async def _create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return self._responses.pop(0)


class _FakeStream:
    def __init__(self, text: str):
        self._events = [
            SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=None, reasoning_content=None))],
            ),
            SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5), choices=[]),
        ]

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for e in self._events:
            yield e


def _assert_valid_wire(messages: list[dict]) -> None:
    """Каждый assistant(tool_calls) сразу продолжен tool-сообщениями ровно на
    свои id, без сирот; id уникальны на всю историю."""
    ids: list[str] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        assert m["role"] != "tool", f"tool без своего assistant(tool_calls) на позиции {i}"
        if m["role"] == "assistant" and m.get("tool_calls"):
            want = [tc["id"] for tc in m["tool_calls"]]
            ids.extend(want)
            got = [messages[i + 1 + k]["tool_call_id"] for k in range(len(want))]
            assert all(messages[i + 1 + k]["role"] == "tool" for k in range(len(want)))
            assert got == want
            i += 1 + len(want)
            continue
        i += 1
    assert len(ids) == len(set(ids)), "повтор tool_call id в истории"


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("key", [WEAK, PROGRESS])
async def test_preset_is_answered_in_one_model_call(fresh_db, user_id, monkeypatch, lang, key):
    client = _FakeClient([_response(content="разбор")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    wires: list = []

    async def keep(w):
        wires.append(w)

    # Вопрос другого языка, чем у хода, и с пробелами по краям — всё равно пресет.
    question = f"  {i18n.t_in(lang, key)}\n"
    answer = await ai_trainer.ask(user_id, question, history=[], on_wire=keep)

    assert answer == "разбор"
    assert len(client.calls) == 1
    sent = client.calls[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant"] + ["tool"] * len(EXPECTED[key])
    calls = sent[2]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == EXPECTED[key]
    assert all(c["function"]["arguments"] == "{}" and c["type"] == "function" for c in calls)
    assert [m["tool_call_id"] for m in sent[3:]] == [c["id"] for c in calls]
    # Те же данные, что отдал бы вызов моделью.
    for call, tool_msg in zip(calls, sent[3:], strict=True):
        direct = await ai_trainer.execute_tool(user_id, call["function"]["name"], {})
        assert json.loads(tool_msg["content"]) == json.loads(direct)
    # Префикс не тронут: тот же системный промпт и те же схемы.
    assert sent[0]["content"] == ai_trainer._system_prompt()
    assert client.calls[0]["tools"] == ai_trainer.TOOLS

    wire = wires[0]
    assert wire == sent[1:] + [{"role": "assistant", "content": "разбор"}]
    _assert_valid_wire(wire)


async def test_ordinary_question_is_unchanged(fresh_db, user_id, monkeypatch):
    client = _FakeClient([_response(content="ок")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    await ai_trainer.ask(user_id, "Как мой прогресс?", history=[])

    assert len(client.calls) == 1
    assert [m["role"] for m in client.calls[0]["messages"]] == ["system", "user"]


async def test_repeated_preset_keeps_history_valid_and_prefix_stable(fresh_db, user_id, monkeypatch):
    """Второй тап того же пресета дописывается к первому ходу: id не
    повторяются, а первый ход уезжает байт-в-байт тем, что сохранили."""
    client = _FakeClient([_response(content="раз"), _response(content="два")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    wires: list = []

    async def keep(w):
        wires.append(w)

    q = i18n.t("ai.screen.preset.weak.question")
    await ai_trainer.ask(user_id, q, history=[], on_wire=keep)
    await ai_trainer.ask(user_id, q, history=wires[0], on_wire=keep)

    assert len(client.calls) == 2
    second = client.calls[1]["messages"]
    assert second[1:1 + len(wires[0])] == wires[0]
    _assert_valid_wire(wires[1])


async def test_model_may_still_ask_for_more(fresh_db, user_id, monkeypatch):
    client = _FakeClient([
        _response(content="", tool_calls=[_tool_call("c9", "compare_periods")]),
        _response(content="итог"),
    ])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    wires: list = []

    async def keep(w):
        wires.append(w)

    answer = await ai_trainer.ask(
        user_id, i18n.t("ai.screen.preset.progress.question"), history=[], on_wire=keep
    )

    assert answer == "итог"
    assert len(client.calls) == 2
    _assert_valid_wire(wires[0])
    assert [m["role"] for m in wires[0]] == [
        "user", "assistant", "tool", "tool", "assistant", "tool", "assistant",
    ]


async def test_streamed_preset_is_one_call_too(fresh_db, user_id, monkeypatch):
    """Стрим (/v1/ai/ask?stream, пузырь-черновик в Telegram) идёт через тот же ход."""
    client = _FakeClient([_FakeStream("разбор стримом")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    chunks: list = []

    async def on_chunk(text):
        chunks.append(text)

    answer = await ai_trainer.ask(
        user_id, i18n.t_in("en", "ai.screen.preset.weak.question"), history=[], on_chunk=on_chunk,
    )

    assert answer == "разбор стримом"
    assert len(client.calls) == 1
    assert client.calls[0]["stream"] is True
    assert client.calls[0]["messages"][2]["tool_calls"][0]["function"]["name"] == "get_training_overview"


async def test_failed_prefetch_falls_back_to_the_ordinary_path(fresh_db, user_id, monkeypatch):
    client = _FakeClient([_response(content="ок")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    async def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(ai_trainer, "execute_tool", boom)
    await ai_trainer.ask(user_id, i18n.t("ai.screen.preset.weak.question"), history=[])

    assert [m["role"] for m in client.calls[0]["messages"]] == ["system", "user"]
