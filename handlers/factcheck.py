"""Переслал пост из фитнес-канала — тренер разбирает: что дело, что бред.

Форвард ловится вне зависимости от текущего состояния (регистрируется в
main.py раньше FSM-роутеров, по той же причине, что и /food_diary или /mcp
там же) — это самостоятельное действие человека, а не ответ на вопрос экрана,
и заставлять его сперва открыть чат тренера было бы лишним шагом. Посреди
активной тренировки или диалога с тренером форвард тоже перехватывается здесь
первым: переслать пост и всё-таки иметь в виду вопрос экрана — комбинация,
которая на практике не встречается.
"""

import logging
from contextlib import suppress

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

import ai_limits
import ai_trainer
import db
import formatting

router = Router(name="factcheck")

logger = logging.getLogger(__name__)

# Короче — не пост, а обрывок («го», «спс», кусок стикер-подписи); длиннее —
# предохранитель от форварда не по теме (переслали статью на разворот).
_MIN_LEN = 30
_MAX_LEN = 4000

_PLACEHOLDER = "🧐 гляну, что там понаписали..."


def _looks_like_a_forwarded_post(message: Message) -> bool:
    if not (message.forward_origin and message.text):
        return False
    if not ai_trainer.is_configured():
        # Фильтром, а не внутри хендлера: молча съеденное сообщение осталось бы
        # без единого ответа, а так форвард долетит до fallback.unhandled_text.
        return False
    return _MIN_LEN <= len(message.text.strip()) <= _MAX_LEN


@router.message(_looks_like_a_forwarded_post)
async def factcheck_forward(message: Message) -> None:
    user_id = message.from_user.id
    # Та же квота, что у обычных вопросов тренеру: это тоже вопрос, просто с
    # чужим текстом вместо своего.
    block = await ai_limits.check(user_id, ai_limits.KIND_QUESTION)
    if block is not None:
        logger.info("fact-check blocked for user %s: %s", user_id, block.log)
        await ai_limits.reply(message, block)
        if not block.preview:
            return

    placeholder = await message.reply(_PLACEHOLDER)
    try:
        verdict = await ai_trainer.fact_check_post(user_id, message.text.strip())
    except Exception:
        logger.exception("fact-check failed for user %s", user_id)
        with suppress(TelegramBadRequest):
            await placeholder.edit_text("Не разобрал — что-то сломалось. Пришли ещё раз?")
        return

    await db.increment_ai_question_count(user_id)
    with suppress(TelegramBadRequest):
        await placeholder.edit_text(
            formatting.ai_markdown_to_html(verdict), parse_mode="HTML"
        )
