"""/year — «Итоги года» в боте: альбом сторис итога года и кнопка
«Поделиться» с реферальной ссылкой.

Числа и слайды — year_review.py (тот же итог, что `GET /v1/year-review` у
приложения). Альбом приходит одним сообщением, под ним — строка тренера и
кнопка шаринга: ссылка — реферальная `?start=ref_<id>` (её считает воронка
`referral_signups_tg`), а на самих слайдах — плашка App Store.

Без состояний, поэтому роутер стоит в main.py рядом с /community: команда
нужна и посреди тренировки. Тот же показ зовут кнопка после импорта
(handlers/csv_import.py, `year:show`) и рассылка 1 декабря.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

import acquisition
import db
import i18n
import product_metrics
import timeutil
import year_review
from handlers import sharing

logger = logging.getLogger(__name__)

router = Router(name="year_review")

SHOW_CALLBACK = "year:show"


def open_keyboard() -> InlineKeyboardMarkup:
    """Кнопка «🏆 Мои итоги года» — под итогом импорта и в рассылке."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t("year.btn.open"), callback_data=SHOW_CALLBACK)],
    ])


async def send_year(
    bot: Bot, chat_id: int, user_id: int, *, source: str, quiet_if_empty: bool = False
) -> int | None:
    """Альбом года и строка с «Поделиться»; вернёт id сообщения со строкой
    (по нему рассылка пишет `pushes.tg_message_id`). None — показывать нечего
    (за год нет законченных тренировок): атлет получает приглашение начать,
    а с `quiet_if_empty` — ничего."""
    user = await db.get_user(user_id)
    if user is None:
        return None
    deck, pngs = await year_review.build(user_id, user=user)
    year = timeutil.user_today(user).year
    if deck is None or not pngs:
        # После импорта молчим: привезли только прошлые годы — звать
        # «запиши первую» под итогом переноса было бы странно.
        if not quiet_if_empty:
            await bot.send_message(chat_id, i18n.t("year.bot.empty", year=year))
        return None
    files = [
        BufferedInputFile(png, filename=f"year_{year}_{index + 1}.png") for index, png in enumerate(pngs)
    ]
    if len(files) == 1:
        await bot.send_photo(chat_id=chat_id, photo=files[0])
    else:
        await bot.send_media_group(chat_id=chat_id, media=[InputMediaPhoto(media=f) for f in files])
    facts = await year_review.collect(user_id, year, user)
    link = acquisition.referral_link(await sharing.get_bot_username(bot), user_id)
    share_text = i18n.t("year.share.text", year=year, workouts=facts.workouts if facts else 0)
    share_url = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(share_text, safe='')}"
    message = await bot.send_message(
        chat_id, i18n.t("year.bot.intro", year=year), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=i18n.t("year.btn.share"), url=share_url)],
        ]),
    )
    await product_metrics.track(user_id, "year_review_open", {"source": source}, platform="tg")
    return getattr(message, "message_id", None)


@router.message(Command("year"))
async def cmd_year(message: Message):
    await db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.language_code
    )
    try:
        await send_year(message.bot, message.chat.id, message.from_user.id, source="command")
    except Exception:
        logger.exception("year review: user %s", message.from_user.id)
        await message.answer(i18n.t("year.bot.failed"))


@router.callback_query(F.data == SHOW_CALLBACK)
async def cb_year(callback: CallbackQuery):
    await callback.answer()
    try:
        await send_year(callback.bot, callback.message.chat.id, callback.from_user.id, source="button")
    except Exception:
        logger.exception("year review: user %s", callback.from_user.id)
        await callback.message.answer(i18n.t("year.bot.failed"))
