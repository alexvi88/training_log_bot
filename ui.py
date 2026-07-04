"""Shared helper for keeping bot screens at the bottom of the chat."""

from contextlib import suppress

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, CallbackQuery, Message


async def safe_edit(callback: CallbackQuery, text: str, reply_markup=None, parse_mode=None) -> Message:
    """Replace the callback's message with a fresh one instead of editing in place.

    Telegram can't move an edited message down past newer ones, so if other
    messages landed in the chat after this one was sent, an in-place edit
    would leave a stale screen stuck above them. Deleting and resending
    keeps every menu screen at the bottom, right where the user just tapped.
    """
    with suppress(TelegramBadRequest):
        await callback.message.delete()
    return await callback.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)


async def safe_edit_photo(
    callback: CallbackQuery,
    photo: bytes,
    filename: str,
    caption: str,
    reply_markup=None,
    parse_mode=None,
) -> Message:
    """Same idea as safe_edit, but for screens whose current message is a photo.

    Deletes whatever screen the callback's button was attached to (text or
    photo) and sends the new chart as a fresh message, so repeated navigation
    doesn't leave a trail of stale photos behind.
    """
    with suppress(TelegramBadRequest):
        await callback.message.delete()
    return await callback.message.answer_photo(
        BufferedInputFile(photo, filename=filename),
        caption=caption,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )
