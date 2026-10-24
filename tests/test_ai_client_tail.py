"""Тренер знает, где идёт разговор, — а шапка запроса от клиента не зависит.

Владелец: пользователю приложения Telegram не нужен ни в одном сценарии. Тренер
же представлялся «AI-тренером в Telegram-боте» и слал человека из приложения в
«⚙️ Настройки → 📥 Импорт CSV» и к команде /feedback. Механика клиента теперь
едет строкой «Клиент: …» в хвосте последнего вопроса, рядом с датой: шапка
(системный промпт и схемы инструментов) одна на оба клиента и держит кэш.
"""

import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from luna_fakes import as_luna

import ai_trainer
import config
import i18n

pytestmark = pytest.mark.asyncio

# Чего человек из приложения видеть не должен: другой клиент, бот, команды.
_TELEGRAM_ONLY = re.compile(r"Telegram|(?<![\w-])бот(?:а|е|у|ом)?(?![\w-])|/feedback|/start|/mcp")


def _response(content: str):
    message = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _fake_client(responses):
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=responses)))
    )


def _header_text() -> str:
    return ai_trainer._system_prompt() + json.dumps(ai_trainer.TOOLS, ensure_ascii=False)


async def test_header_does_not_depend_on_the_client():
    """Шапка байт-в-байт одна у бота и у приложения — иначе два слота кэша."""
    bot_header = _header_text()
    token = ai_trainer.APP_CLIENT.set(True)
    try:
        app_header = _header_text()
    finally:
        ai_trainer.APP_CLIENT.reset(token)
    assert app_header == bot_header


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_header_has_no_telegram_mechanics(lang):
    with i18n.use_lang(lang):
        hits = _TELEGRAM_ONLY.findall(_header_text())
    assert not hits, f"механика Telegram в общей шапке: {hits}"


async def test_app_tail_names_app_screens_only():
    line = ai_trainer._CLIENT_LINE_APP
    assert not _TELEGRAM_ONLY.findall(line), line
    assert "Профиль → Настройки → Импорт CSV" in line
    # Бот сохраняет прежние пути.
    assert "⚙️ Настройки → 📥 Импорт CSV" in ai_trainer._CLIENT_LINE_TELEGRAM
    assert "/feedback" in ai_trainer._CLIENT_LINE_TELEGRAM


async def test_client_line_follows_app_client_flag():
    assert ai_trainer._client_line() == ai_trainer._CLIENT_LINE_TELEGRAM
    token = ai_trainer.APP_CLIENT.set(True)
    try:
        assert ai_trainer._client_line() == ai_trainer._CLIENT_LINE_APP
    finally:
        ai_trainer.APP_CLIENT.reset(token)


async def test_app_turn_request_has_no_telegram(fresh_db, user_id, monkeypatch):
    """Весь запрос модели на ходе из приложения: шапка, схемы и хвост вопроса."""
    client = _fake_client([_response("Профиль → Настройки → Импорт CSV.")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: as_luna(client))
    seen: list = []

    async def store(messages):
        seen.append(messages)

    token = ai_trainer.APP_CLIENT.set(True)
    try:
        await ai_trainer.ask(user_id, "как перенести историю из Hevy?", history=[], on_wire=store)
    finally:
        ai_trainer.APP_CLIENT.reset(token)

    request = json.dumps(client.chat.completions.create.call_args.kwargs, ensure_ascii=False)
    hits = _TELEGRAM_ONLY.findall(request)
    assert not hits, f"в запросе из приложения: {hits}"
    assert seen[0][0]["content"].endswith(ai_trainer._CLIENT_LINE_APP)


async def test_bot_turn_keeps_telegram_paths(fresh_db, user_id, monkeypatch):
    client = _fake_client([_response("ок")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: as_luna(client))
    seen: list = []

    async def store(messages):
        seen.append(messages)

    await ai_trainer.ask(user_id, "как перенести историю?", history=[], on_wire=store)
    assert seen[0][0]["content"].endswith(ai_trainer._CLIENT_LINE_TELEGRAM)


async def test_tool_notes_in_the_app_do_not_promise_telegram(fresh_db, user_id, monkeypatch):
    """Визитка «придёт в чат» и «повтори через /feedback» — про бота."""
    await fresh_db.create_routine(user_id, "Ноги")
    monkeypatch.setattr(config, "ADMIN_ID", None)
    token = ai_trainer.APP_CLIENT.set(True)
    try:
        share = await ai_trainer.execute_tool(user_id, "share_program", {"name": "Ноги"})
        feedback = await ai_trainer.execute_tool(
            user_id, "send_feedback_to_admin", {"message": "таймер не звенит", "kind": "bug"}
        )
    finally:
        ai_trainer.APP_CLIENT.reset(token)
    for payload in (share, feedback):
        assert not _TELEGRAM_ONLY.findall(payload), payload
    assert "НЕ БУДЕТ" in share
