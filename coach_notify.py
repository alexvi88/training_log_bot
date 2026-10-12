"""Комментарий тренера доезжает до атлета: банер на iPhone и сообщение в Telegram.

Сервис для тренеров (coach_service.py) пишет комментарий из двух мест — REST
`/v1/coach/.../comments` (приложение) и экран тренировки клиента в боте
(handlers/coach.py). Оба зовут `deliver_comment` после записи, и атлет получает
его одинаково, откуда бы тренер ни писал:

- APNs — на каждое iOS-устройство атлета (банер «Комментарий тренера»), как
  было у REST с первого дня;
- Telegram — сообщение с текстом и кнопкой «Открыть тренировку», если у атлета
  есть Telegram (`telegram_linked`, id больше нуля: у аккаунта только из
  приложения id синтетический, писать туда некуда).

Два канала — как у остальных пушей продукта: банер на телефоне и сообщение в
боте — это разные места, и человек, который сидит только в одном из них, не
должен терять комментарий. Тексты — на языке атлета (`users.lang`), а не того,
кто пишет: фоновой отправке язык из апдейта не достаётся (CLAUDE.md, «Фоновая
задача, отправляющая текст без апдейта»).

Ни один шаг не бросает: комментарий к этому моменту уже записан, и сбой
доставки не должен превращаться в ошибку у тренера.
"""

from __future__ import annotations

import datetime as dt
import logging
from html import escape
from typing import Any, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import apns
import chat_bottom
import config
import db
import formatting
import i18n
import push_ios
import timeutil

logger = logging.getLogger(__name__)

# Сколько символов комментария влезает в тело банера после имени тренера.
PUSH_TEXT_CLIP = 60


def _lang_of(client: Any) -> str:
    lang = client["lang"]
    return lang if lang in i18n.SUPPORTED else i18n.DEFAULT_LANG


def has_telegram(client: Any) -> bool:
    """Есть ли куда писать в Telegram: аккаунт из приложения без Telegram
    живёт под синтетическим отрицательным id (db.create_app_only_user)."""
    return bool(client["telegram_linked"]) and client["telegram_id"] > 0


async def push_ios_comment(client: Any, coach_name: str, text: str, workout_id: int) -> int:
    """Банер атлету «Комментарий тренера» на его языке, на каждое устройство.
    Возвращает, сколько устройств приняли банер."""
    if not apns.is_configured():
        return 0
    client_id = client["telegram_id"]
    try:
        tokens = await db.get_push_tokens(client_id, "ios")
        if not tokens:
            return 0
        lang = _lang_of(client)
        title = i18n.t_in(lang, "coach.push.comment_title")
        body = i18n.t_in(
            lang, "coach.push.comment_body",
            coach=push_ios._clip_param(coach_name, 20),
            text=push_ios._clip_param(text, PUSH_TEXT_CLIP),
        )
        accepted = 0
        for token in tokens:
            if await apns.send_alert(
                client_id, token, title, body, category=push_ios.COACH_COMMENT,
                route=push_ios.workout_detail_route(workout_id),
            ):
                accepted += 1
        return accepted
    except Exception:
        logger.exception("coach: comment push failed for user %s", client_id)
        return 0


async def telegram_comment_message(
    client: Any, coach_name: str, text: str, workout_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и кнопка сообщения атлету — на ЕГО языке, какой бы ни был у
    того, кто вызвал (тренер мог писать по-английски)."""
    with i18n.use_lang(_lang_of(client)):
        workout = await db.get_workout(workout_id)
        when = ""
        if workout is not None:
            started = dt.datetime.fromisoformat(workout["started_at"])
            local = timeutil.to_user_local(started, client)
            when = formatting.format_day_month_ru(local.date(), timeutil.user_today(client))
        body = i18n.t(
            "coach.tg.comment",
            coach=escape(coach_name or i18n.t("coach.unnamed")),
            date=escape(when),
            text=escape(text),
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=i18n.t("coach.btn.open_workout"), callback_data=f"hist:item:{workout_id}")
        ]])
    return body, kb


async def send_telegram_comment(bot, client: Any, coach_name: str, text: str, workout_id: int) -> bool:
    """Сообщение атлету в Telegram. False — писать некуда или не дошло
    (атлет остановил бота, сеть) — комментарий всё равно лежит под тренировкой."""
    if bot is None or not has_telegram(client):
        return False
    chat_id = client["telegram_id"]
    try:
        body, kb = await telegram_comment_message(client, coach_name, text, workout_id)
        sent = await bot.send_message(chat_id, body, parse_mode="HTML", reply_markup=kb)
    except Exception:
        logger.exception("coach: telegram comment failed for user %s", chat_id)
        return False
    # Сообщение легло под экран атлета: следующий тап по старому экрану должен
    # перерисовать его ниже, а не править на месте над комментарием. Бот из
    # main() отмечает это сам (chat_bottom.TrackOutgoing), короткоживущий бот
    # REST-слоя — нет; повторная отметка ничего не ломает.
    message_id = getattr(sent, "message_id", None)
    if isinstance(message_id, int):
        chat_bottom.note_message(chat_id, message_id)
    return True


async def _send_with_own_bot(client: Any, coach_name: str, text: str, workout_id: int) -> bool:
    """REST-слою своего Bot не досталось (маршруты /v1 собираются раньше, чем
    main() создаёт бота) — короткоживущий на одну отправку, как у
    api_v1_feedback._send_feedback_to_admin."""
    if not config.BOT_TOKEN or not has_telegram(client):
        return False
    from aiogram import Bot

    bot = Bot(token=config.BOT_TOKEN)
    try:
        return await send_telegram_comment(bot, client, coach_name, text, workout_id)
    finally:
        await bot.session.close()


async def deliver_comment(
    client: Any, coach_name: str, text: str, workout_id: int, *, bot: Optional[Any] = None
) -> tuple[int, bool]:
    """Оба канала разом: (сколько iOS-устройств приняли банер, ушло ли
    сообщение в Telegram). `bot` — живой бот хендлера; без него (REST) —
    короткоживущий."""
    pushed = await push_ios_comment(client, coach_name, text, workout_id)
    if bot is not None:
        telegram = await send_telegram_comment(bot, client, coach_name, text, workout_id)
    else:
        try:
            telegram = await _send_with_own_bot(client, coach_name, text, workout_id)
        except Exception:
            logger.exception("coach: telegram comment (rest) failed for user %s", client["telegram_id"])
            telegram = False
    return pushed, telegram
