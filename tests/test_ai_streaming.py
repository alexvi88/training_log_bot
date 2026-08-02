"""Стриминг ответа AI-тренера в пузырь-черновик (Bot API 9.3 sendMessageDraft).

Стриминг у бота уже был и был выпилен: он строился на редактировании сообщения
и приносил лимиты правок и мигающий сырой markdown. Черновик — нативный
механизм под это, и его отсутствие на старом сервере/клиенте не должно ничего
ломать: ответ всё равно приходит целиком одним сообщением.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from handlers import ai_trainer as handler

pytestmark = pytest.mark.asyncio


def _message(user_id: int = 111):
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=user_id)
    msg.message_id = 42
    bot = MagicMock()
    bot.send_message_draft = AsyncMock(return_value=True)
    msg.bot = bot
    return msg


async def test_pushes_accumulated_text_into_a_draft():
    msg = _message()
    streamer = handler._DraftStreamer(msg)

    await streamer.push("Смотрю твой жим")
    await streamer.push("Смотрю твой жим за месяц: три недели один вес")

    calls = msg.bot.send_message_draft.await_args_list
    assert [c.kwargs["text"] for c in calls] == [
        "Смотрю твой жим",
        "Смотрю твой жим за месяц: три недели один вес",
    ]
    assert {c.kwargs["draft_id"] for c in calls} == {42}  # один черновик на ответ


async def test_draft_is_cleared_so_it_does_not_hang_next_to_the_answer():
    msg = _message()
    streamer = handler._DraftStreamer(msg)
    await streamer.push("текст")

    await streamer.close()

    assert msg.bot.send_message_draft.await_args.kwargs["text"] == ""


async def test_nothing_is_cleared_when_streaming_never_started():
    msg = _message()
    await handler._DraftStreamer(msg).close()
    msg.bot.send_message_draft.assert_not_awaited()


async def test_an_unsupported_server_silently_disables_streaming():
    """Метод из Bot API 9.3: на старом сервере он просто не существует, и это не
    повод ронять ответ — он всё равно придёт обычным сообщением."""
    msg = _message()
    msg.bot.send_message_draft = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="method not found")
    )
    streamer = handler._DraftStreamer(msg)

    await streamer.push("первый кусок")
    await streamer.push("второй кусок")

    # Одна неудачная попытка, дальше даже не пробуем.
    assert msg.bot.send_message_draft.await_count == 1
    await streamer.close()  # и гасить нечего


async def test_only_the_tail_is_streamed_for_long_answers():
    msg = _message()
    streamer = handler._DraftStreamer(msg)

    await streamer.push("х" * (handler.MAX_DRAFT_CHARS + 500))

    sent = msg.bot.send_message_draft.await_args.kwargs["text"]
    assert len(sent) == handler.MAX_DRAFT_CHARS


async def test_empty_chunks_are_not_sent():
    msg = _message()
    await handler._DraftStreamer(msg).push("")
    msg.bot.send_message_draft.assert_not_awaited()


# ---------- сборка стрима на стороне ai_trainer ----------


class _FakeStream:
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e
        return gen()


def _delta_event(content=None, tool_calls=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=usage)


async def test_streamed_round_returns_the_whole_text(monkeypatch):
    import ai_trainer

    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=_FakeStream([_delta_event("Жим "), _delta_event("встал.")])
    )
    monkeypatch.setattr(ai_trainer, "_log_llm_cost", AsyncMock())
    # Планка частоты не должна мешать тесту: отдаём каждый кусок.
    monkeypatch.setattr(ai_trainer, "STREAM_FLUSH_SECONDS", 0)
    seen: list[str] = []

    content, tool_calls = await ai_trainer._completion_round(
        client, [{"role": "user", "content": "?"}], user_id=1, on_chunk=_collector(seen),
    )

    assert content == "Жим встал."
    assert tool_calls == []
    assert seen[-1] == "Жим встал."


def _collector(sink: list[str]):
    async def _push(text: str) -> None:
        sink.append(text)
    return _push


async def test_streamed_tool_calls_are_reassembled_from_deltas(monkeypatch):
    """Tool-call приходит по кускам: id и аргументы склеиваются, иначе раунд с
    инструментами при стриминге просто не сработает."""
    import ai_trainer

    part1 = SimpleNamespace(index=0, id="call_", function=SimpleNamespace(name="get_", arguments='{"da'))
    part2 = SimpleNamespace(index=0, id="1", function=SimpleNamespace(name="food_diary", arguments='ys": 7}'))
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=_FakeStream([_delta_event(tool_calls=[part1]), _delta_event(tool_calls=[part2])])
    )
    monkeypatch.setattr(ai_trainer, "_log_llm_cost", AsyncMock())

    _content, tool_calls = await ai_trainer._completion_round(
        client, [{"role": "user", "content": "?"}], user_id=1, on_chunk=_collector([]),
    )

    assert len(tool_calls) == 1
    assert tool_calls[0].id == "call_1"
    assert tool_calls[0].function.name == "get_food_diary"
    assert tool_calls[0].function.arguments == '{"days": 7}'


async def test_without_a_chunk_callback_the_call_is_not_streamed(monkeypatch):
    """Стриминг — опция: без колбэка запрос идёт обычным, не-стримовым путём."""
    import ai_trainer

    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ответ", tool_calls=None))],
            usage=None,
        )
    )
    monkeypatch.setattr(ai_trainer, "_log_llm_cost", AsyncMock())

    content, _ = await ai_trainer._completion_round(client, [], user_id=1)

    assert content == "ответ"
    assert "stream" not in client.chat.completions.create.await_args.kwargs


async def test_flush_cadence_is_configurable():
    """Шаг стриминга подбирается на живых ответах, поэтому живёт в env, а не в
    коде: мельче — «печать» живее, но каждый флаш это запрос sendMessageDraft,
    и упёршись в лимиты Telegram стример молча выключится до конца ответа."""
    import ai_trainer
    import config

    assert ai_trainer.STREAM_FLUSH_SECONDS == config.AI_STREAM_FLUSH_SECONDS
    assert 0 < config.AI_STREAM_FLUSH_SECONDS <= 2
