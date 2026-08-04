"""Стриминг ответа AI-тренера в пузырь-черновик (Bot API 9.3 sendMessageDraft).

Стриминг у бота уже был и был выпилен: он строился на редактировании сообщения
и приносил лимиты правок и мигающий сырой markdown. Черновик — нативный
механизм под это, и его отсутствие на старом сервере/клиенте не должно ничего
ломать: ответ всё равно приходит целиком одним сообщением.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

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


@pytest.fixture(autouse=True)
def _no_draft_pause(monkeypatch):
    """Пауза между черновиками не должна растягивать тесты."""
    monkeypatch.setattr(handler, "DRAFT_MIN_INTERVAL", 0)


async def _settle():
    """Дать фоновому писателю черновиков доработать до конца очереди."""
    for _ in range(20):
        await asyncio.sleep(0)


async def test_pushes_accumulated_text_into_a_draft():
    msg = _message()
    streamer = handler._DraftStreamer(msg)

    await streamer.push("Смотрю твой жим")
    await _settle()
    await streamer.push("Смотрю твой жим за месяц: три недели один вес")
    await _settle()

    calls = msg.bot.send_message_draft.await_args_list
    assert [c.kwargs["text"] for c in calls] == [
        "Смотрю твой жим",
        "Смотрю твой жим за месяц: три недели один вес",
    ]
    assert {c.kwargs["draft_id"] for c in calls} == {42}  # один черновик на ответ
    await streamer.close()


async def test_push_does_not_wait_for_telegram():
    """Главное свойство: чтение стрима модели не тормозит об отправку черновика.

    Пока Telegram отвечает (или флудвейтит), `push` обязан возвращать управление
    сразу — иначе дельты копятся в сокете, черновик замирает на полуслове, а
    потом ответ «пробегает» целиком разом."""
    msg = _message()
    release = asyncio.Event()

    async def _slow_draft(**kwargs):
        await release.wait()

    msg.bot.send_message_draft = AsyncMock(side_effect=_slow_draft)
    streamer = handler._DraftStreamer(msg)

    await asyncio.wait_for(streamer.push("первый"), timeout=1)
    await asyncio.wait_for(streamer.push("первый второй"), timeout=1)

    release.set()
    await _settle()
    # Пока отправка висела, промежуточные состояния просто схлопнулись в
    # последнее: показывать устаревший черновик смысла нет.
    assert msg.bot.send_message_draft.await_args.kwargs["text"] == "первый второй"
    await streamer.close()


async def test_flood_wait_only_pauses_the_draft(monkeypatch):
    """429 — это «подожди», а не «сервер не умеет»: раньше флудвейт насовсем
    гасил стриминг, и черновик замирал до самого ответа."""
    msg = _message()
    calls: list[str] = []

    async def _draft(**kwargs):
        calls.append(kwargs["text"])
        if len(calls) == 1:
            raise TelegramRetryAfter(method=MagicMock(), message="flood", retry_after=0)

    msg.bot.send_message_draft = AsyncMock(side_effect=_draft)
    streamer = handler._DraftStreamer(msg)

    await streamer.push("первый кусок")
    await _settle()
    await streamer.push("первый кусок и второй")
    await _settle()

    assert calls[-1] == "первый кусок и второй"
    await streamer.close()


async def test_draft_is_cleared_so_it_does_not_hang_next_to_the_answer():
    msg = _message()
    streamer = handler._DraftStreamer(msg)
    await streamer.push("текст")
    await _settle()

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
    await _settle()
    await streamer.push("второй кусок")
    await _settle()

    # Одна неудачная попытка, дальше даже не пробуем.
    assert msg.bot.send_message_draft.await_count == 1
    await streamer.close()  # и гасить нечего


async def test_only_the_tail_is_streamed_for_long_answers():
    msg = _message()
    streamer = handler._DraftStreamer(msg)

    await streamer.push("х" * (handler.MAX_DRAFT_CHARS + 500))
    await _settle()

    sent = msg.bot.send_message_draft.await_args.kwargs["text"]
    assert len(sent) == handler.MAX_DRAFT_CHARS
    await streamer.close()


# ---------- the draft window ----------


async def test_short_text_is_streamed_whole():
    assert handler._draft_tail("Смотрю твой жим") == "Смотрю твой жим"


async def test_the_tail_starts_at_a_line_break_not_mid_word():
    long_line = "х" * handler.MAX_DRAFT_CHARS
    assert handler._draft_tail(f"первая строка\n{long_line}\nхвост") == "хвост"


async def test_the_window_holds_still_while_the_text_grows_under_it():
    """The jitter came from slicing by character count: every new delta shoved
    the window left by the same amount, so the whole bubble shifted on each
    update and the client had nothing to animate but a full redraw. Anchored to
    a line, the visible text only ever gains at the bottom until a whole line
    scrolls out."""
    head = "строка одна\nстрока два\n" + "х" * handler.MAX_DRAFT_CHARS + "\n"
    first = handler._draft_tail(head + "растущий")
    second = handler._draft_tail(head + "растущий хвост")

    assert first == "растущий"
    assert second.startswith(first)


async def test_a_single_unbroken_paragraph_falls_back_to_a_word_boundary():
    text = "слово " * (handler.MAX_DRAFT_CHARS // 3)
    assert not handler._draft_tail(text).startswith("во ")


async def test_the_draft_shows_formatted_text_not_raw_markdown():
    """The bubble is a plain message, so ** used to blink through it verbatim —
    on every second line, since that is how exercise names are marked."""
    msg = _message()
    streamer = handler._DraftStreamer(msg)

    await streamer.push("Смотрю твой **pull down**")
    await _settle()

    sent = msg.bot.send_message_draft.await_args.kwargs
    assert sent["text"] == "Смотрю твой <b>pull down</b>"
    assert sent["parse_mode"] == "HTML"
    await streamer.close()


async def test_empty_chunks_are_not_sent():
    msg = _message()
    await handler._DraftStreamer(msg).push("")
    await _settle()
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
    # Планки частоты и минимальной длины не должны мешать тесту: отдаём каждый кусок.
    monkeypatch.setattr(ai_trainer, "STREAM_FLUSH_SECONDS", 0)
    monkeypatch.setattr(ai_trainer, "MIN_FIRST_FLUSH_CHARS", 0)
    seen: list[str] = []

    content, tool_calls = await ai_trainer._completion_round(
        client, [{"role": "user", "content": "?"}], user_id=1, on_chunk=_collector(seen),
    )

    assert content == "Жим встал."
    assert tool_calls == []
    assert seen[-1] == "Жим встал."


async def test_first_flush_waits_for_enough_text_even_if_flush_interval_has_passed(monkeypatch):
    """A slow-arriving first token can already be older than STREAM_FLUSH_SECONDS
    by the time it lands — time alone would flush it instantly, flashing a
    near-empty draft that then sits still for a full interval. The first flush
    also needs enough accumulated text."""
    import ai_trainer

    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=_FakeStream([_delta_event("Ж"), _delta_event("им встал." * 10)])
    )
    monkeypatch.setattr(ai_trainer, "_log_llm_cost", AsyncMock())
    # Планка частоты нулевая (флаш готов сразу по времени) — тест бьёт именно по
    # длине текста, не по интервалу.
    monkeypatch.setattr(ai_trainer, "STREAM_FLUSH_SECONDS", 0)
    seen: list[str] = []

    await ai_trainer._completion_round(
        client, [{"role": "user", "content": "?"}], user_id=1, on_chunk=_collector(seen),
    )

    # Один символ никогда не должен был уйти наружу отдельным флашем.
    assert "Ж" not in seen


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
