"""Разовые релизные рассылки: сначала админу на проверку, потом всем.

Зачем отдельный механизм, когда есть `/broadcast` и ежедневные пуши:

* `/broadcast` — ручной от начала до конца: админ должен сам написать текст,
  сам собрать кнопки (а инлайн-кнопки он собрать и не может) и сам нажать
  «Отправить». Релиз выкатывается мержем, и текст с кнопками должен приехать
  вместе с кодом фичи, а не набираться руками в чате.
* Ежедневный пуш (`engagement.py`) — реакция на сигнал в дневнике (пропуск,
  серия, звание). Релиз ни на какой сигнал не опирается: он про продукт, а не
  про атлета, и приходит один раз всем.

## Как это идёт по шагам

1. Бот поднялся после мержа. Через минуту он присылает анонс **админу** — ровно
   в том виде, в каком его увидят люди: та же картинка, тот же текст, те же
   рабочие кнопки. Следом — короткая справка «уйдёт N получателям» с двумя
   кнопками: разослать или отклонить.
2. Админ тыкает кнопки анонса и проверяет, что они ведут куда надо. Это живые
   кнопки, а не картинка кнопок: они запускают те же сценарии, что у всех.
3. Админ жмёт «Разослать всем» — и рассылка идёт. Или «Не надо» — и она больше
   не всплывает.

Ни один шаг не держится в памяти процесса: состояние лежит в
`announcement_state`, а факт доставки конкретному человеку — строкой в
`pushes` с ключом рассылки в `category`. Поэтому перезапуск контейнера на
любом шаге ничего не ломает: анонс на проверке не показывается админу второй
раз, одобренная рассылка продолжается с того места, где оборвалась, а
получивший релиз не получит его снова.

Звук. Весь бот стоит на `DefaultBotProperties(disable_notification=True)`,
рассылка это наследует. Тихих часов тут нет намеренно: пояс у большинства
неизвестен (см. разбор в engagement.py), а беззвучное сообщение никого не
разбудит — ждать до утра ради этого не за чем.
"""

import asyncio
import datetime as dt
import hashlib
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
# Ждём, пока поллинг встанет на ноги, и только потом трогаем админа: первым
# делом после старта бот должен отвечать живым людям.
STARTUP_DELAY_SECONDS = 60

# Состояния рассылки в `announcement_state`.
STATUS_PREVIEW = "preview"  # показана админу, ждёт добра
STATUS_APPROVED = "approved"  # добро есть: идёт или будет продолжена на следующем старте
STATUS_DECLINED = "declined"  # отклонена, больше не всплывает


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
        "🤖 <b>Составляю программы.</b> Задам пару вопросов — про дни, инвентарь "
        "и цель — и соберу сплит: упражнения, подходы, повторы, как прибавлять. "
        "Стартовые веса подскажу по твоей истории. Не по тебе — скажи словами, "
        "переделаю.\n\n"
        "🎥 <b>Смотрю технику по видео.</b> Пришли ролик подхода, снятый сбоку, — "
        "гляну траекторию, спину и колени и скажу, что править первым.\n\n"
        "Обе штуки уже работают. Выбирай, с чего начнём."
    ),
    # Кнопки ведут в те же сценарии, что и из меню, но своими callback'ами:
    # экранные кнопки встают на место сообщения, с которого их нажали, а
    # рассылку съедать нельзя — под ней вторая кнопка, и человек к ней вернётся.
    buttons=[
        ("🤖 Собрать программу", "ann:buildprog"),
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


def by_key(key: str) -> Announcement | None:
    return next((a for a in ANNOUNCEMENTS if a.key == key), None)


def _as_caption(text: str) -> str:
    if len(text) <= CAPTION_LIMIT:
        return text
    return text[: CAPTION_LIMIT - 1].rstrip() + "…"


def _photo(ann: Announcement) -> FSInputFile | None:
    if ann.image and os.path.exists(ann.image):
        return FSInputFile(ann.image)
    return None


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


# ---------- шаг 1: показать админу ----------


async def send_preview(bot: Bot, ann: Announcement) -> bool:
    """Прислать админу сам анонс и под ним — кнопки «разослать / не надо».

    Превью — это не описание рассылки, а она сама: тот же текст, та же
    картинка, те же рабочие кнопки. Проверять анонс по его пересказу
    бессмысленно — сломанную кнопку так не увидишь.
    """
    if not config.ADMIN_ID:
        logger.warning("Announcement %s has nobody to approve it: ADMIN_ID is not set", ann.key)
        return False
    try:
        await _send_one(bot, config.ADMIN_ID, ann)
    except TelegramAPIError:
        logger.exception("Failed to show announcement %s to admin", ann.key)
        return False
    # Показ админу — это и есть его экземпляр релиза: отмечаем доставку, иначе
    # он получит то же самое второй раз вместе со всеми. Повторный показ
    # (/announce) новой отметки не пишет — она уже стоит.
    if not await db.has_announcement_push(config.ADMIN_ID, ann.key):
        await db.record_push(config.ADMIN_ID, ann.key, ann.text, dt.date.today().isoformat())
    pending = await db.count_announcement_recipients(ann.key)
    try:
        await bot.send_message(
            config.ADMIN_ID,
            f"👆 Так релиз «{ann.key}» увидят атлеты. Кнопки под ним рабочие — потыкай.\n\n"
            f"Разослать {pending} получателям?",
            reply_markup=keyboards.yes_no_keyboard(
                f"admin:ann:go:{ann.key}",
                f"admin:ann:no:{ann.key}",
                yes_text="📢 Разослать всем",
                no_text="Не надо",
            ),
        )
    except TelegramAPIError:
        logger.exception("Failed to show announcement %s to admin", ann.key)
        return False
    await db.set_announcement_status(ann.key, STATUS_PREVIEW, text_hash=text_hash(ann))
    return True


def text_hash(ann: Announcement) -> str:
    """Отпечаток того, что видел админ. Меняется вместе с текстом и кнопками —
    и кнопка, ведущая не туда, тоже стоит повторной проверки."""
    payload = ann.text + "|" + "|".join(f"{label}>{data}" for label, data in ann.buttons)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------- шаг 2: разослать ----------


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


async def deliver_and_report(bot: Bot, ann: Announcement) -> tuple[int, int, int]:
    """Разослать одобренный анонс и отчитаться админу."""
    sent, blocked, failed = await send_announcement(bot, ann)
    if sent or failed:
        await _report_to_admin(bot, ann, sent, blocked, failed)
    return sent, blocked, failed


async def _report_to_admin(bot: Bot, ann: Announcement, sent: int, blocked: int, failed: int) -> None:
    if not config.ADMIN_ID:
        return
    try:
        await bot.send_message(
            config.ADMIN_ID,
            f"📢 Релиз «{ann.key}» разослан: {sent} доставлено, "
            f"{blocked} заблокировали бота, {failed} ошибок.",
        )
    except TelegramAPIError:
        logger.exception("Failed to report announcement %s to admin", ann.key)


# ---------- фоновая задача старта ----------


async def _text_changed(ann: Announcement) -> bool:
    stored = await db.get_announcement_text_hash(ann.key)
    # None — превью из версии, которая отпечатков ещё не писала. Считаем, что
    # это тот же текст: показать лишний раз безобиднее, но дёргать админа на
    # каждом рестарте после обновления — нет.
    return stored is not None and stored != text_hash(ann)


async def run_pending_announcements(bot: Bot) -> None:
    """Что делать с каждой рассылкой при старте бота.

    Шаг определяется по состоянию в базе, а не по тому, первый это запуск или
    десятый:

    * нет записи — показать админу и ждать;
    * `preview` — админ ещё не ответил: молчим, пока текст не переписали;
    * `approved` — рассылка одобрена, но контейнер перезапустился посреди неё:
      дорассылаем оставшимся (получившие отсеиваются по `pushes`);
    * `declined` — забыли.
    """
    if not config.ANNOUNCEMENTS_ENABLED or not ANNOUNCEMENTS:
        return
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    for ann in ANNOUNCEMENTS:
        if not ann.available():
            logger.info("Announcement %s waits: the feature is off in this deploy", ann.key)
            continue
        try:
            status = await db.get_announcement_status(ann.key)
            if status is None:
                await send_preview(bot, ann)
            elif status == STATUS_APPROVED:
                await deliver_and_report(bot, ann)
            elif status == STATUS_PREVIEW and await _text_changed(ann):
                # Текст переписали после показа: то, что админ видел, больше не
                # существует, а новую редакцию никто не проверял. Показываем
                # заново — заодно это единственный способ вернуть анонс,
                # потерявшийся в чате, не вспоминая про /announce.
                logger.info("Announcement %s changed since the preview, showing it again", ann.key)
                await send_preview(bot, ann)
        except Exception:
            logger.exception("Announcement %s crashed", ann.key)
