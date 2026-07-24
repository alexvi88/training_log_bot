"""Shared helper for keeping bot screens at the bottom of the chat."""

from contextlib import suppress

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, CallbackQuery, InputMediaPhoto, Message

import chat_bottom

_NOT_MODIFIED = "message is not modified"


async def _edit_text(message: Message, text: str, reply_markup, parse_mode) -> Message | None:
    """Edit in place; None means "couldn't — fall back to delete and resend"."""
    try:
        edited = await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        # Identical content: the screen already shows exactly what we wanted.
        return message if _NOT_MODIFIED in str(e).lower() else None
    return edited if isinstance(edited, Message) else message


async def _edit_photo(
    message: Message, photo: bytes, filename: str, caption: str, reply_markup, parse_mode
) -> Message | None:
    try:
        edited = await message.edit_media(
            InputMediaPhoto(
                media=BufferedInputFile(photo, filename=filename),
                caption=caption,
                parse_mode=parse_mode,
            ),
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as e:
        return message if _NOT_MODIFIED in str(e).lower() else None
    return edited if isinstance(edited, Message) else message


async def safe_edit(
    callback: CallbackQuery, text: str, reply_markup=None, parse_mode=None, delete: bool = True
) -> Message:
    """Show `text` as the bottom-most screen in the chat.

    Editing the callback's message in place is the cheap, flicker-free path, but
    Telegram can't move an edited message down past newer ones — so it's only
    safe while that message is still the last one in the chat (chat_bottom
    tracks that). Once anything has landed below it — a typed set, a record kept
    with its 🔥, a push — an in-place edit would leave a stale screen stranded
    above, so the message is deleted and a fresh one sent instead, putting the
    screen back under the user's thumb.

    delete=False keeps the callback's message intact — for screens like the
    AI-тренер chat, where that message is part of the user's conversation
    history, not a disposable menu screen.
    """
    message = callback.message
    if delete:
        # A photo message can't be edited into a text one, only replaced.
        if message.text is not None and chat_bottom.is_at_bottom(message.chat.id, message.message_id):
            edited = await _edit_text(message, text, reply_markup, parse_mode)
            if edited is not None:
                return edited
        with suppress(TelegramBadRequest):
            await message.delete()
    return await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)


async def safe_edit_photo(
    callback: CallbackQuery,
    photo: bytes,
    filename: str,
    caption: str,
    reply_markup=None,
    parse_mode=None,
    delete: bool = True,
) -> Message:
    """Same idea as safe_edit, but for screens whose new content is a photo.

    Swapping the media of a photo message keeps chart navigation flicker-free
    while the screen is still at the bottom; otherwise (or when the current
    screen is text, which can't become a photo) the message is deleted and the
    chart sent as a fresh one, so repeated navigation doesn't leave a trail of
    stale photos behind. delete=False preserves the callback's message — see
    safe_edit.
    """
    message = callback.message
    if delete:
        if message.photo and chat_bottom.is_at_bottom(message.chat.id, message.message_id):
            edited = await _edit_photo(message, photo, filename, caption, reply_markup, parse_mode)
            if edited is not None:
                return edited
        with suppress(TelegramBadRequest):
            await message.delete()
    return await message.answer_photo(
        BufferedInputFile(photo, filename=filename),
        caption=caption,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )
