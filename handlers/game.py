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
import i18n

router = Router(name="game")


def _lang_query() -> str:
    """`?lang=` для страницы игры.

    Страница статическая и своего языка ниоткуда взять не может, кроме как из
    Telegram WebApp — а там лежит язык КЛИЕНТА, а не наш users.lang. Для
    человека с русским телефоном, выбравшего в настройках English, это разные
    вещи, и без явной передачи он получал бы игру целиком по-русски. Ссылку
    строит хендлер, где язык пользователя уже выставлен middleware, — значит
    передать его сюда ничего не стоит.
    """
    return f"?lang={i18n.get_lang()}"


def game_url() -> str:
    # Страницу отдаёт тот же сервер, что и MCP, — адрес у них общий.
    return config.MCP_PUBLIC_URL + game_server.GAME_PATH + _lang_query()


def squad_url() -> str:
    return config.MCP_PUBLIC_URL + game_server.SQUAD_PATH + _lang_query()


@router.message(Command("game"))
async def cmd_game(message: Message):
    await db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.language_code
    )
    if not config.mcp_available():
        # Без публичного адреса страницу игры никто не отдаёт (см. main.py:
        # HTTP-сервер поднимается только вместе с MCP).
        await message.answer(i18n.t("game.not_configured"))
        return
    best_runner = await db.get_game_best_distance(message.from_user.id)
    best_squad = await db.get_squad_best_score(message.from_user.id)
    records = []
    if best_runner:
        records.append(i18n.t("game.record.runner", n=best_runner))
    if best_squad:
        records.append(i18n.t("game.record.squad", n=best_squad))
    text = i18n.t("game.intro") + ("\n\n" + ", ".join(records) + "." if records else "")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=i18n.t("game.btn.runner"), web_app=WebAppInfo(url=game_url()))],
            [InlineKeyboardButton(text=i18n.t("game.btn.squad"), web_app=WebAppInfo(url=squad_url()))],
        ]
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
