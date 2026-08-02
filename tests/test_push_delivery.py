"""Push delivery: every push goes out as the coach photo with the text as its caption."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import engagement
import push_texts

pytestmark = pytest.mark.asyncio

_TODAY = dt.date(2026, 5, 4)


def _bot(file_id: str = "AgAD_new_upload"):
    bot = MagicMock()
    bot.send_photo = AsyncMock(
        return_value=SimpleNamespace(photo=[SimpleNamespace(file_id=file_id)])
    )
    return bot


@pytest.fixture(autouse=True)
def reset_cached_file_id(monkeypatch):
    monkeypatch.setattr(engagement, "_push_image_file_id", None)
    yield
    monkeypatch.setattr(engagement, "_push_image_file_id", None)


async def test_push_is_sent_as_a_photo_with_the_text_as_caption(fresh_db, user_id):
    bot = _bot()
    decision = engagement.PushDecision(push_texts.SKIP_3, "ПРИВЕТ АТЛЕТ, третий день без зала.")

    await engagement._deliver(bot, user_id, decision, _TODAY)

    bot.send_photo.assert_awaited_once()
    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs["chat_id"] == user_id
    assert kwargs["caption"] == decision.text
    assert kwargs["reply_markup"] is not None
    assert await fresh_db.has_push_today(user_id, _TODAY.isoformat())


async def test_push_upload_is_cached_and_reused_by_file_id(fresh_db, user_id):
    bot = _bot(file_id="AgAD_cached")
    decision = engagement.PushDecision(push_texts.SKIP_3, "ПРИВЕТ АТЛЕТ, третий день без зала.")

    await engagement._deliver(bot, user_id, decision, _TODAY)
    first_photo = bot.send_photo.await_args.kwargs["photo"]

    await engagement._deliver(bot, user_id, decision, _TODAY)
    second_photo = bot.send_photo.await_args.kwargs["photo"]

    assert second_photo == "AgAD_cached"
    assert first_photo != second_photo


async def test_push_without_cta_omits_the_keyboard(fresh_db, user_id):
    bot = _bot()
    decision = engagement.PushDecision(push_texts.WEEKLY_DIGEST, "текст", with_cta=False)

    await engagement._deliver(bot, user_id, decision, _TODAY)

    assert bot.send_photo.await_args.kwargs["reply_markup"] is None


async def test_a_caption_over_telegrams_limit_is_truncated(fresh_db, user_id):
    """The AI weekly digest is free-form model output and can run long."""
    bot = _bot()
    long_text = "ПРИВЕТ АТЛЕТ, " + "а" * 2000
    decision = engagement.PushDecision(push_texts.AI_WEEKLY, long_text)

    await engagement._deliver(bot, user_id, decision, _TODAY)

    caption = bot.send_photo.await_args.kwargs["caption"]
    assert len(caption) == engagement.CAPTION_LIMIT
    assert caption.endswith("…")
