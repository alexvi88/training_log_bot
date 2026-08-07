"""handlers.ai_trainer: приём видео подхода — лимиты и порядок проверок.

Порядок тут не косметика: длину Telegram сообщает в самом апдейте, а скачивание
и разбор стоят и трафика, и денег. Поэтому дорогое не должно случаться после
отказа — на это и смотрят тесты.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import db
from handlers import ai_trainer as handler

pytestmark = pytest.mark.asyncio


def _message(duration=10, file_size=1024, caption=None, user_id=777):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.caption = caption
    message.video = SimpleNamespace(
        duration=duration, file_size=file_size, mime_type="video/mp4"
    )
    message.video_note = None
    message.animation = None
    message.reply = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(delete=AsyncMock()))
    message.bot = MagicMock()
    message.bot.download = AsyncMock(return_value=SimpleNamespace(read=lambda: b"bytes"))
    return message


@pytest.fixture(autouse=True)
def _clear_busy():
    handler._busy.clear()
    yield
    handler._busy.clear()


@pytest.fixture
def wired(monkeypatch):
    """Всё подключено, квота свободна, разбор возвращает пустой, но валидный ответ."""
    monkeypatch.setattr(config, "NOVITA_API_KEY", "test-key")
    monkeypatch.setattr(db, "get_ai_video_count_today", AsyncMock(return_value=0))
    monkeypatch.setattr(db, "increment_ai_video_count", AsyncMock())
    analyze = AsyncMock(return_value={
        "exercise": "присед", "reps_seen": 3,
        "view": {"angle": "сбоку", "usable": True, "problem": ""},
        "description": [], "observations": [], "not_visible": [], "camera_advice": "",
    })
    monkeypatch.setattr(handler.video_analysis, "analyze", analyze)
    handle = AsyncMock()
    monkeypatch.setattr(handler, "_handle_question", handle)
    return SimpleNamespace(analyze=analyze, handle=handle)


async def test_video_analyzed_and_handed_to_trainer(wired):
    message = _message()
    await handler.ai_video_question(message, MagicMock())

    wired.analyze.assert_awaited_once()
    # Тренер получает наблюдения текстом, а не само видео.
    kwargs = wired.handle.await_args.kwargs
    assert "присед" in kwargs["video_context"]
    assert kwargs["history_question"] == "[прислал видео подхода]"
    db.increment_ai_video_count.assert_awaited_once()


async def test_caption_becomes_the_question(wired):
    message = _message(caption="почему поясницу тянет")
    await handler.ai_video_question(message, MagicMock())

    args = wired.handle.await_args.args
    assert args[2] == "почему поясницу тянет"
    assert wired.handle.await_args.kwargs["history_question"] == "[видео] почему поясницу тянет"


async def test_too_long_video_rejected_before_download(wired):
    message = _message(duration=config.MAX_VIDEO_SECONDS + 1)
    await handler.ai_video_question(message, MagicMock())

    message.bot.download.assert_not_awaited()
    wired.analyze.assert_not_awaited()
    assert str(config.MAX_VIDEO_SECONDS) in message.reply.await_args.args[0]


async def test_daily_limit_blocks_before_download(monkeypatch, wired):
    monkeypatch.setattr(
        db, "get_ai_video_count_today", AsyncMock(return_value=config.AI_VIDEO_DAILY_LIMIT)
    )
    monkeypatch.setattr(handler, "ai_keyboard", AsyncMock(return_value=None))
    message = _message()
    await handler.ai_video_question(message, MagicMock())

    message.bot.download.assert_not_awaited()
    wired.analyze.assert_not_awaited()
    assert str(config.AI_VIDEO_DAILY_LIMIT) in message.reply.await_args.args[0]


async def test_oversized_file_rejected_before_download(wired):
    message = _message(file_size=config.MAX_VIDEO_BYTES + 1)
    await handler.ai_video_question(message, MagicMock())

    message.bot.download.assert_not_awaited()
    wired.analyze.assert_not_awaited()


async def test_missing_key_tells_user_to_type(monkeypatch, wired):
    monkeypatch.setattr(config, "NOVITA_API_KEY", "")
    message = _message()
    await handler.ai_video_question(message, MagicMock())

    wired.analyze.assert_not_awaited()
    assert "текстом" in message.reply.await_args.args[0]


async def test_failed_analysis_does_not_spend_quota(monkeypatch, wired):
    """Поломка провайдера не должна стоить человеку одного из десяти разборов."""
    monkeypatch.setattr(handler.video_analysis, "analyze", AsyncMock(return_value=None))
    message = _message()
    await handler.ai_video_question(message, MagicMock())

    db.increment_ai_video_count.assert_not_awaited()
    wired.handle.assert_not_awaited()
    assert "Не смог разобрать" in message.reply.await_args.args[0]


async def test_second_video_while_busy_is_refused(wired):
    handler._busy.add(777)
    message = _message()
    await handler.ai_video_question(message, MagicMock())

    message.bot.download.assert_not_awaited()
    wired.analyze.assert_not_awaited()


async def test_busy_released_after_run(wired):
    message = _message()
    await handler.ai_video_question(message, MagicMock())
    assert 777 not in handler._busy


async def test_video_note_accepted(wired):
    """Кружок — тот же путь: у него есть и duration, и file_size."""
    message = _message()
    message.video = None
    message.video_note = SimpleNamespace(duration=8, file_size=2048)
    await handler.ai_video_question(message, MagicMock())

    wired.analyze.assert_awaited_once()
    # У кружка своего mime_type нет — подставляем mp4, а не падаем на getattr.
    assert wired.analyze.await_args.kwargs["mime_type"] == "video/mp4"


async def test_animation_accepted(wired):
    """Ролик без аудиодорожки Telegram присылает как animation (в клиенте «GIF»).

    Именно на этом фича молча не работала: фильтр ловил только video, и снятое
    молча видео из зала улетало в общий fallback «Не понял».
    """
    message = _message()
    message.video = None
    message.animation = SimpleNamespace(duration=12, file_size=3072, mime_type="video/mp4")
    await handler.ai_video_question(message, MagicMock())

    wired.analyze.assert_awaited_once()


async def test_animation_mime_type_passed_through(wired):
    """Настоящий .gif приходит как image/gif — врать про тип в data: URL нельзя."""
    message = _message()
    message.video = None
    message.animation = SimpleNamespace(duration=5, file_size=1024, mime_type="image/gif")
    await handler.ai_video_question(message, MagicMock())

    assert wired.analyze.await_args.kwargs["mime_type"] == "image/gif"


async def test_animation_length_limit_applies(wired):
    """Лимит длины не должен обходиться через тип: GIF считается так же."""
    message = _message()
    message.video = None
    message.animation = SimpleNamespace(
        duration=config.MAX_VIDEO_SECONDS + 5, file_size=1024, mime_type="video/mp4"
    )
    await handler.ai_video_question(message, MagicMock())

    message.bot.download.assert_not_awaited()
    wired.analyze.assert_not_awaited()
