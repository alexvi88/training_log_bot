"""Кнопка «Разбери видео подхода» в быстром наборе.

Разбор видео — единственная возможность бота, о которой нельзя догадаться: в чате
нигде не написано «пришли ролик». Поэтому кнопка есть всегда, независимо от того,
сколько видео уже разобрано сегодня, — дневную квоту сдерживает не кнопка, а
ai_limits.KIND_VIDEO у самого разбора.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import db
from handlers import ai_trainer as handler

pytestmark = pytest.mark.asyncio

VIDEO_LABEL = "🎥 Разбери видео подхода"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(config, "NOVITA_API_KEY", "test-key")


async def test_button_shown_before_first_video_today(fresh_db, user_id):
    labels = [label for label, _cb in await handler.intro_presets(user_id)]
    assert VIDEO_LABEL in labels


async def test_button_still_shown_after_videos_today(monkeypatch, fresh_db, user_id):
    """Регрессия: кнопка раньше пряталась после первого разбора — человек,
    вернувшийся во второй раз за день, терял единственную подсказку про фичу,
    хотя сам разбор всё ещё ограничен только дневной квотой, а не кнопкой."""
    monkeypatch.setattr(db, "get_ai_video_count_today", AsyncMock(return_value=1))
    await db.create_finished_workout(
        user_id, started_at="2026-07-13T10:00:00", finished_at="2026-07-13T11:00:00"
    )
    labels = [label for label, _cb in await handler.intro_presets(user_id)]
    assert VIDEO_LABEL in labels
    # Остальные готовые вопросы никуда не делись.
    assert any("Как мой прогресс" in label for label in labels)


async def test_button_hidden_when_analysis_not_configured(monkeypatch, fresh_db, user_id):
    """Обещать кнопкой то, что ответит «пока не подключил», — худшая реклама."""
    monkeypatch.setattr(config, "NOVITA_API_KEY", "")
    labels = [label for label, _cb in await handler.intro_presets(user_id)]
    assert VIDEO_LABEL not in labels


async def test_hint_explains_the_angle_before_shooting(fresh_db, user_id):
    """«Сними сбоку» на разборе человек читает, уже потратив попытку."""
    assert "сбоку" in handler.VIDEO_HINT_TEXT
    assert str(config.MAX_VIDEO_SECONDS) in handler.VIDEO_HINT_TEXT


async def test_hint_costs_nothing(monkeypatch, fresh_db, user_id):
    """Подсказка — не вопрос: ни вызова модели, ни списанной квоты."""
    ask = AsyncMock()
    monkeypatch.setattr(handler.ai_trainer, "ask", ask)
    spend_video = AsyncMock()
    spend_question = AsyncMock()
    monkeypatch.setattr(db, "increment_ai_video_count", spend_video)
    monkeypatch.setattr(db, "increment_ai_question_count", spend_question)

    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, language_code=None)
    callback.data = "ai:videohint"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer = AsyncMock()
    state = MagicMock()
    state.set_state = AsyncMock()

    await handler.ai_video_hint(callback, state)

    ask.assert_not_awaited()
    spend_video.assert_not_awaited()
    spend_question.assert_not_awaited()
    # И человек получил инструкцию, а не пустой экран.
    assert "сбоку" in callback.message.answer.await_args.args[0]


async def test_hint_keeps_user_in_the_chat_state(fresh_db, user_id):
    """После подсказки ролик должен попасть в хендлер видео — значит состояние чата."""
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, language_code=None)
    callback.data = "ai:videohint"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer = AsyncMock()
    state = MagicMock()
    state.set_state = AsyncMock()

    await handler.ai_video_hint(callback, state)

    state.set_state.assert_awaited_once()
    assert state.set_state.await_args.args[0] == handler.AITrainerFlow.chatting


async def test_hint_refuses_when_not_configured(monkeypatch, fresh_db, user_id):
    monkeypatch.setattr(config, "NOVITA_API_KEY", "")
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, language_code=None)
    callback.data = "ai:videohint"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer = AsyncMock()

    await handler.ai_video_hint(callback, MagicMock())

    callback.message.answer.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get("show_alert") is True
