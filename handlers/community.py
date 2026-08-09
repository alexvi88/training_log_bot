"""/community — вход в общий чат атлетов.

Чат — обычная телеграм-группа снаружи бота: люди разговаривают там между
собой, бот туда не пишет и ничего оттуда не читает. Его дело — дать дорогу:
кнопка «💬 Чат атлетов» в главном меню (см. keyboards.main_menu) и эта команда
для тех, кто ссылку потерял.

Раздела нет вовсе, пока адрес группы не задан (config.community_available):
кнопка в никуда хуже отсутствующей кнопки.
"""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import db

router = Router(name="community")

INTRO = (
    "💬 <b>ЧАТ АТЛЕТОВ</b>\n\n"
    "Общая группа тех, кто ведёт дневник здесь же. Спрашивай про технику, "
    "показывай подходы, обсуждай программы — говорят там атлеты между собой, "
    "я в разговор не лезу.\n\n"
    "Жми кнопку и заходи."
)

NOT_READY = "Общего чата пока нет. Заведу — кнопка появится в меню, скажу отдельно."

BUTTON_TEXT = "💬 Зайти в чат"


def community_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BUTTON_TEXT, url=config.COMMUNITY_CHAT_URL)]
        ]
    )


@router.message(Command("community"))
async def cmd_community(message: Message):
    await db.get_or_create_user(message.from_user.id, message.from_user.username)
    if not config.community_available():
        await message.answer(NOT_READY)
        return
    await message.answer(INTRO, reply_markup=community_keyboard(), parse_mode="HTML")
