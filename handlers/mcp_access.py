"""🔌 Подключение своих данных к внешнему AI-клиенту по MCP.

Экран открывается командой /mcp и из настроек. Всё, что он делает — выдаёт,
показывает и отзывает bearer-токен к `mcp_server.py`: сам сервер живёт рядом с
поллингом в том же процессе (см. main.py), а токен — единственное, что
связывает запрос снаружи с конкретным пользователем.

Токен показывается открытым текстом в чате. Иначе никак: его надо скопировать в
конфиг клиента, а второго канала связи с человеком у бота нет. Поэтому и
перевыпуск с отзывом лежат на том же экране, в один тап — «случайно переслал
скриншот» лечится ровно этим.
"""

import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import db
import keyboards
import mcp_server
import ui

router = Router(name="mcp_access")

logger = logging.getLogger(__name__)

_DISABLED_TEXT = (
    "🔌 Подключение по MCP сейчас выключено — бот развёрнут без публичного адреса "
    "для него. Все данные по-прежнему доступны здесь, в боте."
)

_INTRO = (
    "🔌 <b>Свои данные в Claude и других AI-клиентах</b>\n\n"
    "Бот умеет отдавать твою историю тренировок по протоколу MCP: подключаешь его "
    "один раз в своём AI-клиенте и дальше спрашиваешь про тренировки прямо там — "
    "«что у меня с жимом за полгода», «собери сплит с учётом моей истории».\n\n"
    "Отдаём <b>только на чтение</b>: тренировки и подходы, прогресс по упражнениям, "
    "недельный объём по группам мышц, вес тела, дневник питания, программы. "
    "Переписка с AI-тренером наружу не уходит, и записать что-либо в дневник "
    "снаружи нельзя.\n\n"
    "Токен — это ключ ко всему перечисленному. Никому его не показывай; если "
    "утёк — перевыпусти, старый сразу перестанет работать."
)


def _config_snippet(token: str) -> str:
    url = f"{config.MCP_PUBLIC_URL}{mcp_server.MCP_PATH}"
    return (
        f"🔑 <b>Твой токен:</b>\n<code>{escape(token)}</code>\n\n"
        f"🌐 <b>Адрес сервера:</b>\n<code>{escape(url)}</code>\n\n"
        "<b>Claude Code</b> — одной командой в терминале:\n"
        f"<pre>claude mcp add --transport http training-log \\\n"
        f"  {escape(url)} \\\n"
        f'  --header "Authorization: Bearer {escape(token)}"</pre>\n'
        "<b>Claude Desktop и другие клиенты</b> — HTTP-сервер с заголовком:\n"
        f"<pre>{{\n"
        f'  "type": "http",\n'
        f'  "url": "{escape(url)}",\n'
        f'  "headers": {{"Authorization": "Bearer {escape(token)}"}}\n'
        f"}}</pre>"
    )


def _screen_text(token_row) -> str:
    if token_row is None:
        return _INTRO + "\n\nНажми «Выдать токен» — покажу его и готовый конфиг."
    used = token_row["last_used_at"]
    # «Ещё ни разу» — это не мелочь: сразу после настройки клиента по этой
    # строке видно, дошёл запрос или нет, и не надо гадать, где ошибка.
    used_line = (
        f"\n\n🕒 Последнее обращение: {used}"
        if used
        else "\n\n🕒 Ещё ни разу не использовался."
    )
    return _INTRO + "\n\n" + _config_snippet(token_row["token"]) + used_line


async def _show(target, state: FSMContext, alert: str | None = None) -> None:
    """target — Message (команда) или CallbackQuery (кнопка)."""
    user = target.from_user
    is_callback = isinstance(target, CallbackQuery)
    if not config.mcp_available():
        if is_callback:
            await ui.safe_edit(target, _DISABLED_TEXT, reply_markup=keyboards.mcp_keyboard(False))
            await target.answer()
        else:
            await target.answer(_DISABLED_TEXT, reply_markup=keyboards.mcp_keyboard(False))
        return
    await state.clear()
    row = await db.get_mcp_token(user.id)
    text = _screen_text(row)
    kb = keyboards.mcp_keyboard(row is not None)
    if is_callback:
        await ui.safe_edit(target, text, reply_markup=kb, parse_mode="HTML")
        await target.answer(alert, show_alert=bool(alert))
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("mcp"))
async def cmd_mcp(message: Message, state: FSMContext):
    await db.get_or_create_user(message.from_user.id, message.from_user.username)
    await _show(message, state)


@router.callback_query(F.data.in_({"mcp:open", "settings:mcp"}))
async def mcp_open(callback: CallbackQuery, state: FSMContext):
    await _show(callback, state)


@router.callback_query(F.data == "mcp:issue")
async def mcp_issue(callback: CallbackQuery, state: FSMContext):
    """Выдать токен — он же перевыпуск: db.issue_mcp_token гасит прежний."""
    if not config.mcp_available():
        await callback.answer("Подключение по MCP выключено.", show_alert=True)
        return
    had_token = await db.get_mcp_token(callback.from_user.id) is not None
    await db.issue_mcp_token(callback.from_user.id)
    logger.info("MCP token issued for user %s (reissue=%s)", callback.from_user.id, had_token)
    await _show(
        callback,
        state,
        alert="Готово. Прежний токен больше не работает." if had_token else None,
    )


@router.callback_query(F.data == "mcp:revoke")
async def mcp_revoke(callback: CallbackQuery, state: FSMContext):
    revoked = await db.revoke_mcp_token(callback.from_user.id)
    if revoked:
        logger.info("MCP token revoked for user %s", callback.from_user.id)
    await _show(
        callback,
        state,
        alert="Токен отозван — доступ снаружи закрыт." if revoked else None,
    )
