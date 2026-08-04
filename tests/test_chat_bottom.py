"""The bottom-of-chat tracker that decides whether a screen can be edited in
place or has to be deleted and resent, plus the two middlewares that feed it."""
import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.methods import DeleteMessage, DeleteMessages, SendMessage
from aiogram.types import CallbackQuery, Chat, InaccessibleMessage, Message

import chat_bottom
import ui


@pytest.fixture(autouse=True)
def clean_tracker():
    chat_bottom.reset()
    yield
    chat_bottom.reset()


def _message(chat_id: int, message_id: int) -> Message:
    return Message(
        message_id=message_id,
        date=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        chat=Chat(id=chat_id, type="private"),
    )


def test_unknown_chat_is_never_at_bottom():
    assert chat_bottom.is_at_bottom(1, 10) is False


def test_last_noted_message_is_the_bottom_one():
    chat_bottom.note_message(1, 10)
    chat_bottom.note_message(1, 11)
    assert chat_bottom.is_at_bottom(1, 11)
    assert not chat_bottom.is_at_bottom(1, 10)


def test_deleting_the_bottom_message_reveals_the_one_above():
    chat_bottom.note_message(1, 10)
    chat_bottom.note_message(1, 11)
    chat_bottom.note_deleted(1, 11)
    assert chat_bottom.is_at_bottom(1, 10)


def test_chats_are_tracked_independently():
    chat_bottom.note_message(1, 10)
    chat_bottom.note_message(2, 99)
    assert chat_bottom.is_at_bottom(1, 10)
    assert not chat_bottom.is_at_bottom(2, 10)


def test_out_of_order_ids_still_pick_the_highest():
    chat_bottom.note_message(1, 11)
    chat_bottom.note_message(1, 10)
    assert chat_bottom.is_at_bottom(1, 11)


def test_forgetting_old_ids_never_promotes_them_to_bottom():
    for message_id in range(1, chat_bottom._MAX_IDS_PER_CHAT + 20):
        chat_bottom.note_message(1, message_id)
    assert chat_bottom.is_at_bottom(1, chat_bottom._MAX_IDS_PER_CHAT + 19)
    assert not chat_bottom.is_at_bottom(1, 1)


def test_noting_the_same_message_twice_is_a_no_op():
    chat_bottom.note_message(1, 10)
    chat_bottom.note_message(1, 11)
    chat_bottom.note_message(1, 10)
    assert chat_bottom.is_at_bottom(1, 11)


@pytest.mark.asyncio
async def test_incoming_middleware_counts_the_message_before_the_handler_runs():
    """A typed set has to already count as "below the screen" by the time the
    handler decides how to redraw it."""
    seen = []

    async def handler(event, data):
        seen.append(chat_bottom.is_at_bottom(1, 10))

    chat_bottom.note_message(1, 10)
    await chat_bottom.TrackIncomingMessages()(handler, _message(1, 11), {})
    assert seen == [False]


@pytest.mark.asyncio
async def test_outgoing_middleware_counts_sent_messages():
    async def make_request(bot, method):
        return _message(1, 11)

    chat_bottom.note_message(1, 10)
    await chat_bottom.TrackOutgoingMessages()(make_request, None, SendMessage(chat_id=1, text="x"))
    assert chat_bottom.is_at_bottom(1, 11)


@pytest.mark.asyncio
async def test_outgoing_middleware_counts_every_message_of_a_media_group():
    async def make_request(bot, method):
        return [_message(1, 11), _message(1, 12)]

    chat_bottom.note_message(1, 10)
    await chat_bottom.TrackOutgoingMessages()(make_request, None, SendMessage(chat_id=1, text="x"))
    assert chat_bottom.is_at_bottom(1, 12)


@pytest.mark.asyncio
async def test_outgoing_middleware_counts_deletions():
    async def make_request(bot, method):
        return True

    chat_bottom.note_message(1, 10)
    chat_bottom.note_message(1, 11)
    await chat_bottom.TrackOutgoingMessages()(make_request, None, DeleteMessage(chat_id=1, message_id=11))
    assert chat_bottom.is_at_bottom(1, 10)


@pytest.mark.asyncio
async def test_outgoing_middleware_counts_bulk_deletions():
    async def make_request(bot, method):
        return True

    for message_id in (10, 11, 12):
        chat_bottom.note_message(1, message_id)
    await chat_bottom.TrackOutgoingMessages()(
        make_request, None, DeleteMessages(chat_id=1, message_ids=[11, 12])
    )
    assert chat_bottom.is_at_bottom(1, 10)


@pytest.mark.asyncio
async def test_failed_send_leaves_the_tracker_alone():
    async def make_request(bot, method):
        raise RuntimeError("network")

    chat_bottom.note_message(1, 10)
    with pytest.raises(RuntimeError):
        await chat_bottom.TrackOutgoingMessages()(make_request, None, SendMessage(chat_id=1, text="x"))
    assert chat_bottom.is_at_bottom(1, 10)


# ---------- сообщения, которых уже нет ----------


async def test_safe_edit_survives_an_inaccessible_message():
    """Кнопки живут в истории чата вечно, и для сообщения старше суток (или
    удалённого) Telegram присылает `InaccessibleMessage` — это не `Message`: ни
    `text`, ни `answer`, ни `delete` у него нет.

    Читать их — `AttributeError` вместо экрана, то есть «⚠️ Что-то пошло не так»
    ровно в том случае, когда кнопку и нажимают. Править там нечего, поэтому
    экран уходит новым сообщением в тот же чат.
    """
    inaccessible = InaccessibleMessage(chat=Chat(id=77, type="private"), message_id=5)
    callback = MagicMock(spec=CallbackQuery)
    callback.message = inaccessible
    callback.bot = MagicMock()
    callback.bot.send_message = AsyncMock(return_value="sent")

    result = await ui.safe_edit(callback, "экран")

    assert result == "sent"
    assert callback.bot.send_message.await_args.args[0] == 77


async def test_safe_edit_photo_survives_an_inaccessible_message():
    inaccessible = InaccessibleMessage(chat=Chat(id=77, type="private"), message_id=5)
    callback = MagicMock(spec=CallbackQuery)
    callback.message = inaccessible
    callback.bot = MagicMock()
    callback.bot.send_photo = AsyncMock(return_value="sent")

    result = await ui.safe_edit_photo(callback, b"\x89PNG", "chart.png", "подпись")

    assert result == "sent"
    assert callback.bot.send_photo.await_args.args[0] == 77
