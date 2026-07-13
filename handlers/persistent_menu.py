"""Persistent reply-keyboard buttons shown under the input field.

Registered before every other router in main.py so pressing one of these
buttons always short-circuits whatever FSM state the user is currently in —
same hard reset semantics as /start.
"""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import ai_trainer
import db
import keyboards
from fsm import AITrainerFlow
from handlers.ai_trainer import INTRO_TEXT, ai_keyboard

router = Router(name="persistent_menu")


class _MessageAsCallback:
    """Adapts a Message to the (.message, .from_user, .bot) shape that
    workout.py's screen builders expect from a CallbackQuery, so the
    persistent-keyboard handler can reuse them without forking the flow.
    """

    def __init__(self, message: Message):
        self.message = message
        self.from_user = message.from_user
        self.bot = message.bot


@router.message(F.text == keyboards.BTN_MENU)
async def persistent_menu_button(message: Message, state: FSMContext) -> None:
    from handlers.workout import cmd_start

    await cmd_start(message, state)


@router.message(F.text == keyboards.BTN_WORKOUT)
async def persistent_workout_button(message: Message, state: FSMContext) -> None:
    from handlers.workout import start_workout

    await state.clear()
    await start_workout(_MessageAsCallback(message), state)


async def _open_ai_trainer(message: Message, state: FSMContext) -> None:
    if not ai_trainer.is_configured():
        await message.answer("AI-тренер не настроен: администратору нужно задать XAI_API_KEY.")
        return
    await db.get_or_create_user(message.from_user.id, message.from_user.username)
    await state.clear()
    await state.set_state(AITrainerFlow.chatting)
    await state.update_data(ai_history=[])
    await message.answer(INTRO_TEXT, reply_markup=await ai_keyboard(message.from_user.id))


@router.message(F.text == keyboards.BTN_AI)
async def persistent_ai_button(message: Message, state: FSMContext) -> None:
    await _open_ai_trainer(message, state)


@router.message(Command("ai_trainer"))
async def cmd_ai_trainer(message: Message, state: FSMContext) -> None:
    await _open_ai_trainer(message, state)
