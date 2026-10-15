"""Модель тренера одна — OpenAI GPT-6 Luna; xAI/Grok выпилен, выбора модели нет.

Бывший test_ai_model_switch.py (grok по умолчанию, Luna — своим аккаунтам),
переведённый на один путь. Что держат эти тесты:

- любой аккаунт — свой и чужой — идёт в Luna (`ai_trainer.chat_backend`),
  значение users.ai_model в базе ни на что не влияет;
- переключателя нет: /me не отдаёт ai_model/ai_model_options (iOS строку
  «Модель тренера» не показывает), PATCH /v1/ai/model → 410 с текстом на
  языке атлета и ничего не пишет;
- параметры запроса Luna: max_completion_tokens, reasoning_effort и
  prompt_cache_key в extra_body; поле прежнего провайдера reasoning_content
  из старой истории не уезжает;
- ход идёт в Responses API, cost-события несут имя модели;
- цена: доля кэша Luna 0.1, размышления не прибавляются сверху; исторические
  строки grok-* в таблице цен считают старые cost_events по своей ставке;
- живой поиск — только OpenAI Responses с web_search; вызовы поиска — по своей
  ставке; оборванный — оценкой по ставке OpenAI.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import httpx
import pytest
from luna_fakes import ResponsesScript, as_luna, web_search_body

import admin_tasks
import ai_trainer
import api_v1
import config
import db
import i18n

OWN = 111
STRANGER = 222


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 1)
    monkeypatch.setattr(config, "TEST_USER_ID", OWN)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")


async def _users(fresh_db):
    for uid in (OWN, STRANGER):
        await fresh_db.get_or_create_user(telegram_id=uid, username=f"u{uid}")


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


# ---------- одна модель на всех ----------


async def test_every_account_gets_luna(fresh_db, keys):
    await _users(fresh_db)
    for uid in (OWN, STRANGER, None):
        backend = await ai_trainer.chat_backend(uid)
        assert backend.model == config.OPENAI_CHAT_MODEL == "gpt-6-luna"
        # Главный цикл рассуждает (Responses API), быстрые вызовы — нет.
        assert backend.effort == config.OPENAI_REASONING_EFFORT == "medium" and backend.quick_effort == "none"


async def test_old_ai_model_value_in_db_changes_nothing(fresh_db, keys):
    """Колонка users.ai_model осталась (миграции только добавляют), но её никто
    не читает: ни 'luna', ни прежнее значение не выбирают модель."""
    await _users(fresh_db)
    await db.conn().execute("UPDATE users SET ai_model = 'grok' WHERE telegram_id = ?", (STRANGER,))
    await db.conn().commit()
    assert (await ai_trainer.chat_backend(STRANGER)).model == config.OPENAI_CHAT_MODEL


async def test_without_openai_key_the_trainer_is_not_configured(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    assert not ai_trainer.is_configured()
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    assert ai_trainer.is_configured()


def test_no_xai_left_in_config_or_trainer():
    for name in ("XAI_API_KEY", "GROK_MODEL", "GROK_BASE_URL", "GROK_SEARCH_MODEL",
                 "GROK_REASONING_EFFORT", "AI_MODEL_CHOICES", "ai_model_switch_ids"):
        assert not hasattr(config, name), name
    for name in ("grok_backend", "ai_model_for", "_get_sdk_client", "_cache_headers"):
        assert not hasattr(ai_trainer, name), name


# ---------- /v1: переключателя нет ----------


async def _api_client(fresh_db, telegram_id):
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


@pytest.mark.parametrize("uid", [OWN, STRANGER])
@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_api_has_no_model_switch(fresh_db, keys, uid, lang):
    await _users(fresh_db)
    await fresh_db.set_user_lang(uid, lang)
    client = await _api_client(fresh_db, uid)
    me = (await client.get("/me")).json()
    assert "ai_model" not in me and "ai_model_options" not in me
    resp = await client.patch("/ai/model", json={"ai_model": "luna"})
    assert resp.status_code == 410
    assert resp.json()["error"] == "ai_model_gone"
    assert resp.json()["message"] == i18n.t_in(lang, "api.error.ai_model_gone")
    cur = await db.conn().execute("SELECT ai_model FROM users WHERE telegram_id = ?", (uid,))
    assert (await cur.fetchone())[0] is None


# ---------- параметры запроса ----------


def test_luna_request_params(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    backend = ai_trainer.luna_backend()
    quick = ai_trainer._model_kwargs(backend, 5, max_tokens=700, quick=True, scope="workout_comment")
    assert quick["model"] == "gpt-6-luna"
    assert "max_tokens" not in quick and "extra_headers" not in quick
    assert quick["max_completion_tokens"] == 700  # effort none — запаса на размышления нет
    assert quick["extra_body"] == {"reasoning_effort": "none", "prompt_cache_key": "workout_comment-5"}
    # Без пользователя — без ключа кэша (суточный разбор логов админу).
    assert "prompt_cache_key" not in ai_trainer._model_kwargs(backend, None, max_tokens=10, quick=True)["extra_body"]
    stripped = ai_trainer._wire_messages(
        [{"role": "assistant", "content": "x", "reasoning_content": "думал"}]
    )
    assert stripped == [{"role": "assistant", "content": "x"}]
    clean = [{"role": "user", "content": "q"}]
    assert ai_trainer._wire_messages(clean) is clean  # нечего снимать — тот же объект


# ---------- ход: Responses API, cost-событие несёт имя модели ----------


class _FakeCompletions:
    def __init__(self, calls: list, tool_first: bool):
        self.calls = calls
        self.tool_first = tool_first

    async def create(self, **kwargs):
        self.calls.append(kwargs)
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
            message = SimpleNamespace(content="", tool_calls=[tc])
        else:
            message = SimpleNamespace(content="Ответ", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def _fake_client(calls, tool_first=False):
    return SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(calls, tool_first)))


async def test_stranger_turn_goes_to_luna_responses(fresh_db, keys, monkeypatch):
    await _users(fresh_db)
    calls: list = []
    client = as_luna(_fake_client(calls, tool_first=True))
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    async def overview(user_id, name, args, **kwargs):
        return "{}"

    monkeypatch.setattr(ai_trainer, "execute_tool", overview)
    history = [{"role": "user", "content": "q"},
               {"role": "assistant", "content": "a", "reasoning_content": "думал прежней моделью"}]
    answer = await ai_trainer.ask(STRANGER, "Как мой прогресс?", history)

    assert answer == "Ответ"
    assert len(client.requests) == 2
    body = client.requests[0]
    assert body["model"] == "gpt-6-luna"
    assert body["reasoning"] == {"effort": config.OPENAI_REASONING_EFFORT}
    assert body["prompt_cache_key"] == f"trainer-{STRANGER}"
    assert body["store"] is False
    assert "думал прежней моделью" not in str(body["input"])

    breakdown = await db.get_llm_cost_breakdown(_today())
    assert breakdown["gpt-6-luna"]["calls"] == 2


async def test_workout_comment_uses_luna_and_logs_its_model(fresh_db, keys, monkeypatch):
    await _users(fresh_db)
    calls: list = []
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: _fake_client(calls))
    workout_id = await db.create_workout(STRANGER)
    await db.finish_workout(workout_id)
    await ai_trainer.comment_on_workout(STRANGER, workout_id)
    kwargs, = calls
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


def test_historical_grok_rows_still_price_old_events():
    """xAI больше не вызывается, но cost_events за прошлые дни лежат с именами
    grok-* — их строки цены и доля кэша остаются историческими."""
    assert config.cached_input_multiplier("gpt-6-luna") == 0.1
    assert config.cached_input_multiplier("grok-4.5-latest") == config.CACHED_INPUT_PRICE_MULTIPLIER == 0.15
    # grok по-прежнему прибавляет размышления сверху (у xAI они были отдельный тип).
    _inp, out = config.LLM_PRICES_USD_PER_1K["grok-4.5-latest"]
    assert config.call_price_usd("grok-4.5-latest", 0, 0, reasoning_tokens=1_000) == pytest.approx(out)
    # Исторический xAI web_search — своя ставка, OpenAI — своя.
    assert config.server_tool_price_usd("web_search") == 0.005
    assert config.server_tool_price_usd(config.OPENAI_WEB_SEARCH_TOOL) == 0.01


async def test_daily_report_breaks_cost_down_by_model(fresh_db):
    await db.log_cost_event(1, "llm_call", model="gpt-6-luna", prompt_tokens=10_000, completion_tokens=500)
    await db.log_cost_event(1, "llm_call", model="grok-4.5-latest", prompt_tokens=10_000, completion_tokens=500)
    report = await admin_tasks._build_cost_report(_today())
    luna_cost = config.call_price_usd("gpt-6-luna", 10_000, 500)
    assert f"gpt-6-luna: 1 (10 500 ток., ~${luna_cost:.3f})" in report
    assert "grok-4.5-latest: 1" in report


# ---------- живой поиск ----------


async def test_search_goes_to_openai_responses(fresh_db, keys, monkeypatch):
    await _users(fresh_db)
    client = ResponsesScript([web_search_body(
        "  Свежий обзор (2026-09-01, example.com): креатин работает.  ", searches=2,
        usage={
            "input_tokens": 6_000, "output_tokens": 800,
            "input_tokens_details": {"cached_tokens": 1_000},
            "output_tokens_details": {"reasoning_tokens": 300},
        },
    )])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    found = await ai_trainer._web_search_findings(STRANGER, "что нового по креатину?", [])
    assert found == "Свежий обзор (2026-09-01, example.com): креатин работает."
    body, = client.requests
    assert body["tools"] == [{"type": "web_search"}]
    assert body["model"] == config.OPENAI_SEARCH_MODEL
    assert body["instructions"].startswith(ai_trainer.SEARCH_SYSTEM_PROMPT[:40])
    assert "X-поиск" not in ai_trainer.SEARCH_SYSTEM_PROMPT and "веб/X" not in ai_trainer.SEARCH_SYSTEM_PROMPT
    assert client.options == [{"timeout": config.AI_SEARCH_TIMEOUT_SECONDS}]

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
    assert await db.get_ai_search_count_today(STRANGER) == 1


async def test_search_without_web_calls_is_not_findings(fresh_db, keys, monkeypatch):
    await _users(fresh_db)
    client = ResponsesScript([web_search_body("Из памяти.", searches=0)])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    assert await ai_trainer._web_search_findings(STRANGER, "q", []) is None


async def test_aborted_search_is_billed_at_openai_rate(fresh_db, keys, monkeypatch):
    import asyncio

    await _users(fresh_db)
    client = ResponsesScript([asyncio.TimeoutError()])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    assert await ai_trainer._web_search_findings(STRANGER, "q", []) is None
    assert await db.get_server_tool_count(_today()) == {config.OPENAI_WEB_SEARCH_TOOL: 1}
