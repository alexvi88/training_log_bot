"""Главный цикл Luna — OpenAI Responses API (POST /responses), с рассуждением.

В /v1/chat/completions gpt-6-luna не принимает инструменты вместе с
reasoning_effort (400), поэтому раунды с инструментами Luna идут в Responses.
Что держат эти тесты (живых вызовов нет — клиент подменён):

- запрос: /responses, reasoning low, инструменты в формате Responses (strict
  явно False), store false + include reasoning.encrypted_content, ключ кэша;
- function_call → наш инструмент → function_call_output с тем же call_id,
  рассуждение раунда (encrypted_content) возвращается следующим запросом как
  было, и следующий ход дописывает к тому же input — префикс не переписан;
- стрим: события Responses → те же on_chunk / on_status, а /v1/ai/ask отдаёт
  прежние chunk / status / done;
- цена — по usage Responses, размышления не удваиваются; обрыв стрима —
  оценкой, как у chat-пути;
- после Luna-хода grok получает историю без чужих полей.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from types import SimpleNamespace

import httpx
import pytest

import ai_trainer
import api_v1
import config
import db

OWN = 111

REASONING_1 = {
    "type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAA-round-1",
}
CALL_1 = {
    "type": "function_call", "id": "fc_1", "call_id": "call_abc",
    "name": "get_training_overview", "arguments": "{}", "status": "completed",
}
REASONING_2 = {
    "type": "reasoning", "id": "rs_2", "summary": [], "encrypted_content": "gAAA-round-2",
}
ANSWER = "Жим растёт третью неделю подряд, присед стоит — добавь второй день на ноги."
MESSAGE_2 = {
    "type": "message", "id": "msg_2", "role": "assistant", "status": "completed",
    "content": [{"type": "output_text", "text": ANSWER, "annotations": []}],
}
USAGE_1 = {
    "input_tokens": 12_000, "output_tokens": 300,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens_details": {"reasoning_tokens": 250},
}
USAGE_2 = {
    "input_tokens": 13_000, "output_tokens": 900,
    "input_tokens_details": {"cached_tokens": 11_000},
    "output_tokens_details": {"reasoning_tokens": 400},
}


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


@pytest.fixture
def own_luna(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 1)
    monkeypatch.setattr(config, "TEST_USER_ID", OWN)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")

    def no_grok():
        raise AssertionError("Luna-ход не должен трогать xAI-клиент")

    monkeypatch.setattr(ai_trainer, "_get_client", no_grok)


async def _luna_user(fresh_db):
    await fresh_db.get_or_create_user(telegram_id=OWN, username="own")
    assert await db.set_user_ai_model(OWN, "luna")


class _FakeOpenAI:
    """client.post("/responses", ...) по сценарию: на каждый запрос — либо
    готовое тело ответа, либо (stream=True) список SSE-событий в той форме, в
    какой их отдаёт AsyncStream SDK 1.57: {"event": имя, "data": {...}}."""

    def __init__(self, script: list):
        self.script = list(script)
        self.requests: list[dict] = []

    async def post(self, path, *, body, cast_to, stream=False, stream_cls=None, options=None):
        assert path == "/responses"
        # Копия через JSON: ровно то, что ушло бы по сети, и без общих ссылок.
        self.requests.append(json.loads(json.dumps(body)))
        step = self.script.pop(0)
        if stream:
            assert stream_cls is not None
            return _events(step)
        assert not body.get("stream")
        return step


async def _events(events: list):
    for event in events:
        if isinstance(event, BaseException):
            raise event
        yield {"event": event["type"], "data": event}


def _round_body(output: list, usage: dict) -> dict:
    return {"id": "resp_x", "status": "completed", "output": output, "usage": usage}


async def _overview(user_id, name, args, **kwargs):
    assert name == "get_training_overview"
    return '{"workouts": 12}'


async def test_luna_loop_goes_to_responses_with_reasoning(fresh_db, own_luna, monkeypatch):
    await _luna_user(fresh_db)
    fake = _FakeOpenAI([
        _round_body([REASONING_1, CALL_1], USAGE_1),
        _round_body([REASONING_2, MESSAGE_2], USAGE_2),
    ])
    monkeypatch.setattr(ai_trainer, "_get_openai_chat_client", lambda: fake)
    monkeypatch.setattr(ai_trainer, "execute_tool", _overview)
    wire: dict = {}

    async def on_wire(messages):
        wire["messages"] = messages

    answer = await ai_trainer.ask(OWN, "Как прогресс?", [], on_wire=on_wire)
    assert answer == ANSWER

    first, second = fake.requests
    for body in (first, second):
        assert body["model"] == "gpt-6-luna"
        assert body["reasoning"] == {"effort": "low"}
        assert body["store"] is False
        assert body["include"] == ["reasoning.encrypted_content"]
        assert body["prompt_cache_key"] == f"trainer-{OWN}"
        assert body["max_output_tokens"] == 2048 + ai_trainer._OPENAI_REASONING_HEADROOM
        assert "messages" not in body and "reasoning_effort" not in body
    # Инструменты — в формате Responses: плоско, strict явно выключен.
    tool = next(t for t in first["tools"] if t["name"] == "get_training_overview")
    assert tool["type"] == "function" and "function" not in tool
    assert tool["strict"] is False and tool["parameters"]["type"] == "object"
    assert len(first["tools"]) == len(ai_trainer.TOOLS)
    # Шапка — системное сообщение, вопрос — user, в конце с датой.
    assert first["input"][0]["role"] == "system"
    assert first["input"][-1]["role"] == "user"
    assert first["input"][-1]["content"].startswith("Как прогресс?")

    # Второй раунд ДОПИСЫВАЕТ к первому: рассуждение и вызов — как пришли,
    # затем результат инструмента с тем же call_id.
    assert second["input"][: len(first["input"])] == first["input"]
    tail = second["input"][len(first["input"]):]
    assert tail == [
        REASONING_1, CALL_1,
        {"type": "function_call_output", "call_id": "call_abc", "output": '{"workouts": 12}'},
    ]
    assert not any(item.get("role") == "tool" for item in second["input"])

    # Цена — по usage Responses, рассуждение внутри output_tokens.
    llm = await db.get_llm_cost_breakdown(_today())
    assert llm["gpt-6-luna"]["calls"] == 2
    assert llm["gpt-6-luna"]["prompt_tokens"] == 25_000
    expected = (
        config.call_price_usd("gpt-6-luna", 12_000, 300, 0, 250)
        + config.call_price_usd("gpt-6-luna", 13_000, 900, 11_000, 400)
    )
    assert await db.get_cost_total_usd() == pytest.approx(expected)
    assert expected == pytest.approx(
        (12_000 + 2_000) / 1000 * 0.0001 + 11_000 / 1000 * 0.0001 * 0.1 + 1_200 / 1000 * 0.0005
    )

    # История хода: chat-форма для всех (grok её поймёт) + сырые элементы Luna.
    saved = wire["messages"]
    tool_round = next(m for m in saved if m.get("tool_calls"))
    assert tool_round[ai_trainer.OPENAI_OUTPUT_KEY] == [REASONING_1, CALL_1]
    assert tool_round["tool_calls"][0]["id"] == "call_abc"
    final = saved[-1]
    assert final["content"] == ANSWER
    assert final[ai_trainer.OPENAI_OUTPUT_KEY] == [REASONING_2, MESSAGE_2]
    assert "reasoning_content" not in final

    # Следующий ход отправляет прошлый input + ответ байт в байт: кэш живёт.
    fake.script.append(_round_body([MESSAGE_2], USAGE_2))
    await ai_trainer.ask(OWN, "А присед?", saved)
    third = fake.requests[-1]
    assert third["input"][: len(second["input"])] == second["input"]
    assert third["input"][len(second["input"]): len(second["input"]) + 2] == [REASONING_2, MESSAGE_2]


async def test_luna_stream_maps_to_chunks_and_statuses(fresh_db, own_luna, monkeypatch):
    await _luna_user(fresh_db)
    words = ["Жим растёт ", "третью неделю подряд, ", "присед стоит — ", "добавь второй день на ноги."]
    fake = _FakeOpenAI([
        [
            {"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "response.output_item.done", "output_index": 0, "item": REASONING_1},
            {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": "{}"},
            {"type": "response.output_item.done", "output_index": 1, "item": CALL_1},
            {"type": "response.completed",
             "response": _round_body([REASONING_1, CALL_1], USAGE_1)},
        ],
        [
            {"type": "response.output_item.done", "output_index": 0, "item": REASONING_2},
            *({"type": "response.output_text.delta", "output_index": 1, "delta": w} for w in words),
            {"type": "response.output_item.done", "output_index": 1, "item": MESSAGE_2},
            {"type": "response.completed",
             "response": _round_body([REASONING_2, MESSAGE_2], USAGE_2)},
        ],
    ])
    monkeypatch.setattr(ai_trainer, "_get_openai_chat_client", lambda: fake)
    monkeypatch.setattr(ai_trainer, "execute_tool", _overview)
    monkeypatch.setattr(ai_trainer, "STREAM_FLUSH_SECONDS", 0)
    chunks: list[str] = []
    statuses: list[str] = []

    async def on_chunk(text):
        chunks.append(text)

    async def on_status(text):
        statuses.append(text)

    answer = await ai_trainer.ask(OWN, "Как прогресс?", [], on_chunk=on_chunk, on_status=on_status)
    assert answer == ANSWER
    assert all(body["stream"] is True for body in fake.requests)
    assert fake.requests[1]["input"][-3:-1] == [REASONING_1, CALL_1]
    # Статус инструмента — из того же места, что у grok; текст — нарастающий.
    assert len(statuses) == 1 and statuses[0] in ai_trainer._tool_status_variants("get_training_overview")
    assert chunks and chunks[-1] == ANSWER
    assert all(ANSWER.startswith(c) for c in chunks)
    assert (await db.get_llm_cost_breakdown(_today()))["gpt-6-luna"]["calls"] == 2


async def test_luna_stream_abort_is_billed_by_estimate(fresh_db, own_luna, monkeypatch):
    await _luna_user(fresh_db)
    fake = _FakeOpenAI([[
        {"type": "response.output_text.delta", "delta": "Начал отвечать и "},
        asyncio.TimeoutError(),
    ]])
    monkeypatch.setattr(ai_trainer, "_get_openai_chat_client", lambda: fake)

    async def on_chunk(text):
        pass

    with pytest.raises(asyncio.TimeoutError):
        await ai_trainer.ask(OWN, "Как прогресс?", [], on_chunk=on_chunk)
    llm = await db.get_llm_cost_breakdown(_today())
    assert llm["gpt-6-luna"]["calls"] == 1
    assert llm["gpt-6-luna"]["prompt_tokens"] > 1_000  # оценка по длине запроса, не ноль


async def test_luna_stream_failed_event_raises_and_bills(fresh_db, own_luna, monkeypatch):
    await _luna_user(fresh_db)
    fake = _FakeOpenAI([[
        {"type": "response.failed", "response": {
            "status": "failed", "error": {"code": "server_error", "message": "boom"},
            "usage": {"input_tokens": 9_000, "output_tokens": 0},
        }},
    ]])
    monkeypatch.setattr(ai_trainer, "_get_openai_chat_client", lambda: fake)

    async def on_chunk(text):
        pass

    with pytest.raises(ai_trainer.ResponsesStreamError):
        await ai_trainer.ask(OWN, "Как прогресс?", [], on_chunk=on_chunk)
    assert (await db.get_llm_cost_breakdown(_today()))["gpt-6-luna"]["prompt_tokens"] == 9_000


async def test_grok_after_luna_gets_history_without_openai_items(fresh_db, own_luna, monkeypatch):
    """Атлет переключился обратно на grok: история Luna-хода едет chat-формой,
    без сырых элементов OpenAI."""
    await fresh_db.get_or_create_user(telegram_id=OWN, username="own")
    calls: list = []

    class _Completions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            message = SimpleNamespace(content="Ответ", tool_calls=None, model_extra={})
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    monkeypatch.setattr(ai_trainer, "_get_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions())))
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_abc", "type": "function",
             "function": {"name": "get_training_overview", "arguments": "{}"}}],
         ai_trainer.OPENAI_OUTPUT_KEY: [REASONING_1, CALL_1]},
        {"role": "tool", "tool_call_id": "call_abc", "content": "{}"},
        {"role": "assistant", "content": ANSWER, ai_trainer.OPENAI_OUTPUT_KEY: [REASONING_2, MESSAGE_2]},
    ]
    await ai_trainer.ask(OWN, "А присед?", history)
    (kwargs,) = calls
    assert not any(ai_trainer.OPENAI_OUTPUT_KEY in m for m in kwargs["messages"])
    assert kwargs["messages"][2]["tool_calls"][0]["id"] == "call_abc"
    assert kwargs["messages"][4] == {"role": "assistant", "content": ANSWER}


def test_grok_era_history_converts_to_responses_items():
    """Ход grok'а (или предзагрузка пресета) без сырых элементов — собирается
    из chat-формы; reasoning_content xAI в OpenAI не уезжает."""
    items = ai_trainer._responses_input([
        {"role": "system", "content": "шапка"},
        {"role": "user", "content": [
            {"type": "text", "text": "что на фото?"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
        ]},
        {"role": "assistant", "content": "Смотрю.", "reasoning_content": "думал",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "get_training_overview", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    ])
    assert items == [
        {"role": "system", "content": "шапка"},
        {"role": "user", "content": [
            {"type": "input_text", "text": "что на фото?"},
            {"type": "input_image", "image_url": "data:image/jpeg;base64,AAAA"},
        ]},
        {"role": "assistant", "content": "Смотрю."},
        {"type": "function_call", "call_id": "c1", "name": "get_training_overview", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "{}"},
    ]


# ---------- /v1/ai/ask стримом на Luna: те же события, что всегда ----------


def _sse(text: str) -> list[tuple[str, object]]:
    out = []
    for block in text.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        name = data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        out.append((name, data))
    return out


async def test_api_stream_on_luna_keeps_chunk_status_done(fresh_db, own_luna, monkeypatch):
    await _luna_user(fresh_db)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "STREAM_FLUSH_SECONDS", 0)
    fake = _FakeOpenAI([
        [
            {"type": "response.output_item.done", "item": REASONING_1},
            {"type": "response.output_item.done", "item": CALL_1},
            {"type": "response.completed", "response": _round_body([REASONING_1, CALL_1], USAGE_1)},
        ],
        [
            {"type": "response.output_text.delta", "delta": ANSWER},
            {"type": "response.completed", "response": _round_body([REASONING_2, MESSAGE_2], USAGE_2)},
        ],
    ])
    monkeypatch.setattr(ai_trainer, "_get_openai_chat_client", lambda: fake)
    monkeypatch.setattr(ai_trainer, "execute_tool", _overview)

    code = await fresh_db.issue_oauth_link_code(OWN, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    token = (await client.post("/auth/link", json={"code": code})).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    resp = await client.post("/ai/ask", json={"question": "Как прогресс?", "stream": True})
    assert resp.status_code == 200, resp.text
    events = _sse(resp.text)
    assert [name for name, _ in events] == ["status", "chunk", "done"]
    assert events[1][1] == {"text": ANSWER}
    assert events[2][1]["answer"] == ANSWER

    # История в базе несёт рассуждение — следующий вопрос вернёт его модели.
    saved = await db.get_ai_conversation_wire_history(OWN)
    assert saved[-1][ai_trainer.OPENAI_OUTPUT_KEY] == [REASONING_2, MESSAGE_2]
