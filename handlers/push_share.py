"""«📤 Поделиться» под пушем о пропуске — пуш картинкой-сторис (push_card.py).

Текст берётся из самого сообщения пуша (подпись под фото): пуш уже на языке
атлета, и в карточку уходит ровно то, что он прочитал. Картинка приходит
отдельным фото — его пересылают или сохраняют в сторис, — под ней строка и
кнопка шаринга с реферальной ссылкой `?start=ref_<id>`.

Без состояний: кнопка живёт под пушем, который приходит посреди чего угодно,
поэтому роутер в main.py стоит рядом с /community и /year.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import acquisition
import i18n
import keyboards
import product_metrics
import push_card
from handlers import sharing

logger = logging.getLogger(__name__)

router = Router(name="push_share")


@router.callback_query(F.data == keyboards.PUSH_CARD_CALLBACK)
async def cb_share_push(callback: CallbackQuery):
    await callback.answer()
    message = callback.message
    text = (getattr(message, "caption", None) or getattr(message, "text", None) or "").strip()
    if not text:
        await message.answer(i18n.t("push_card.gone"))
        return
    try:
        png = await push_card.build(text, lang=i18n.get_lang())
        await message.answer_photo(BufferedInputFile(png, filename="push.png"))
        link = acquisition.referral_link(await sharing.get_bot_username(callback.bot), callback.from_user.id)
        share_url = (
            f"https://t.me/share/url?url={quote(link, safe='')}"
            f"&text={quote(i18n.t('push_card.share_text'), safe='')}"
        )
        await message.answer(
            i18n.t("push_card.bot.text"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=i18n.t("push_card.btn"), url=share_url)],
            ]),
        )
    except Exception:
        logger.exception("push card: user %s", callback.from_user.id)
        return
    await product_metrics.track(callback.from_user.id, "push_card_share", {"source": "tg"}, platform="tg")
