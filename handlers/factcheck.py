"""Переслал пост из фитнес-канала — тренер разбирает: что дело, что бред.

Форвард ловится вне зависимости от текущего состояния (регистрируется в
main.py раньше FSM-роутеров, по той же причине, что и /food_diary или /mcp
там же) — это самостоятельное действие человека, а не ответ на вопрос экрана,
и заставлять его сперва открыть чат тренера было бы лишним шагом. Посреди
активной тренировки или диалога с тренером форвард тоже перехватывается здесь
первым: переслать пост и всё-таки иметь в виду вопрос экрана — комбинация,
которая на практике не встречается.

Фото с подписью — тот же случай, и он даже более частый: пост из канала обычно
приходит картинкой (скриншот замеров, график, инфографика) с текстом в
caption. Первая версия смотрела только `message.text`, поэтому такие посты
проваливались в обычный чат тренера и получали placeholder, угаданный по
ключевым словам подписи, — на посте про семаглутид это выглядело как «ищу, не
слишком ли быстро уходит вес», хотя ни в какой дневник бот не смотрел.
"""

import logging
from contextlib import suppress
from typing import Optional

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

import ai_limits
import ai_trainer
import config
import db
import formatting
import i18n
import running_texts

# Тот же загрузчик, что у фото-вопросов в чате тренера: один лимит размера и
# один формат data-URL на оба места. Цикла нет — handlers/ai_trainer про этот
# модуль не знает.
from handlers.ai_trainer import _download_photo_as_data_url

router = Router(name="factcheck")

logger = logging.getLogger(__name__)

# Короче — не пост, а обрывок («го», «спс», кусок стикер-подписи); длиннее —
# предохранитель от форварда не по теме (переслали статью на разворот).
_MIN_LEN = 30
_MAX_LEN = 4000

# Свой busy-замок, не общий с handlers.ai_trainer._busy: тот держит «идёт
# основной вопрос», этот — «идёт разбор форварда»; смешивать их означало бы,
# что чат с тренером блокирует пересланный пост и наоборот, хотя по продукту
# это два разных действия, которые человек вполне может начать одно за
# другим (переслал пост, пока ждёт ответ на прошлый вопрос).
_busy: set[int] = set()


def _try_claim_busy(user_id: int) -> bool:
    """Atomically check-and-reserve `_busy` for this user.

    Тот же приём, что и `handlers.ai_trainer._try_claim_busy` (см. её
    докстринг про то, почему проверка и `.add()` обязаны идти без единого
    `await` между ними). Без замка гонка была именно той, что описана в
    контракте квоты (handlers/ai_trainer.py, ~2082): «прочитал счётчик →
    дождался ответа модели → увеличил» — а пачка пересланных постов подряд
    (человек форвардит несколько сообщений из канала одно за другим) успевала
    пройти проверку квоты хором, до того как первый из форвардов её увеличил.
    """
    if user_id in _busy:
        return False
    _busy.add(user_id)
    return True


def _post_text(message: Message) -> Optional[str]:
    """Текст поста — из `text` или из `caption` у фото. None, если это не наш случай."""
    raw = (message.text or message.caption or "").strip()
    return raw if _MIN_LEN <= len(raw) <= _MAX_LEN else None


def _looks_like_a_forwarded_post(message: Message) -> bool:
    if not message.forward_origin:
        return False
    if not ai_trainer.is_configured():
        # Фильтром, а не внутри хендлера: молча съеденное сообщение осталось бы
        # без единого ответа, а так форвард долетит до fallback.unhandled_text.
        return False
    # Всё, что не текст и не фото (видео, кружок, документ, опрос), пропускаем
    # дальше по цепочке: разбирать там нечего, а перехватить и промолчать хуже,
    # чем не перехватывать.
    if message.video or message.video_note or message.animation or message.document:
        return False
    return _post_text(message) is not None


@router.message(_looks_like_a_forwarded_post)
async def factcheck_forward(message: Message) -> None:
    user_id = message.from_user.id
    post_text = _post_text(message)
    if not _try_claim_busy(user_id):
        # Разбор прошлого форварда этого же человека ещё не закончился —
        # именно тот промежуток, где раньше и пряталась гонка за квоту
        # (проверка лимита ниже читает счётчик, а списывается он только после
        # ответа модели, секундами позже). Не открываем вторую параллельную
        # дорожку к модели, а сообщаем и ждём, пока освободится первая.
        with suppress(TelegramBadRequest):
            await message.reply(i18n.t("factcheck.busy"))
        return
    try:
        # Та же квота, что у обычных вопросов тренеру: это тоже вопрос, просто с
        # чужим текстом вместо своего.
        block = await ai_limits.check(user_id, ai_limits.KIND_QUESTION)
        if block is not None:
            logger.info("fact-check blocked for user %s: %s", user_id, block.log)
            await ai_limits.reply(message, block)
            if not block.preview:
                return

        placeholder = await message.reply(running_texts.pick(running_texts.fact_check_pool()))
        image_data_url = None
        if message.photo:
            # Не роняем разбор из-за картинки: слишком большое фото или сбой
            # скачивания — разбираем подпись, а промпт велит сказать, что картинку
            # прочитать не удалось, вместо того чтобы делать вид, что её не было.
            try:
                image_data_url = await _download_photo_as_data_url(message)
            except Exception:
                logger.exception("fact-check photo download failed for user %s", user_id)

        try:
            verdict = await ai_trainer.fact_check_post(user_id, post_text, image_data_url)
        except Exception:
            logger.exception("fact-check failed for user %s", user_id)
            with suppress(TelegramBadRequest):
                await placeholder.edit_text(i18n.t("factcheck.failed"))
            return

        # Атомарно — тот же приём, что и у обычного чата (см.
        # handlers/ai_trainer.py и db.try_increment_ai_question_count):
        # списание по-прежнему ПОСЛЕ ответа модели, но само увеличение
        # счётчика не может перескочить дневной потолок.
        await db.try_increment_ai_question_count(user_id, config.AI_QUESTION_DAILY_LIMIT)
        with suppress(TelegramBadRequest):
            await placeholder.edit_text(
                formatting.ai_markdown_to_html(verdict), parse_mode="HTML"
            )
    finally:
        _busy.discard(user_id)

