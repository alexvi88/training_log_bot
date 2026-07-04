from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from handlers import fallback

pytestmark = pytest.mark.asyncio


async def test_unhandled_text_gets_a_pointer_back_to_start():
    message = MagicMock()
    message.from_user = SimpleNamespace(id=1)
    message.text = "какая-то ерунда"
    message.reply = AsyncMock()

    await fallback.unhandled_text(message)

    message.reply.assert_awaited_once()
    assert "/start" in message.reply.await_args.args[0]
