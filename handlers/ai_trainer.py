"""AI-тренер: чат с Grok, у которого есть доступ к данным текущего пользователя."""

import asyncio
import base64
import logging
from contextlib import suppress
from typing import Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import ai_trainer
import db
import formatting
import keyboards
import ui
from fsm import AITrainerFlow

router = Router(name="ai_trainer")

logger = logging.getLogger(__name__)

# Сколько последних реплик (вопрос+ответ = 2) держим в контексте диалога.
# Чётное число, чтобы история всегда начиналась с реплики пользователя.
HISTORY_LIMIT = 12

# Telegram обрезает сообщения на 4096 символах; режем с запасом.
TG_CHUNK = 4000

# Лимит xAI на размер одного изображения (см. xai_sdk.chat.image).
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Вопрос по умолчанию, если пользователь прислал фото без подписи.
DEFAULT_PHOTO_QUESTION = "Посмотри на фото и прокомментируй."

INTRO_TEXT = (
    "🤖 ПРИВЕТ, АТЛЕТ. ТРЕНЕР НА СВЯЗИ.\n\n"
    "Я вижу твой дневник: тренировки, подходы, рекорды и прогресс. Спрашивай что угодно, например:\n"
    "• «Почему у меня застой в приседе?»\n"
    "• «Как у меня прогресс в жиме лёжа?»\n"
    "• «Как составить программу тренировок на неделю?»\n"
    "• «Сколько белка мне есть, чтобы расти?»\n\n"
    "Можно прислать и фото — с подписью или без: технику упражнения, скриншот "
    "тренировки, этикетку продукта.\n\n"
    "Просто напиши вопрос сообщением 👇"
)

# Пользователи, чей вопрос сейчас обрабатывается — защита от параллельных запросов.
_busy: set[int] = set()


async def _keep_typing(message: Message) -> None:
    # "typing" в Telegram живёт ~5 секунд, а ответ модели может занять дольше.
    while True:
        await message.bot.send_chat_action(message.chat.id, "typing")
        await asyncio.sleep(4)


@router.callback_query(F.data == "menu:ai")
async def menu_ai(callback: CallbackQuery, state: FSMContext):
    if not ai_trainer.is_configured():
        await callback.answer(
            "AI-тренер не настроен: администратору нужно задать XAI_API_KEY.",
            show_alert=True,
        )
        return
    await state.set_state(AITrainerFlow.chatting)
    await state.update_data(ai_history=[])
    await ui.safe_edit(callback, INTRO_TEXT, reply_markup=keyboards.ai_trainer_keyboard())
    await callback.answer()


@router.callback_query(F.data == "ai:menu")
async def ai_to_menu(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == "ai:reset")
async def ai_reset(callback: CallbackQuery, state: FSMContext):
    await state.update_data(ai_history=[])
    await ui.safe_edit(
        callback,
        "🗑 Начали с чистого листа. Задавай вопрос!",
        reply_markup=keyboards.ai_trainer_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ai:comment:"))
async def ai_comment_workout(callback: CallbackQuery, state: FSMContext):
    """Ручной запрос комментария к тренировке — кнопка на карточке завершённой тренировки.

    Работает и на свежезавершённой карточке, и на карточке из истории: правит то же
    сообщение на месте, убирая из клавиатуры только саму эту кнопку.
    """
    workout_id = int(callback.data.split(":")[2])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    if not ai_trainer.is_configured():
        await callback.answer("AI-тренер не настроен.", show_alert=True)
        return
    await callback.answer()

    comment = workout["ai_comment"]
    if not comment:
        try:
            comment = await ai_trainer.comment_on_workout(callback.from_user.id, workout_id)
        except Exception:
            logger.exception("AI trainer workout comment failed for workout %s", workout_id)
            await callback.message.answer("⚠️ Не получилось получить комментарий, попробуй ещё раз позже.")
            return
        await db.set_workout_ai_comment(workout_id, comment)

    new_text = (callback.message.html_text or "") + "\n\n" + formatting.build_ai_comment_block(comment)
    existing_kb = callback.message.reply_markup
    rows = existing_kb.inline_keyboard if existing_kb else []
    new_rows = [
        [btn for btn in row if not (btn.callback_data or "").startswith("ai:comment:")] for row in rows
    ]
    new_rows = [r for r in new_rows if r]
    new_markup = InlineKeyboardMarkup(inline_keyboard=new_rows) if new_rows else None
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(new_text, parse_mode="HTML", reply_markup=new_markup)


async def _download_photo_as_data_url(message: Message) -> Optional[str]:
    photo = message.photo[-1]
    if photo.file_size and photo.file_size > MAX_IMAGE_BYTES:
        return None
    buf = await message.bot.download(photo)
    return "data:image/jpeg;base64," + base64.b64encode(buf.read()).decode()


async def _handle_question(
    message: Message,
    state: FSMContext,
    question: str,
    history_question: str,
    image_data_url: Optional[str] = None,
) -> None:
    """Общая логика для текстовых и фото-вопросов: запрос к модели, история, отправка ответа.

    question — то, что реально уходит модели на этот ход (текст +, если есть, фото).
    history_question — облегчённая версия для ai_history/БД: фото туда не попадают
    (не пересылать же их каждый следующий ход), только текст/подпись или заглушка.
    """
    user_id = message.from_user.id
    if user_id in _busy:
        await message.reply("Секунду, ещё думаю над прошлым вопросом 😅")
        return

    data = await state.get_data()
    history = data.get("ai_history", [])

    _busy.add(user_id)
    typing = asyncio.create_task(_keep_typing(message))
    try:
        answer = await ai_trainer.ask(user_id, question, history, image_data_url=image_data_url)
    except Exception:
        logger.exception("AI trainer request failed for user %s", user_id)
        await message.answer(
            "⚠️ Не получилось получить ответ, попробуй ещё раз чуть позже.",
            reply_markup=keyboards.ai_trainer_keyboard(),
        )
        return
    finally:
        typing.cancel()
        _busy.discard(user_id)

    history = (
        history
        + [
            {"role": "user", "content": history_question},
            {"role": "assistant", "content": answer},
        ]
    )[-HISTORY_LIMIT:]
    await state.update_data(ai_history=history)

    # Full, permanent log — separate from the live window above, which is
    # capped and wiped on ai:reset. Lets the model pull it back via the
    # get_full_chat_history tool if a later question references it.
    await db.add_ai_chat_message(user_id, "user", history_question)
    await db.add_ai_chat_message(user_id, "assistant", answer)

    chunks = [answer[i : i + TG_CHUNK] for i in range(0, len(answer), TG_CHUNK)]
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await message.answer(
            chunk, reply_markup=keyboards.ai_trainer_keyboard() if is_last else None
        )


@router.message(AITrainerFlow.chatting, F.text)
async def ai_question(message: Message, state: FSMContext):
    question = (message.text or "").strip()
    if not question:
        return
    await _handle_question(message, state, question, history_question=question)


@router.message(AITrainerFlow.chatting, F.photo)
async def ai_photo_question(message: Message, state: FSMContext):
    if message.from_user.id in _busy:
        await message.reply("Секунду, ещё думаю над прошлым вопросом 😅")
        return

    caption = (message.caption or "").strip()
    question = caption or DEFAULT_PHOTO_QUESTION

    image_data_url = await _download_photo_as_data_url(message)
    if image_data_url is None:
        await message.reply("Фото слишком большое, пришли поменьше.")
        return

    history_question = f"[фото] {caption}" if caption else "[прислал фото]"
    await _handle_question(
        message, state, question, history_question=history_question, image_data_url=image_data_url
    )
