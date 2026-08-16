"""/game — выбор мини-игры (Telegram Mini App): «Кач-Раннер» и «Кач-Отряд».

Доступ пока сознательно только слеш-командой, без кнопок в меню: игры —
эксперимент, и главное меню не должно их обещать каждому. Кнопки в самом
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
    "🏃 <b>МИНИ-ИГРЫ</b>\n\n"
    "Беги по залу или веди отряд качков через полосу препятствий — выбирай, где размяться."
)


def game_url() -> str:
    # Страницу отдаёт тот же сервер, что и MCP, — адрес у них общий.
    return config.MCP_PUBLIC_URL + game_server.GAME_PATH


def squad_url() -> str:
    return config.MCP_PUBLIC_URL + game_server.SQUAD_PATH


@router.message(Command("game"))
async def cmd_game(message: Message):
    await db.get_or_create_user(message.from_user.id, message.from_user.username)
    if not config.mcp_available():
        # Без публичного адреса страницу игры никто не отдаёт (см. main.py:
        # HTTP-сервер поднимается только вместе с MCP).
        await message.answer("Игра пока не подключена — загляни позже.")
        return
    best_runner = await db.get_game_best_distance(message.from_user.id)
    best_squad = await db.get_squad_best_score(message.from_user.id)
    records = []
    if best_runner:
        records.append(f"Кач-Раннер — рекорд {best_runner} м")
    if best_squad:
        records.append(f"Кач-Отряд — рекорд {best_squad} очков")
    text = INTRO + ("\n\n" + ", ".join(records) + "." if records else "")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏃 Кач-Раннер", web_app=WebAppInfo(url=game_url()))],
            [InlineKeyboardButton(text="💪 Кач-Отряд", web_app=WebAppInfo(url=squad_url()))],
        ]
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
