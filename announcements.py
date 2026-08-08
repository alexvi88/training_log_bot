"""Разовые релизные рассылки: одно сообщение про новую фичу, один раз на человека.

Зачем отдельный механизм, когда есть `/broadcast` и ежедневные пуши:

* `/broadcast` — ручной: админ должен сидеть в чате с ботом и нажать кнопку.
  Релиз выкатывается мержем, и рассылка должна уйти сама, без человека у
  клавиатуры.
* Ежедневный пуш (`engagement.py`) — это реакция на сигнал в дневнике
  (пропуск, серия, звание). Релиз ни на какой сигнал не опирается: он про
  продукт, а не про атлета, и приходит один раз всем.

Как это работает. Каждая рассылка — запись в `ANNOUNCEMENTS` со своим ключом.
Ключ едет в `pushes.category`, и по нему же считается, кому ещё не уходило
(`db.list_announcement_recipients`). Поэтому перезапуск контейнера,
повторный деплой и докатка не рассылают ничего второй раз: отметка о доставке
лежит в базе, а не в памяти процесса. Отправленную рассылку из `ANNOUNCEMENTS`
можно удалить хоть в тот же день — ключи в базе останутся, и даже если запись
вернут обратно, второй раз никто её не получит.

Звук. Весь бот стоит на `DefaultBotProperties(disable_notification=True)`,
рассылка это наследует. Тихих часов тут нет намеренно: пояс у большинства
неизвестен (см. разбор в engagement.py), а беззвучное сообщение никого не
разбудит — ждать до утра ради этого не за чем.
"""

import asyncio
import datetime as dt
import logging
import os
from dataclasses import dataclass, field
from typing import Callable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import FSInputFile

import config
import db
import keyboards

logger = logging.getLogger(__name__)

# Telegram's hard limit on a photo caption.
CAPTION_LIMIT = 1024
# Пауза между отправками — тот же лимит ~30 сообщений в секунду, что у
# /broadcast и у ежедневных пушей.
SEND_DELAY = 0.05
# Ждём, пока поллинг встанет на ноги, и только потом начинаем рассылку: первым
# делом после старта бот должен отвечать живым людям, а не разгребать очередь
# на всю базу. Заодно контейнер, который падает сразу после старта, не успеет
# отправить ничего.
STARTUP_DELAY_SECONDS = 60


@dataclass
class Announcement:
    """Одна разовая рассылка.

    `key` — он же `pushes.category`, менять у уже отправленной нельзя: новый
    ключ означает «это другая рассылка», и она уйдёт всем повторно.

    `buttons` — пары (текст, callback_data). Кнопка ведёт прямо в ту фичу, про
    которую сообщение: релиз без входа в него — это анонс, который человеку
    надо ещё пойти и найти.

    `available` — рассылка ждёт, пока фича реально включена в этом развороте.
    Пуш обязан быть правдой (TONE_OF_VOICE.md): обещать разбор видео там, где
    он не подключён, нельзя.
    """

    key: str
    text: str
    buttons: list[tuple[str, str]]
    image: str | None = None
    available: Callable[[], bool] = lambda: True
    parse_mode: str | None = "HTML"
    _file_id: list[str] = field(default_factory=list, repr=False)


RELEASE_AI_PROGRAMS_AND_VIDEO = Announcement(
    key="release_ai_programs_and_video",
    text=(
        "ПРИВЕТ АТЛЕТ, я подрос в двух местах.\n\n"
        "🤖 <b>Составляю программы.</b> Скажешь, сколько дней в неделю тянешь и "
        "к чему идёшь, — соберу план на недели вперёд: упражнения, подходы, веса. "
        "Что-то не по тебе — скажи словами, переделаю.\n\n"
        "🎥 <b>Смотрю технику по видео.</b> Пришли ролик подхода — гляну "
        "траекторию грифа, спину и колени и скажу, что править первым.\n\n"
        "Обе штуки уже работают. Выбирай, с чего начнём."
    ),
    buttons=[
        ("🤖 Собрать программу", "ai:buildprog"),
        ("🎥 Разобрать видео", "ai:videohint"),
    ],
    # Файла может и не быть: тогда уходит текстом. Картинку под релиз кладём
    # сюда же — промпт для генерации в docs/release_ai_programs_and_video.md.
    image=os.path.join(os.path.dirname(__file__), "media", "push", "release_ai_programs_and_video.jpg"),
    available=config.video_analysis_available,
)

# Что ещё не разослано. Отправленную рассылку отсюда убираем — база помнит её
# и без этого списка.
ANNOUNCEMENTS: list[Announcement] = [RELEASE_AI_PROGRAMS_AND_VIDEO]


def _as_caption(text: str) -> str:
    if len(text) <= CAPTION_LIMIT:
        return text
    return text[: CAPTION_LIMIT - 1].rstrip() + "…"


async def _send_one(bot: Bot, telegram_id: int, ann: Announcement) -> None:
    """Одна отправка, с одной повторной попыткой, если Telegram просит подождать."""
    kb = keyboards.announcement_keyboard(ann.buttons)
    photo = ann._file_id[0] if ann._file_id else _photo(ann)
    if photo is None:
        kwargs = dict(chat_id=telegram_id, text=ann.text, reply_markup=kb, parse_mode=ann.parse_mode)
        send = bot.send_message
    else:
        kwargs = dict(
            chat_id=telegram_id,
            photo=photo,
            caption=_as_caption(ann.text),
            reply_markup=kb,
            parse_mode=ann.parse_mode,
        )
        send = bot.send_photo
    try:
        message = await send(**kwargs)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        message = await send(**kwargs)
    # Telegram отдаёт file_id после первой загрузки; дальше по базе едет он, а
    # не файл — картинка у всех одна.
    if photo is not None and not ann._file_id and getattr(message, "photo", None):
        ann._file_id.append(message.photo[-1].file_id)


def _photo(ann: Announcement) -> FSInputFile | None:
    if ann.image and os.path.exists(ann.image):
        return FSInputFile(ann.image)
    return None


async def send_announcement(bot: Bot, ann: Announcement) -> tuple[int, int, int]:
    """Разослать всем, кому ещё не уходило. Возвращает (доставлено, заблокировали, ошибок).

    Каждая ошибка Telegram остаётся внутри цикла: это проход по всей базе, и
    исключение наружу означало бы, что все, кто стоял в очереди после
    удалённого чата, не получат релиз вообще — до следующего деплоя.
    """
    recipients = await db.list_announcement_recipients(ann.key)
    if not recipients:
        return 0, 0, 0
    logger.info("Announcement %s: %s recipients pending", ann.key, len(recipients))
    sent = blocked = failed = 0
    today = dt.date.today().isoformat()
    for telegram_id in recipients:
        try:
            await _send_one(bot, telegram_id, ann)
        except TelegramForbiddenError:
            # Человек заблокировал бота. Гасим тумблер, как это делает
            # ежедневный пуш, — иначе он остаётся в пуле каждой следующей
            # рассылки навсегда.
            blocked += 1
            await db.update_user(telegram_id, pushes_enabled=0)
            continue
        except TelegramAPIError:
            # Отметку не пишем: не дошло — попробуем на следующем старте.
            failed += 1
            logger.exception("Announcement %s failed for user %s", ann.key, telegram_id)
            continue
        sent += 1
        await db.record_push(telegram_id, ann.key, ann.text, today)
        await asyncio.sleep(SEND_DELAY)
    logger.info("Announcement %s done: %s sent, %s blocked, %s failed", ann.key, sent, blocked, failed)
    return sent, blocked, failed


async def run_pending_announcements(bot: Bot) -> None:
    """Фоновая задача старта: разослать всё, что ещё не разослано."""
    if not config.ANNOUNCEMENTS_ENABLED or not ANNOUNCEMENTS:
        return
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    for ann in ANNOUNCEMENTS:
        if not ann.available():
            logger.info("Announcement %s waits: the feature is off in this deploy", ann.key)
            continue
        try:
            sent, blocked, failed = await send_announcement(bot, ann)
        except Exception:
            logger.exception("Announcement %s crashed", ann.key)
            continue
        if sent or failed:
            await _report_to_admin(bot, ann, sent, blocked, failed)


async def _report_to_admin(bot: Bot, ann: Announcement, sent: int, blocked: int, failed: int) -> None:
    if not config.ADMIN_ID:
        return
    try:
        await bot.send_message(
            config.ADMIN_ID,
            f"📢 Разовая рассылка «{ann.key}»: {sent} доставлено, "
            f"{blocked} заблокировали бота, {failed} ошибок.",
        )
    except TelegramAPIError:
        logger.exception("Failed to report announcement %s to admin", ann.key)
