"""AI-тренер: чат с Grok, у которого есть доступ к данным текущего пользователя."""

import asyncio
import base64
import logging
import random
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

# Лимит на размер голосового (Telegram сам не режет сильнее, но перестрахуемся).
MAX_VOICE_BYTES = 20 * 1024 * 1024

# Длиннее — явно не короткий вопрос, дороже распознавать и дольше ждать ответ.
MAX_VOICE_SECONDS = 300

INTRO_TEXT = (
    "🤖 <b>ПРИВЕТ, АТЛЕТ. ТРЕНЕР НА СВЯЗИ.</b>\n\n"
    "У меня есть доступ к истории твоих тренировок и многолетний тренерский опыт. "
    "Спрашивай что угодно:\n"
    "• «Как прогресс в жиме лёжа? Почему не растёт присед?»\n"
    "• «Дай совет по программе тренировок»\n"
    "• «Сколько белка есть, чтобы расти?»\n\n"
    "Пиши вопрос 👇 (можно голосом — жми на 🎤)"
)

# Пользователи, чей вопрос сейчас обрабатывается — защита от параллельных запросов.
_busy: set[int] = set()

# Крутятся в placeholder-сообщении, пока модель думает — вместо голого "печатает..."
# на несколько секунд/десятков секунд (особенно с tool-calls и веб-поиском под капотом).
RUNNING_REPLIES = [
    "💪 держи паузу, сейчас будет по делу...",
    "🧠 включаю тренерский мозг, момент...",
    "🔥 разминаюсь перед ответом...",
    "🎯 целюсь в точный совет, не спугни...",
    "🧘 собираю мысли, не гони...",
    "🏋️ гружу знания, как штангу — по чуть-чуть...",
    "📖 сверяюсь с методикой, секунду...",
    "⏱️ отдыхаю между подходами мысли, погоди...",
    "🥩 перевариваю вопрос, дай времени...",
    "🧊 остываю от подхода, сейчас отвечу...",
    "🩹 разбираю по косточкам, момент...",
    "🚿 после подхода думается чётче, секунду...",
    "🧢 не гони, тренер думает медленно, но метко...",
    "🥊 обдумываю удар точнее, чем ты жим...",
    "🍗 заряжаюсь белком мысли, момент...",
    "🧱 закладываю фундамент ответа...",
    "⚡ собираю энергию для ответа...",
    "🗿 стою как штанга — думаю тяжело, но верно...",
    "🧭 нахожу верное направление, секунду...",
    "🛠️ докручиваю ответ, почти готово...",
]

# Интервал ротации placeholder-текста, секунды.
RUNNING_INTERVAL = 2.8


def _pick(replies: list[str]) -> str:
    return random.choice(replies)


def _pick_different(replies: list[str], exclude: Optional[str]) -> str:
    """Случайная реплика, отличная от предыдущей — иначе editText упадёт с
    "message is not modified", да и ротация без этого выглядит нечестно."""
    if len(replies) <= 1:
        return _pick(replies)
    choice = exclude
    while choice == exclude:
        choice = _pick(replies)
    return choice


async def _cycle_running_messages(placeholder: Message, initial_text: str) -> None:
    last_text = initial_text
    while True:
        await asyncio.sleep(RUNNING_INTERVAL)
        last_text = _pick_different(RUNNING_REPLIES, last_text)
        with suppress(TelegramBadRequest):
            await placeholder.edit_text(last_text)


async def ai_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """AI-trainer reply keyboard: 'К тренировке' instead of 'Меню' while a workout is active."""
    active = await db.get_active_workout(user_id)
    return keyboards.ai_trainer_keyboard(has_active_workout=bool(active))


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
    await ui.safe_edit(
        callback, INTRO_TEXT, reply_markup=await ai_keyboard(callback.from_user.id), parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "ai:menu")
async def ai_to_menu(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == "ai:resume_workout")
async def ai_resume_workout(callback: CallbackQuery, state: FSMContext):
    """'К тренировке' from the AI-trainer chat — unlike menu:resume_workout, keeps the
    AI conversation in the chat instead of deleting the message the button was on.
    """
    from handlers.workout import _enter_live

    active = await db.get_active_workout(callback.from_user.id)
    if not active:
        await callback.answer("Нет активной тренировки", show_alert=True)
        return
    await callback.answer()
    await _enter_live(callback, state, active["id"], delete_message=False)


@router.callback_query(F.data == "ai:reset")
async def ai_reset(callback: CallbackQuery, state: FSMContext):
    await state.update_data(ai_history=[])
    await ui.safe_edit(
        callback,
        "🗑 Начали с чистого листа. Задавай вопрос!",
        reply_markup=await ai_keyboard(callback.from_user.id),
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
    running_text = _pick(RUNNING_REPLIES)
    placeholder = await message.answer(running_text)
    running_task = asyncio.create_task(_cycle_running_messages(placeholder, running_text))
    try:
        answer = await ai_trainer.ask(user_id, question, history, image_data_url=image_data_url)
    except Exception:
        logger.exception("AI trainer request failed for user %s", user_id)
        with suppress(TelegramBadRequest):
            await placeholder.edit_text(
                "⚠️ Не получилось получить ответ, попробуй ещё раз чуть позже.",
                reply_markup=await ai_keyboard(user_id),
            )
        return
    finally:
        running_task.cancel()
        with suppress(asyncio.CancelledError):
            await running_task
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

    reply_markup = await ai_keyboard(user_id)
    chunks = [answer[i : i + TG_CHUNK] for i in range(0, len(answer), TG_CHUNK)]
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        markup = reply_markup if is_last else None
        html_chunk = formatting.markdown_bold_to_html(chunk)
        if i == 0:
            try:
                await placeholder.edit_text(html_chunk, parse_mode="HTML", reply_markup=markup)
                continue
            except TelegramBadRequest:
                pass  # разошлось с ротацией (например текст не изменился) — просто шлём отдельным сообщением
        await message.answer(html_chunk, parse_mode="HTML", reply_markup=markup)


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


async def _download_voice_as_file(message: Message):
    voice = message.voice
    if voice.file_size and voice.file_size > MAX_VOICE_BYTES:
        return None
    buf = await message.bot.download(voice)
    buf.name = "voice.ogg"
    return buf


@router.message(AITrainerFlow.chatting, F.voice)
async def ai_voice_question(message: Message, state: FSMContext):
    if message.from_user.id in _busy:
        await message.reply("Секунду, ещё думаю над прошлым вопросом 😅")
        return
    if not ai_trainer.is_voice_configured():
        await message.reply("Голосовой ввод пока не настроен, напиши вопрос текстом.")
        return
    if message.voice.duration and message.voice.duration > MAX_VOICE_SECONDS:
        await message.reply("Голосовое слишком длинное, запиши покороче.")
        return

    voice_file = await _download_voice_as_file(message)
    if voice_file is None:
        await message.reply("Голосовое слишком большое, запиши покороче.")
        return

    try:
        question = await ai_trainer.transcribe_voice(voice_file)
    except Exception:
        logger.exception("AI trainer voice transcription failed for user %s", message.from_user.id)
        await message.reply("⚠️ Не получилось распознать голосовое, попробуй ещё раз или напиши текстом.")
        return

    if not question:
        await message.reply("🤐 Не удалось разобрать речь, попробуй ещё раз.")
        return

    await _handle_question(message, state, question, history_question=question)
