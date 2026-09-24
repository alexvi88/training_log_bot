"""/ios, /link_app — код связки для iOS-приложения (training_log_bot_ios, см.
api_v1.py).

Два направления одного и того же приёма — доказать, что телефон и
телеграм-аккаунт принадлежат одному человеку, — общим кодом и общим
хранилищем (`db.oauth_link_codes`, срок и лимит попыток — mcp_oauth):

  - /ios: код показывает бот, вводит его человек в приложении — обычная
    привязка телеграм-аккаунта к телефону (см. api_v1.auth_link/auth_apple).
  - /link_app: код показывает приложение (человек завёл аккаунт там через
    Sign in with Apple, без Telegram — App Review не пропускает приложения,
    которые нельзя завести без стороннего мессенджера), а вводит его человек
    здесь, боту. У app-only аккаунта нет чата, куда бот мог бы прислать код
    сам, — направление ролей тут обязано быть обратным.

Обе — отдельные команды, а не кнопки на экране /mcp: у приложения нет ни
адреса, ни выбора клиента — только код, который сразу можно вставить.
"""

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Message

import db
import i18n
import mcp_oauth

router = Router(name="ios_link")


def _ios_link_text(code: str) -> str:
    minutes = mcp_oauth.LINK_CODE_TTL_MINUTES
    # <code>, а не <pre>: блок <pre> на телефоне сужается под шесть цифр, и
    # его значок «копировать» ложится поверх последней цифры, а сам блок
    # копироваться не хотел. Строчный <code> копируется тапом, а надёжный
    # способ — кнопка под сообщением (_copy_code_keyboard).
    return i18n.t("ios_link.screen", code=f"<code>{code}</code>", ttl=i18n.t("mcp.code_ttl", n=minutes))


def _copy_code_keyboard(code: str) -> InlineKeyboardMarkup:
    """Одна кнопка «Скопировать код» — Bot API copy_text, как у токена MCP."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=i18n.t("btn.copy_code"), copy_text=CopyTextButton(text=code))
        ]]
    )


@router.message(Command("ios"))
async def cmd_ios_link(message: Message):
    await db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.language_code
    )
    code = await mcp_oauth.link_code(message.from_user.id, force_new=True)
    await message.answer(_ios_link_text(code), parse_mode="HTML", reply_markup=_copy_code_keyboard(code))


@router.message(Command("link_app"))
async def cmd_link_app(message: Message, command: CommandObject):
    """Обратное направление: код взят из приложения (app-only аккаунт,
    заведённый через Sign in with Apple без Telegram), а связывает его этот
    Telegram-аккаунт — тот, что написал команду.

    Слияние (db.link_telegram_to_app_account) само решает, что делать с этим
    telegram-аккаунтом — переиспользовать пустую строку или отказаться, если
    у обоих уже есть история; здесь только код, а не бизнес-правило.
    """
    code = (command.args or "").strip()
    if not code:
        await message.answer(i18n.t("ios_link.merge_usage"), parse_mode="HTML")
        return

    status, code_user_id = await db.consume_link_code(
        code,
        # Бот получает не IP, а Telegram id — с ним лимит попыток тот же по
        # смыслу («не дать перебрать шесть цифр»), просто ключ другой.
        client_ip=f"tg:{message.from_user.id}",
        window_seconds=mcp_oauth.CONSENT_FAILURE_WINDOW,
        window_limit_per_ip=mcp_oauth.CONSENT_FAILURE_LIMIT_PER_IP,
        window_limit_total=mcp_oauth.CONSENT_FAILURE_LIMIT_TOTAL,
    )
    if status == "rate_limited":
        await message.answer(i18n.t("ios_link.merge_rate_limited"))
        return
    if status != "ok" or code_user_id is None:
        await message.answer(i18n.t("ios_link.merge_bad_code"))
        return

    result = await db.link_telegram_to_app_account(
        code_user_id, message.from_user.id, username=message.from_user.username
    )
    if result == "both_accounts_have_data":
        await message.answer(i18n.t("ios_link.merge_conflict"))
        return
    if result == "not_app_account":
        # Код был на app-only аккаунт, но тот уже связан (второй раз этим же
        # кодом, или связка кем-то произошла между выдачей и вводом) — для
        # человека это неотличимо от истёкшего кода.
        await message.answer(i18n.t("ios_link.merge_bad_code"))
        return
    await message.answer(i18n.t("ios_link.merge_ok"))
