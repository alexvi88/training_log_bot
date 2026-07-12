"""AI-тренер: чат с Claude, у которого есть доступ к данным текущего пользователя."""

import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import ai_trainer
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

INTRO_TEXT = (
    "🤖 AI-тренер на связи!\n\n"
    "Я вижу твой дневник: тренировки, подходы, рекорды и прогресс. Спрашивай что угодно, например:\n"
    "• «Как у меня прогресс в жиме лёжа?»\n"
    "• «Что мне потренировать сегодня?»\n"
    "• «Почему у меня застой в приседе?»\n\n"
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
            "AI-тренер не настроен: администратору нужно задать ANTHROPIC_API_KEY.",
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


@router.message(AITrainerFlow.chatting, F.text)
async def ai_question(message: Message, state: FSMContext):
    user_id = message.from_user.id
    question = (message.text or "").strip()
    if not question:
        return
    if user_id in _busy:
        await message.reply("Секунду, ещё думаю над прошлым вопросом 😅")
        return

    data = await state.get_data()
    history = data.get("ai_history", [])

    _busy.add(user_id)
    typing = asyncio.create_task(_keep_typing(message))
    try:
        answer = await ai_trainer.ask(user_id, question, history)
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
        + [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
    )[-HISTORY_LIMIT:]
    await state.update_data(ai_history=history)

    chunks = [answer[i : i + TG_CHUNK] for i in range(0, len(answer), TG_CHUNK)]
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await message.answer(
            chunk, reply_markup=keyboards.ai_trainer_keyboard() if is_last else None
        )
