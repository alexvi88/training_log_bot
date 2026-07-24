"""Knows which message currently sits at the bottom of each chat.

Bot screens are kept at the bottom of the chat by deleting and re-sending them
(see ui.py and workout._refresh_live), which costs a visible flicker on every
single tap. That flicker is only the price of a real constraint: Telegram can't
move an edited message down past newer ones. While the screen still *is* the
last message in the chat, editing it in place is both cheaper and smoother —
and this module is what makes that call.

It's fed by two middlewares registered in main(): one sees every incoming
message (before filters, so even messages no handler wants are counted), the
other wraps every outgoing Bot API call, so anything the bot sends or deletes
is tracked without call sites having to remember to report it.

Everything here is in-memory and per-process. After a restart every chat is
unknown and is_at_bottom() answers False, so callers fall back to
delete-and-resend — the old behaviour. The failure mode is an extra flicker,
never a stale screen stranded in the middle of the history.
"""

from __future__ import annotations

from bisect import insort
from collections import OrderedDict

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import DeleteMessage, DeleteMessages
from aiogram.types import Message

# Per chat we keep the ids of the messages we believe still exist, so that
# deleting the bottom one reveals whatever was underneath it. Both caps are
# plain memory hygiene: forgetting old ids only ever costs an extra
# delete+resend, since an unknown message is never treated as the bottom one.
_MAX_IDS_PER_CHAT = 64
_MAX_CHATS = 1024

_live: OrderedDict[int, list[int]] = OrderedDict()


def note_message(chat_id: int, message_id: int) -> None:
    """Record that this message exists in the chat (sent by the bot or the user)."""
    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        return
    ids = _live.get(chat_id)
    if ids is None:
        ids = _live[chat_id] = []
        if len(_live) > _MAX_CHATS:
            _live.popitem(last=False)
    _live.move_to_end(chat_id)
    if message_id not in ids:
        insort(ids, message_id)
        del ids[:-_MAX_IDS_PER_CHAT]


def note_deleted(chat_id: int, message_id: int) -> None:
    """Record that this message is gone, revealing whatever sat above it."""
    ids = _live.get(chat_id)
    if ids and message_id in ids:
        ids.remove(message_id)


def is_at_bottom(chat_id: int, message_id: int) -> bool:
    """True only if this message is known to be the last one in the chat.

    False whenever we simply don't know (fresh process, forgotten id) — the
    caller then does the safe thing and re-sends.
    """
    ids = _live.get(chat_id)
    return bool(ids) and ids[-1] == message_id


def reset() -> None:
    """Drop everything — for tests."""
    _live.clear()


class TrackIncomingMessages(BaseMiddleware):
    """Counts every incoming message, including ones no handler matches.

    Registered as an *outer* middleware so it runs before filters: a sticker
    dropped into the logging screen leaves the bot's screen no longer at the
    bottom just as much as a typed set does, even though nothing handles it.
    """

    async def __call__(self, handler, event, data):
        if isinstance(event, Message):
            note_message(event.chat.id, event.message_id)
        return await handler(event, data)


class TrackOutgoingMessages(BaseRequestMiddleware):
    """Counts everything the bot sends or deletes, whichever call site did it.

    Sitting on the session means send_message, message.reply, answer_photo,
    answer_media_group, the background push jobs and every delete_message all
    report themselves for free — no flag for handlers to keep in sync.
    """

    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        if isinstance(method, DeleteMessage):
            note_deleted(method.chat_id, method.message_id)
        elif isinstance(method, DeleteMessages):
            for message_id in method.message_ids:
                note_deleted(method.chat_id, message_id)
        elif isinstance(result, Message):
            note_message(result.chat.id, result.message_id)
        elif isinstance(result, (list, tuple)):
            for item in result:
                if isinstance(item, Message):
                    note_message(item.chat.id, item.message_id)
        return result
