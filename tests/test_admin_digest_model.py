"""Суточный разбор лога для админа идёт старшей моделью OpenAI, а без ключа —
прежним бэкендом; цена пишется под настоящим именем модели."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ai_trainer
import config
import db


def _fake_client(text: str = "Разбор."):
    create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=15_000, completion_tokens=3_000),
    ))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), create


async def _cost_models() -> list[tuple]:
    cur = await db.conn().execute("SELECT model, source FROM cost_events")
    return [tuple(r) for r in await cur.fetchall()]


def test_admin_digest_model_priced_from_its_own_row():
    """$2.00 вход / $0.10 кэш / $10.00 выход за 1M — в таблице за 1K."""
    model = "gpt-6.1-sol"
    assert config.LLM_PRICES_USD_PER_1K[model] == pytest.approx((0.002, 0.01))
    assert config.cached_input_multiplier(model) == pytest.approx(0.05)
    assert model in config.REASONING_INCLUDED_IN_COMPLETION
    # 10к входа, из них 4к из кэша, 1к выхода (размышления уже внутри).
    price = config.call_price_usd(model, 10_000, 1_000, cached_tokens=4_000, reasoning_tokens=500)
    assert price == pytest.approx(6_000 * 2.00 / 1e6 + 4_000 * 0.10 / 1e6 + 1_000 * 10.00 / 1e6)


@pytest.mark.asyncio
async def test_digest_goes_to_openai_admin_model(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(config, "OPENAI_ADMIN_DIGEST_MODEL", "gpt-6.1-sol")
    monkeypatch.setattr(config, "OPENAI_ADMIN_DIGEST_REASONING_EFFORT", "medium")
    client, create = _fake_client()
    monkeypatch.setattr(ai_trainer, "_get_openai_chat_client", lambda: client)
    grok = AsyncMock()
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: grok)

    text = await ai_trainer.behaviour_digest("сводка", "память")

    assert text == "Разбор."
    kwargs = create.call_args.kwargs
    assert kwargs["model"] == "gpt-6.1-sol"
    assert kwargs["extra_body"] == {"reasoning_effort": "medium"}
    assert "max_tokens" not in kwargs
    assert kwargs["max_completion_tokens"] > 1200
    # Промпт и порядок сообщений — прежние.
    assert kwargs["messages"] == [
        {"role": "system", "content": ai_trainer.BEHAVIOUR_DIGEST_SYSTEM_PROMPT},
        {"role": "user", "content": "память"},
        {"role": "user", "content": "сводка"},
    ]
    assert await _cost_models() == [("gpt-6.1-sol", "admin")]


@pytest.mark.asyncio
async def test_digest_falls_back_without_openai_key(fresh_db, monkeypatch, caplog):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(config, "XAI_API_KEY", "xai-test")
    client, create = _fake_client()
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    with caplog.at_level("WARNING", logger=ai_trainer.logger.name):
        text = await ai_trainer.behaviour_digest("сводка")

    assert text == "Разбор."
    assert create.call_args.kwargs["model"] == config.GROK_MODEL
    assert any("OPENAI_API_KEY" in r.getMessage() for r in caplog.records)
    assert await _cost_models() == [(config.GROK_MODEL, "admin")]


@pytest.mark.asyncio
async def test_digest_silent_when_nothing_configured(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(config, "XAI_API_KEY", "")
    assert await ai_trainer.behaviour_digest("сводка") is None
