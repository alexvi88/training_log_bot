"""Деплой обрывает вызов модели посреди ответа, а запись его цены приходит,
когда база уже закрыта: вместо трейсбека — одна строка в лог."""

import logging
from types import SimpleNamespace

import pytest

import ai_trainer
import db

pytestmark = pytest.mark.asyncio


async def test_cost_log_after_db_closed_is_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(db, "_conn", None)
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=5)
    with caplog.at_level(logging.WARNING, logger="ai_trainer"):
        await ai_trainer._log_llm_cost(1, "gpt-6-luna", usage)
    assert "db closed" in caplog.text
    assert "Traceback" not in caplog.text
