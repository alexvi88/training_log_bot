"""Поддельный клиент Luna для тестов главного цикла — поверх chat-образных фейков.

Главный цикл тренера ходит в OpenAI Responses API (`client.post("/responses")`,
см. ai_trainer._responses_round), а сотни тестов написаны на фейках формы
chat/completions: `client.chat.completions.create(...)` отдаёт
`choices[0].message` (или стрим дельт). `as_luna(client)` переводит одно в
другое, ничего не подменяя в коде под тестом:

- тело запроса /responses → kwargs chat.completions.create: `messages`
  (input обратно в chat-сообщения: function_call → tool_calls ассистента,
  function_call_output → role=tool, input_text/input_image → text/image_url),
  `tools` (только если ушли в запросе), `stream`, `model`, `extra_body` с
  `reasoning_effort` и `prompt_cache_key`;
- ответ фейка → тело Responses (`output`: message с output_text и
  function_call на каждый tool_call, `usage` input/output_tokens), а стрим —
  в события `response.output_text.delta` … `response.completed` в той форме,
  в какой их отдаёт AsyncStream SDK ({"event": ..., "data": {...}}).

Проверки в тестах на `client.chat.completions.create.await_args` работают как
раньше: зовётся тот же объект. Сам перевод Responses ↔ chat проверяют тесты
test_ai_luna_responses.py на настоящих телах запросов.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any


def _chat_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    parts = []
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "input_text":
            parts.append({"type": "text", "text": part.get("text", "")})
        elif kind == "input_image":
            parts.append({"type": "image_url", "image_url": {"url": part.get("image_url")}})
        else:
            parts.append(part)
    return parts


def chat_messages(items: list[dict]) -> list[dict]:
    """input Responses API → chat-сообщения (обратный ход ai_trainer._responses_input)."""
    out: list[dict] = []
    for item in items:
        kind = item.get("type")
        if kind == "function_call":
            call = {
                "id": item.get("call_id"),
                "type": "function",
                "function": {"name": item.get("name"), "arguments": item.get("arguments")},
            }
            if out and out[-1].get("role") == "assistant" and "tool_calls" in out[-1]:
                out[-1]["tool_calls"].append(call)
            elif out and out[-1].get("role") == "assistant" and out[-1].get("_from_message"):
                out[-1]["tool_calls"] = [call]
            else:
                out.append({"role": "assistant", "content": "", "tool_calls": [call]})
        elif kind == "function_call_output":
            out.append({"role": "tool", "tool_call_id": item.get("call_id"), "content": item.get("output")})
        elif kind == "reasoning":
            continue
        elif kind == "message":
            text = "".join(
                c.get("text", "") for c in item.get("content") or [] if c.get("type") == "output_text"
            )
            out.append({"role": "assistant", "content": text, "_from_message": True})
        else:
            msg = {"role": item.get("role"), "content": _chat_content(item.get("content"))}
            if msg["role"] == "assistant":
                msg["_from_message"] = True
            out.append(msg)
    for m in out:
        m.pop("_from_message", None)
    return out


def chat_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                k: v for k, v in (
                    ("name", t.get("name")),
                    ("description", t.get("description")),
                    ("parameters", t.get("parameters")),
                ) if v is not None
            },
        }
        if t.get("type") == "function" and "name" in t else t
        for t in tools
    ]


def chat_kwargs(body: dict) -> dict:
    kwargs: dict[str, Any] = {
        "model": body.get("model"),
        "messages": chat_messages(body.get("input") or []),
        "max_completion_tokens": body.get("max_output_tokens"),
        "extra_body": {
            "reasoning_effort": (body.get("reasoning") or {}).get("effort"),
            **({"prompt_cache_key": body["prompt_cache_key"]} if "prompt_cache_key" in body else {}),
        },
    }
    if "tools" in body:
        kwargs["tools"] = chat_tools(body["tools"])
    if body.get("stream"):
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}
    return kwargs


def _usage_dict(usage: Any) -> dict:
    if usage is None:
        return {}
    details = getattr(usage, "prompt_tokens_details", None)
    out_details = getattr(usage, "completion_tokens_details", None)
    return {
        "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "input_tokens_details": {"cached_tokens": getattr(details, "cached_tokens", 0) or 0},
        "output_tokens_details": {"reasoning_tokens": getattr(out_details, "reasoning_tokens", 0) or 0},
    }


def _output(content: Any, tool_calls: list[Any]) -> list[dict]:
    items: list[dict] = []
    if content:
        items.append({
            "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": content, "annotations": []}],
        })
    for tc in tool_calls or []:
        fn = tc.function if hasattr(tc, "function") else SimpleNamespace(**tc["function"])
        items.append({
            "type": "function_call",
            "call_id": tc.id if hasattr(tc, "id") else tc["id"],
            "name": fn.name,
            "arguments": fn.arguments,
            "status": "completed",
        })
    return items


def responses_body(chat_response: Any) -> dict:
    message = chat_response.choices[0].message
    return {
        "status": "completed",
        "output": _output(getattr(message, "content", None), getattr(message, "tool_calls", None) or []),
        "usage": _usage_dict(getattr(chat_response, "usage", None)),
    }


async def _events(chat_stream: Any):
    parts: list[str] = []
    calls: dict[int, dict] = {}
    usage = None
    async for chunk in chat_stream:
        usage = getattr(chunk, "usage", None) or usage
        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta
        for tc in getattr(delta, "tool_calls", None) or []:
            slot = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
            slot["id"] += tc.id or ""
            if tc.function:
                slot["name"] += tc.function.name or ""
                slot["arguments"] += tc.function.arguments or ""
        if getattr(delta, "content", None):
            parts.append(delta.content)
            yield {"event": "response.output_text.delta",
                   "data": {"type": "response.output_text.delta", "delta": delta.content}}
    tool_calls = [
        SimpleNamespace(id=s["id"], function=SimpleNamespace(name=s["name"], arguments=s["arguments"]))
        for s in calls.values()
    ]
    response = {"status": "completed", "output": _output("".join(parts), tool_calls),
                "usage": _usage_dict(usage)}
    yield {"event": "response.completed", "data": {"type": "response.completed", "response": response}}


class LunaFromChat:
    """Клиент с `.post("/responses")` поверх chat-образного фейка (см. модуль)."""

    def __init__(self, chat_client: Any):
        self._inner = chat_client
        self.requests: list[dict] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def post(self, path, *, body, cast_to=None, stream=False, stream_cls=None, options=None):
        assert path == "/responses", path
        self.requests.append(json.loads(json.dumps(body)))
        result = await self._inner.chat.completions.create(**chat_kwargs(body))
        if stream:
            return _events(result)
        return responses_body(result)


def as_luna(chat_client: Any) -> LunaFromChat:
    return chat_client if isinstance(chat_client, LunaFromChat) else LunaFromChat(chat_client)


class ResponsesScript:
    """Клиент, который на каждый POST /responses отдаёт следующее готовое тело
    ответа Responses API (нестрим) и запоминает тела запросов — для тестов,
    которым нужен именно Responses-формат: рассуждение (reasoning с
    encrypted_content), вызовы web_search и т.п."""

    def __init__(self, bodies: list):
        self.bodies = list(bodies)
        self.requests: list[dict] = []
        self.options: list = []

    async def post(self, path, *, body, cast_to=None, stream=False, stream_cls=None, options=None):
        assert path == "/responses", path
        assert not stream, "ResponsesScript — только нестрим"
        self.requests.append(json.loads(json.dumps(body)))
        self.options.append(options)
        step = self.bodies.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def web_search_body(text: str, searches: int = 1, usage: dict | None = None) -> dict:
    """Тело ответа живого поиска: `searches` вызовов web_search и текст находок."""
    return {
        "status": "completed",
        "output": [
            *({"type": "web_search_call", "id": f"ws_{i}", "status": "completed"} for i in range(searches)),
            {"type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": text, "annotations": []}]},
        ],
        "usage": usage or {"input_tokens": 3000, "output_tokens": 400},
    }


def chat_view(messages: list[dict]) -> list[dict]:
    """История без сырых элементов Responses (ai_trainer.OPENAI_OUTPUT_KEY) —
    то, что видно в chat-форме: для сравнения с тем, что получил chat-фейк."""
    return [{k: v for k, v in m.items() if k != "openai_output"} for m in messages]
