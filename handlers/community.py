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
import i18n

router = Router(name="community")


def _home_menu_button() -> InlineKeyboardButton:
    # Тот же `live:back_to_menu`, что и на карточке законченной тренировки —
    # открывает меню, не требуя своего обработчика (см. handlers/workout.py).
    # Экран /community доходит и до тех, у кого кнопки чата ещё нет в главном
    # меню (handlers.workout._main_menu_kb, порог — 3 законченные
    # тренировки), так что без неё это был бы тупик.
    return InlineKeyboardButton(text=i18n.t("btn.home_menu"), callback_data="live:back_to_menu")


def community_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=i18n.t("community.btn.enter"), url=config.COMMUNITY_CHAT_URL)],
            [_home_menu_button()],
        ]
    )


@router.message(Command("community"))
async def cmd_community(message: Message):
    await db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.language_code
    )
    if not config.community_available():
        await message.answer(i18n.t("community.not_ready"))
        return
    await message.answer(i18n.t("community.intro"), reply_markup=community_keyboard(), parse_mode="HTML")
