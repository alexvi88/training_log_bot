"""/game — мини-игра «Кач-Раннер» (Telegram Mini App).

Доступ пока сознательно только слеш-командой, без кнопок в меню: игра —
эксперимент, и главное меню не должно её обещать каждому. Кнопка в самом
ответе на команду — не навигация, а единственный способ открыть Mini App
без настройки прямой ссылки в BotFather.
"""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

import config
import db
import game_server

router = Router(name="game")

INTRO = (
    "🏃 <b>КАЧ-РАННЕР</b>\n\n"
    "Беги по залу, собирай спортпит и не влетай в гантели — я засеку, докуда добежишь."
)


def game_url() -> str:
    # Страницу отдаёт тот же сервер, что и MCP, — адрес у них общий.
    return config.MCP_PUBLIC_URL + game_server.GAME_PATH


@router.message(Command("game"))
async def cmd_game(message: Message):
    await db.get_or_create_user(message.from_user.id, message.from_user.username)
    if not config.mcp_available():
        # Без публичного адреса страницу игры никто не отдаёт (см. main.py:
        # HTTP-сервер поднимается только вместе с MCP).
        await message.answer("Игра пока не подключена — это к админу бота.")
        return
    best = await db.get_game_best_distance(message.from_user.id)
    text = INTRO + (f"\n\nТвой рекорд — {best} м. Перебьёшь?" if best else "")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Побежали", web_app=WebAppInfo(url=game_url()))]
        ]
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
