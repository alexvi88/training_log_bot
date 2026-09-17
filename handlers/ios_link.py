"""/ios — код связки для iOS-приложения (training_log_bot_ios, см. api_v1.py).

Тот же приём, что у /mcp: одноразовый шестизначный код доказывает
iOS-приложению, что телефон и телеграм-аккаунт — один человек. Код и его срок —
общие с mcp_oauth (`db.oauth_link_codes`): это ровно та же проверка «ты
владеешь этим аккаунтом», не два разных механизма.

Отдельная команда, а не кнопка на экране /mcp: у приложения нет ни адреса, ни
выбора клиента — только код, который сразу можно вставить в приложение.
"""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import db
import i18n
import mcp_oauth

router = Router(name="ios_link")


def _ios_link_text(code: str) -> str:
    minutes = mcp_oauth.LINK_CODE_TTL_MINUTES
    return i18n.t("ios_link.screen", code=f"<pre>{code}</pre>", ttl=i18n.t("mcp.code_ttl", n=minutes))


@router.message(Command("ios"))
async def cmd_ios_link(message: Message):
    await db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.language_code
    )
    code = await mcp_oauth.link_code(message.from_user.id, force_new=True)
    await message.answer(_ios_link_text(code), parse_mode="HTML")
