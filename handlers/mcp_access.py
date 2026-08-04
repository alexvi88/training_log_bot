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


def _server_url() -> str:
    return f"{config.MCP_PUBLIC_URL}{mcp_server.MCP_PATH}"


def _credentials(token: str) -> str:
    """Токен и адрес — то, что нужно подставить в любой клиент. Каждый в своём
    <code>: в Telegram по такому блоку копирование идёт одним тапом, а токен
    руками не перенабрать."""
    return (
        f"🔑 <b>Токен:</b>\n<code>{escape(token)}</code>\n\n"
        f"🌐 <b>Адрес сервера:</b>\n<code>{escape(_server_url())}</code>"
    )


# Инструкции разведены по отдельным экранам не для красоты: вместе они не влезают
# в одно сообщение Telegram (лимит 4096 символов), а резать текст ради влезания
# значит выкинуть ровно те детали, из-за которых подключение и не получается —
# путь к файлу конфига, перезапуск клиента, куда смотреть при проверке.
def _claude_code_guide(token: str) -> str:
    url, tok = escape(_server_url()), escape(token)
    return (
        "<b>Claude Code</b> (терминал)\n\n"
        "Одна команда:\n"
        f"<pre>claude mcp add --transport http -s user training-log \\\n"
        f"  {url} \\\n"
        f'  --header "Authorization: Bearer {tok}"</pre>\n'
        "<code>-s user</code> — чтобы сервер был доступен во всех проектах, "
        "а не только в текущей папке.\n\n"
        "Проверка: запусти <code>claude</code>, набери <code>/mcp</code> — "
        "training-log должен быть <b>connected</b>.\n\n"
        f"{_credentials(token)}"
    )


def _claude_desktop_guide(token: str) -> str:
    url, tok = escape(_server_url()), escape(token)
    return (
        "<b>Claude Desktop</b>\n\n"
        "Приложение ходит в удалённые серверы через мостик <code>mcp-remote</code>, "
        "так что нужен установленный Node.js.\n\n"
        "Открой <b>Настройки → Разработчик → Изменить конфигурацию</b> "
        "(файл <code>claude_desktop_config.json</code>) и впиши:\n"
        f"<pre>{{\n"
        f'  "mcpServers": {{\n'
        f'    "training-log": {{\n'
        f'      "command": "npx",\n'
        f'      "args": ["-y", "mcp-remote",\n'
        f'        "{url}",\n'
        f'        "--header",\n'
        f'        "Authorization: Bearer {tok}"]\n'
        f"    }}\n"
        f"  }}\n"
        f"}}</pre>\n"
        "После правки приложение надо <b>полностью закрыть и открыть заново</b> — "
        "по крестику оно уходит в трей и конфиг не перечитывает.\n\n"
        f"{_credentials(token)}"
    )


def _cursor_guide(token: str) -> str:
    url, tok = escape(_server_url()), escape(token)
    return (
        "<b>Cursor</b>\n\n"
        "Файл <code>~/.cursor/mcp.json</code> (для всех проектов) или "
        "<code>.cursor/mcp.json</code> внутри проекта:\n"
        f"<pre>{{\n"
        f'  "mcpServers": {{\n'
        f'    "training-log": {{\n'
        f'      "url": "{url}",\n'
        f'      "headers": {{\n'
        f'        "Authorization": "Bearer {tok}"\n'
        f"      }}\n"
        f"    }}\n"
        f"  }}\n"
        f"}}</pre>\n"
        "Проверка: <b>Settings → MCP</b>, сервер должен гореть зелёным.\n\n"
        f"{_credentials(token)}"
    )


def _vscode_guide(token: str) -> str:
    url, tok = escape(_server_url()), escape(token)
    return (
        "<b>VS Code</b> (Copilot, режим агента)\n\n"
        "Файл <code>.vscode/mcp.json</code> в проекте. Ключ верхнего уровня — "
        "<code>servers</code>, не <code>mcpServers</code>:\n"
        f"<pre>{{\n"
        f'  "servers": {{\n'
        f'    "training-log": {{\n'
        f'      "type": "http",\n'
        f'      "url": "{url}",\n'
        f'      "headers": {{\n'
        f'        "Authorization": "Bearer {tok}"\n'
        f"      }}\n"
        f"    }}\n"
        f"  }}\n"
        f"}}</pre>\n"
        "Именно <code>.vscode/mcp.json</code>: в корневом <code>.mcp.json</code> "
        "VS Code молча выбрасывает заголовки, и сервер отвечает 401 без объяснений.\n\n"
        f"{_credentials(token)}"
    )


def _generic_guide(token: str) -> str:
    url, tok = escape(_server_url()), escape(token)
    return (
        "<b>Любой другой MCP-клиент</b>\n\n"
        "Всё, что ему нужно знать:\n\n"
        f"• Транспорт: <b>streamable HTTP</b> (не stdio и не SSE)\n"
        f"• URL: <code>{url}</code>\n"
        f"• Заголовок: <code>Authorization: Bearer {tok}</code>\n"
        "• Аутентификация — статический токен, OAuth не нужен\n"
        # Считаем по факту, а не пишем числом: инструмент добавят, а цифру в
        # тексте поправить забудут.
        f"• {len(mcp_server.READ_ONLY_TOOLS)} инструментов, все только на чтение\n\n"
        "Проверить, что сервер жив, можно откуда угодно:\n"
        f"<pre>curl -si -X POST {url} \\\n"
        f'  -H "Content-Type: application/json" \\\n'
        f"  -d '{{}}' | head -1</pre>\n"
        "Без токена ответит <code>401</code> — значит сервер на месте и ждёт "
        "заголовок. С токеном тот же запрос пройдёт дальше."
    )


GUIDES = {
    "claude_code": ("Claude Code", _claude_code_guide),
    "claude_desktop": ("Claude Desktop", _claude_desktop_guide),
    "cursor": ("Cursor", _cursor_guide),
    "vscode": ("VS Code", _vscode_guide),
    "other": ("Другой клиент", _generic_guide),
}


def _screen_text(token_row) -> str:
    if token_row is None:
        return _INTRO + "\n\nНажми «Выдать токен» — покажу его и инструкцию под твой клиент."
    used = token_row["last_used_at"]
    # «Ещё ни разу» — это не мелочь: сразу после настройки клиента по этой
    # строке видно, дошёл запрос или нет, и не надо гадать, где ошибка.
    used_line = (
        f"\n\n🕒 Последнее обращение: {used}"
        if used
        else "\n\n🕒 Ещё ни разу не использовался."
    )
    return (
        _INTRO
        + "\n\n"
        + _credentials(token_row["token"])
        + used_line
        + "\n\n👇 Выбери свой клиент — покажу, куда это вставить."
    )


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


@router.callback_query(F.data.startswith("mcp:how:"))
async def mcp_guide(callback: CallbackQuery, state: FSMContext):
    """Инструкция под конкретный клиент."""
    kind = callback.data.split(":", 2)[2]
    guide = GUIDES.get(kind)
    row = await db.get_mcp_token(callback.from_user.id)
    # Токен могли отозвать с другого устройства, пока этот экран висел открытым:
    # показывать инструкцию с мёртвым токеном — гарантированный «не работает».
    if guide is None or row is None or not config.mcp_available():
        await _show(callback, state)
        return
    await ui.safe_edit(
        callback,
        guide[1](row["token"]),
        reply_markup=keyboards.mcp_guide_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


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
