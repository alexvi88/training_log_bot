"""Модель тренера: grok по умолчанию, GPT-6 Luna — только своим аккаунтам.

Что держат эти тесты:

- выбор бэкенда по пользователю (`ai_trainer.chat_backend`) и то, что чужой
  аккаунт Luna не включит ни записью (`db.set_user_ai_model`, `PATCH
  /v1/ai/model` → 403), ни чтением (значение в базе игнорируется);
- grok-путь не изменился: те же параметры запроса и тот же объект сообщений —
  кэш xAI у обычных атлетов не трогается;
- ход фиксирует модель: переключение посреди хода действует со следующего;
- цена Luna: своя доля кэша (0.1), размышления не прибавляются сверху (у
  OpenAI они уже внутри completion_tokens), cost-событие с именем модели;
- живой поиск: Luna-аккаунт — OpenAI Responses API с web_search, grok —
  xAI, формат находок один (текст), вызовы поиска считаются по своей ставке.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import httpx
import pytest

import admin_tasks
import ai_trainer
import api_v1
import config
import db

OWN = 111
STRANGER = 222


@pytest.fixture
def own_accounts(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 1)
    monkeypatch.setattr(config, "TEST_USER_ID", OWN)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")


async def _users(fresh_db):
    for uid in (OWN, STRANGER):
        await fresh_db.get_or_create_user(telegram_id=uid, username=f"u{uid}")


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


# ---------- кто может выбрать Luna ----------


async def test_stranger_cannot_switch_to_luna(fresh_db, own_accounts):
    await _users(fresh_db)
    assert await db.set_user_ai_model(STRANGER, "luna") is False
    assert await db.get_user_ai_model(STRANGER) is None
    assert (await ai_trainer.chat_backend(STRANGER)).name == "grok"


async def test_stranger_value_in_db_is_ignored_on_read(fresh_db, own_accounts):
    """Даже если значение как-то попало в базу — чужому всегда grok."""
    await _users(fresh_db)
    await db.conn().execute("UPDATE users SET ai_model = 'luna' WHERE telegram_id = ?", (STRANGER,))
    await db.conn().commit()
    assert await ai_trainer.ai_model_for(STRANGER) == "grok"
    assert (await ai_trainer.chat_backend(STRANGER)).model == config.GROK_MODEL


async def test_own_account_switches_both_ways(fresh_db, own_accounts):
    await _users(fresh_db)
    assert (await ai_trainer.chat_backend(OWN)).name == "grok"
    assert await db.set_user_ai_model(OWN, "luna") is True
    backend = await ai_trainer.chat_backend(OWN)
    assert backend.name == "luna"
    assert backend.model == config.OPENAI_CHAT_MODEL == "gpt-6-luna"
    assert backend.effort == "low" and backend.quick_effort == "none"
    await db.set_user_ai_model(OWN, "grok")
    # grok хранится как NULL — «по умолчанию».
    assert await db.get_user_ai_model(OWN) is None
    assert (await ai_trainer.chat_backend(OWN)).name == "grok"


async def test_unknown_token_is_rejected(fresh_db, own_accounts):
    await _users(fresh_db)
    with pytest.raises(ValueError):
        await db.set_user_ai_model(OWN, "gpt")


async def test_luna_without_openai_key_falls_back_to_grok(fresh_db, own_accounts, monkeypatch):
    await _users(fresh_db)
    await db.set_user_ai_model(OWN, "luna")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    assert (await ai_trainer.chat_backend(OWN)).name == "grok"


# ---------- /v1 ----------


async def _api_client(fresh_db, telegram_id):
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def test_api_stranger_gets_no_fields_and_403(fresh_db, own_accounts):
    await _users(fresh_db)
    client = await _api_client(fresh_db, STRANGER)
    me = (await client.get("/me")).json()
    assert "ai_model" not in me and "ai_model_options" not in me
    resp = await client.patch("/ai/model", json={"ai_model": "luna"})
    assert resp.status_code == 403
    assert resp.json()["message"]  # человеческий текст на языке атлета
    assert await db.get_user_ai_model(STRANGER) is None


async def test_api_own_account_sees_and_switches(fresh_db, own_accounts):
    await _users(fresh_db)
    client = await _api_client(fresh_db, OWN)
    me = (await client.get("/me")).json()
    assert me["ai_model"] == "grok"
    assert me["ai_model_options"] == ["grok", "luna"]
    resp = await client.patch("/ai/model", json={"ai_model": "luna"})
    assert resp.status_code == 200
    assert resp.json() == {"ai_model": "luna", "ai_model_options": ["grok", "luna"]}
    assert (await client.get("/me")).json()["ai_model"] == "luna"
    assert (await client.patch("/ai/model", json={"ai_model": "o3"})).status_code == 400


# ---------- grok-путь не изменился ----------


def test_grok_request_params_are_unchanged():
    backend = ai_trainer.ChatBackend(
        client=object(), model=config.GROK_MODEL, effort=config.GROK_REASONING_EFFORT,
        quick_effort=config.GROK_QUICK_REASONING_EFFORT, name="grok",
    )
    assert ai_trainer._model_kwargs(backend, 5, max_tokens=2048, quick=False, scope="trainer") == {
        "model": config.GROK_MODEL,
        "max_tokens": 2048,
        "extra_body": {"reasoning_effort": config.GROK_REASONING_EFFORT},
        "extra_headers": {"x-grok-conv-id": "trainer-5"},
    }
    messages = [{"role": "assistant", "content": "x", "reasoning_content": "думал"}]
    # Тот же объект — ни байта префикса не переписано.
    assert ai_trainer._wire_messages(backend, messages) is messages


def test_luna_request_params(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    backend = ai_trainer.luna_backend()
    main = ai_trainer._model_kwargs(backend, 5, max_tokens=2048, quick=False, scope="trainer")
    assert main["model"] == "gpt-6-luna"
    assert "max_tokens" not in main and "extra_headers" not in main
    assert main["max_completion_tokens"] == 2048 + ai_trainer._OPENAI_REASONING_HEADROOM
    assert main["extra_body"] == {"reasoning_effort": "low", "prompt_cache_key": "trainer-5"}
    quick = ai_trainer._model_kwargs(backend, 5, max_tokens=700, quick=True, scope="workout_comment")
    assert quick["max_completion_tokens"] == 700
    assert quick["extra_body"]["reasoning_effort"] == "none"
    stripped = ai_trainer._wire_messages(
        backend, [{"role": "assistant", "content": "x", "reasoning_content": "думал"}]
    )
    assert stripped == [{"role": "assistant", "content": "x"}]


# ---------- ход фиксирует модель, cost-событие несёт имя ----------


class _FakeCompletions:
    def __init__(self, label: str, calls: list, tool_first: bool):
        self.label = label
        self.calls = calls
        self.tool_first = tool_first

    async def create(self, **kwargs):
        self.calls.append((self.label, kwargs))
        usage = SimpleNamespace(
            prompt_tokens=10_000, completion_tokens=1_000,
            prompt_tokens_details=SimpleNamespace(cached_tokens=8_000),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=400),
        )
        if self.tool_first:
            self.tool_first = False
            tc = SimpleNamespace(
                id="c1", function=SimpleNamespace(name="get_training_overview", arguments="{}")
            )
            message = SimpleNamespace(content="", tool_calls=[tc], model_extra={})
        else:
            message = SimpleNamespace(content="Ответ", tool_calls=None, model_extra={})
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def _fake_client(label, calls, tool_first=False):
    return SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(label, calls, tool_first)))


async def test_turn_keeps_its_model_when_switched_mid_turn(fresh_db, own_accounts, monkeypatch):
    await _users(fresh_db)
    await db.set_user_ai_model(OWN, "luna")
    calls: list = []
    shared = _FakeCompletions("?", calls, tool_first=True)
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=_relabel(shared, "grok"))))
    monkeypatch.setattr(ai_trainer, "_get_openai_chat_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=_relabel(shared, "luna"))))

    async def tool_switches_model(user_id, name, args, **kwargs):
        # Посреди хода атлет переключился на grok — ход доезжает на Luna.
        await db.set_user_ai_model(OWN, "grok")
        return "{}"

    monkeypatch.setattr(ai_trainer, "execute_tool", tool_switches_model)
    answer = await ai_trainer.ask(OWN, "Как мой прогресс?", [])
    assert answer == "Ответ"
    assert [label for label, _ in calls] == ["luna", "luna"]
    assert all(kw["model"] == "gpt-6-luna" for _, kw in calls)

    # Следующий ход — уже grok.
    calls.clear()
    await ai_trainer.ask(OWN, "Ещё вопрос", [])
    assert [label for label, _ in calls] == ["grok"]
    assert calls[0][1]["model"] == config.GROK_MODEL

    breakdown = await db.get_llm_cost_breakdown(_today())
    assert breakdown["gpt-6-luna"]["calls"] == 2
    assert breakdown[config.GROK_MODEL]["calls"] == 1


def _relabel(shared: _FakeCompletions, label: str):
    class _Proxy:
        async def create(self, **kwargs):
            shared.label = label
            return await shared.create(**kwargs)

    return _Proxy()


async def test_stranger_turn_goes_to_grok_with_unchanged_params(fresh_db, own_accounts, monkeypatch):
    await _users(fresh_db)
    calls: list = []
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: _fake_client("grok", calls))

    def boom():
        raise AssertionError("чужой аккаунт не должен трогать OpenAI-клиент")

    monkeypatch.setattr(ai_trainer, "_get_openai_chat_client", boom)
    history = [{"role": "user", "content": "q"},
               {"role": "assistant", "content": "a", "reasoning_content": "думал"}]
    await ai_trainer.ask(STRANGER, "Как мой прогресс?", history)
    (_label, kwargs), = calls
    assert kwargs["model"] == config.GROK_MODEL
    assert kwargs["max_tokens"] == 2048
    assert kwargs["extra_body"] == {"reasoning_effort": config.GROK_REASONING_EFFORT}
    assert kwargs["extra_headers"] == {"x-grok-conv-id": f"trainer-{STRANGER}"}
    # reasoning_content в истории уехал grok'у как был — это и держит его кэш.
    assert kwargs["messages"][2]["reasoning_content"] == "думал"


async def test_workout_comment_uses_luna_and_logs_its_model(fresh_db, own_accounts, monkeypatch):
    await _users(fresh_db)
    await db.set_user_ai_model(OWN, "luna")
    calls: list = []
    monkeypatch.setattr(ai_trainer, "_get_openai_chat_client", lambda: _fake_client("luna", calls))
    workout_id = await db.create_workout(OWN)
    await db.finish_workout(workout_id)
    await ai_trainer.comment_on_workout(OWN, workout_id)
    (_label, kwargs), = calls
    assert kwargs["model"] == "gpt-6-luna"
    assert kwargs["extra_body"]["reasoning_effort"] == "none"
    assert (await db.get_llm_cost_breakdown(_today()))["gpt-6-luna"]["calls"] == 1


# ---------- цена ----------


def test_luna_price_with_cache_and_reasoning():
    inp, out = config.LLM_PRICES_USD_PER_1K["gpt-6-luna"]
    assert (inp, out) == (0.0001, 0.0005)
    price = config.call_price_usd("gpt-6-luna", 10_000, 1_000, cached_tokens=8_000, reasoning_tokens=400)
    # Свежий вход по полной, кэш — 0.1 входа, размышления уже внутри 1000 выхода.
    expected = 2_000 / 1000 * inp + 8_000 / 1000 * inp * 0.1 + 1_000 / 1000 * out
    assert price == pytest.approx(expected)


def test_cache_multiplier_is_per_model():
    assert config.cached_input_multiplier("gpt-6-luna") == 0.1
    assert config.cached_input_multiplier(config.GROK_MODEL) == config.CACHED_INPUT_PRICE_MULTIPLIER
    # grok по-прежнему прибавляет размышления сверху (у xAI они отдельный тип).
    _inp, out = config.LLM_PRICES_USD_PER_1K["grok-4.5-latest"]
    assert config.call_price_usd("grok-4.5-latest", 0, 0, reasoning_tokens=1_000) == pytest.approx(out)


async def test_daily_report_breaks_cost_down_by_model(fresh_db):
    await db.log_cost_event(1, "llm_call", model="gpt-6-luna", prompt_tokens=10_000, completion_tokens=500)
    await db.log_cost_event(1, "llm_call", model=config.GROK_MODEL, prompt_tokens=10_000, completion_tokens=500)
    report = await admin_tasks._build_cost_report(_today())
    luna_cost = config.call_price_usd("gpt-6-luna", 10_000, 500)
    assert f"gpt-6-luna: 1 (10 500 ток., ~${luna_cost:.3f})" in report
    assert f"{config.GROK_MODEL}: 1" in report


# ---------- живой поиск ----------


def _responses_payload(search_calls: int, text: str) -> dict:
    return {
        "output": [
            *({"type": "web_search_call", "status": "completed"} for _ in range(search_calls)),
            {"type": "message", "content": [{"type": "output_text", "text": text, "annotations": []}]},
        ],
        "usage": {
            "input_tokens": 6_000, "output_tokens": 800,
            "input_tokens_details": {"cached_tokens": 1_000},
            "output_tokens_details": {"reasoning_tokens": 300},
        },
    }


async def test_luna_search_goes_to_openai_responses(fresh_db, own_accounts, monkeypatch):
    await _users(fresh_db)
    posted: list = []

    async def fake_post(path, *, body, cast_to, options):
        posted.append((path, body, options))
        return _responses_payload(2, "  Свежий обзор (2026-09-01, example.com): креатин работает.  ")

    monkeypatch.setattr(ai_trainer, "_get_openai_chat_client", lambda: SimpleNamespace(post=fake_post))

    async def no_xai():
        raise AssertionError("Luna-аккаунт не должен идти в xAI-поиск")

    monkeypatch.setattr(ai_trainer, "_get_sdk_client", no_xai)
    found = await ai_trainer._web_search_findings(
        OWN, "что нового по креатину?", [], backend=ai_trainer.luna_backend()
    )
    assert found == "Свежий обзор (2026-09-01, example.com): креатин работает."
    (path, body, options), = posted
    assert path == "/responses"
    assert body["tools"] == [{"type": "web_search"}]
    assert body["model"] == config.OPENAI_SEARCH_MODEL
    assert body["instructions"].startswith(ai_trainer.SEARCH_SYSTEM_PROMPT[:40])
    assert options == {"timeout": config.AI_SEARCH_TIMEOUT_SECONDS}

    # Токены по ставке модели + каждый вызов web_search по своей.
    tools = await db.get_server_tool_count(_today())
    assert tools == {config.OPENAI_WEB_SEARCH_TOOL: 2}
    llm = await db.get_llm_cost_breakdown(_today())
    assert llm["gpt-6-luna"]["prompt_tokens"] == 6_000
    expected = (
        config.call_price_usd("gpt-6-luna", 6_000, 800, 1_000, 300)
        + 2 * config.OPENAI_WEB_SEARCH_USD_PER_CALL
    )
    assert await db.get_cost_total_usd() == pytest.approx(expected)
    assert config.OPENAI_WEB_SEARCH_USD_PER_CALL == 0.01
    assert await db.get_ai_search_count_today(OWN) == 1


async def test_luna_search_without_web_calls_is_not_findings(fresh_db, own_accounts, monkeypatch):
    await _users(fresh_db)

    async def fake_post(path, *, body, cast_to, options):
        return _responses_payload(0, "Из памяти.")

    monkeypatch.setattr(ai_trainer, "_get_openai_chat_client", lambda: SimpleNamespace(post=fake_post))
    assert await ai_trainer._web_search_findings(OWN, "q", [], backend=ai_trainer.luna_backend()) is None


async def test_grok_search_stays_on_xai_with_same_output_shape(fresh_db, own_accounts, monkeypatch):
    await _users(fresh_db)

    class _Session:
        async def sample(self):
            return SimpleNamespace(
                content="  Свежий обзор (2026-09-01, example.com): креатин работает.  ",
                citations=["https://example.com"], server_side_tool_usage={"web_search": 1},
                usage=None,
            )

    class _Sdk:
        chat = SimpleNamespace(create=lambda **kwargs: _Session())

    async def sdk():
        return _Sdk()

    monkeypatch.setattr(ai_trainer, "_get_sdk_client", sdk)

    def no_openai():
        raise AssertionError("grok-аккаунт не должен идти в OpenAI")

    monkeypatch.setattr(ai_trainer, "_get_openai_chat_client", no_openai)
    found = await ai_trainer._web_search_findings(STRANGER, "q", [], backend=ai_trainer.grok_backend())
    # Тот же формат, что у Luna-пути: обрезанный текст находок.
    assert found == "Свежий обзор (2026-09-01, example.com): креатин работает."
    assert await db.get_server_tool_count(_today()) == {"web_search": 1}


async def test_aborted_openai_search_is_billed_at_openai_rate(fresh_db, own_accounts, monkeypatch):
    import asyncio

    await _users(fresh_db)

    async def slow_post(path, *, body, cast_to, options):
        raise asyncio.TimeoutError

    monkeypatch.setattr(ai_trainer, "_get_openai_chat_client", lambda: SimpleNamespace(post=slow_post))
    assert await ai_trainer._web_search_findings(OWN, "q", [], backend=ai_trainer.luna_backend()) is None
    assert await db.get_server_tool_count(_today()) == {config.OPENAI_WEB_SEARCH_TOOL: 1}
