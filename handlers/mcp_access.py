"""🔌 Подключение своих данных к внешнему AI-клиенту по MCP.

Экран открывается командой /mcp и из настроек. Он ведёт два способа доступа к
одним и тем же данным:

* коннектор по OAuth — вставить адрес и подтвердить шестизначным кодом из бота;
  так подключаются claude.ai, Claude Desktop и ChatGPT, и ничего копировать в
  файлы не нужно;
* статический bearer-токен — для клиентов, где заголовок вписывают руками
  (Claude Code, Cursor, VS Code).

Первым идёт коннектор: он доступен всем и сразу, а токен нужен меньшинству.

Токен показывается открытым текстом в чате. Иначе никак: его надо скопировать в
конфиг клиента, а второго канала связи с человеком у бота нет. Поэтому и
перевыпуск с отзывом лежат на том же экране, в один тап — «случайно переслал
скриншот» лечится ровно этим. Код связывания живёт минуты и гасится при первом
использовании, поэтому его цена в чате куда ниже.
"""

import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import db
import formatting
import keyboards
import mcp_oauth
import mcp_server
import ui

router = Router(name="mcp_access")

logger = logging.getLogger(__name__)

_DISABLED_TEXT = (
    "🔌 Подключение по MCP сейчас выключено — бот развёрнут без публичного адреса "
    "для него. Все данные по-прежнему доступны здесь, в боте."
)

_INTRO = (
    "🔌 <b>Свои данные в Claude и ChatGPT</b>\n\n"
    "Бот умеет отдавать твою историю тренировок по протоколу MCP: подключаешь его "
    "один раз и дальше спрашиваешь про тренировки прямо там — «что у меня с жимом "
    "за полгода», «собери сплит с учётом моей истории».\n\n"
    "<b>Как подключить:</b> в приложении добавь коннектор по адресу ниже, "
    "оно откроет страницу подтверждения — введи там код из бота, и всё.\n\n"
    "Отдаём <b>только на чтение</b>: тренировки и подходы, прогресс по упражнениям, "
    "недельный объём по группам мышц, вес тела, дневник питания, программы. "
    "Переписка с AI-тренером наружу не уходит, и записать что-либо в дневник "
    "снаружи нельзя."
)


def _server_url() -> str:
    return f"{config.MCP_PUBLIC_URL}{mcp_server.MCP_PATH}"


def _address() -> str:
    """Адрес сервера отдельным <code>: по такому блоку в Telegram копирование
    идёт одним тапом, а адрес руками не перенабрать."""
    return f"🌐 <b>Адрес для коннектора:</b>\n<code>{escape(_server_url())}</code>"


def _credentials(token: str) -> str:
    """Токен и адрес — то, что нужно подставить в клиент с заголовком."""
    return (
        f"🔑 <b>Токен:</b>\n<code>{escape(token)}</code>\n\n"
        f"🌐 <b>Адрес сервера:</b>\n<code>{escape(_server_url())}</code>"
    )


def _code_ttl() -> str:
    """«5 минут» одним источником: срок задан в mcp_oauth, и разъехаться текст с
    ним не должен — человек поверит тексту."""
    minutes = mcp_oauth.LINK_CODE_TTL_MINUTES
    return f"{minutes} {formatting.plural_ru(minutes, ('минуту', 'минуты', 'минут'))}"


def _when(value: str | None) -> str:
    """ISO-время из базы в человеческий вид. Секунды не нужны: это строка «когда
    последний раз ходили за данными», а не отладочный лог."""
    if not value:
        return "ещё ни разу"
    date, _, clock = value.partition("T")
    parts = date.split("-")
    if len(parts) != 3:  # pragma: no cover — формат пишет db.now_iso
        return value
    return f"{parts[2]}.{parts[1]}.{parts[0]} {clock[:5]}".strip()


# Инструкции разведены по отдельным экранам не для красоты: вместе они не влезают
# в одно сообщение Telegram (лимит 4096 символов), а резать текст ради влезания
# значит выкинуть ровно те детали, из-за которых подключение и не получается —
# где лежит нужная настройка, что перезапустить, куда смотреть при проверке.


def _claude_web_guide(token: str | None) -> str:
    return (
        "<b>Claude в браузере</b> (claude.ai)\n\n"
        "Ни файлов, ни терминала — коннектор добавляется в настройках.\n\n"
        "1. claude.ai → <b>Settings → Connectors</b> (Настройки → Коннекторы)\n"
        "2. <b>Add custom connector</b> (Добавить свой коннектор)\n"
        "3. Вставь адрес и нажми «Добавить»\n"
        "4. Claude откроет страницу подтверждения\n"
        "5. Вернись в бота, нажми <b>«🔗 Код для подключения»</b>, введи шесть цифр "
        "на странице и нажми «Разрешить»\n\n"
        "Проверка: в новом чате спроси «покажи мои последние тренировки» — Claude "
        "спросит разрешение на вызов инструмента и вернёт данные.\n\n"
        f"Код одноразовый и живёт {_code_ttl()}: не успел — жми кнопку в боте "
        "ещё раз.\n\n"
        f"{_address()}"
    )


def _chatgpt_guide(token: str | None) -> str:
    return (
        "<b>ChatGPT</b>\n\n"
        "MCP-серверы ChatGPT подключает как коннекторы; раздел спрятан за режимом "
        "разработчика.\n\n"
        "1. <b>Settings → Connectors → Advanced</b> → включи <b>Developer mode</b>\n"
        "2. Вернись в <b>Connectors</b> → <b>Create</b> (Создать)\n"
        "3. <b>MCP Server URL</b> — адрес ниже\n"
        "4. <b>Authentication</b> — <b>OAuth</b>\n"
        "5. Откроется страница подтверждения: введи код из бота "
        "(кнопка «🔗 Код для подключения») и нажми «Разрешить»\n\n"
        "Проверка: в чате включи коннектор через «+» и спроси «покажи мои последние "
        "тренировки».\n\n"
        "Названия пунктов OpenAI периодически меняет — ищи по словам "
        "<i>Connectors</i> и <i>Developer mode</i>.\n\n"
        f"{_address()}"
    )


def _claude_desktop_guide(token: str | None) -> str:
    tail = (
        "Старым версиям без раздела коннекторов остаётся мостик "
        "<code>mcp-remote</code> (нужен Node.js): в "
        "<code>claude_desktop_config.json</code> прописать "
        f'<code>npx -y mcp-remote {escape(_server_url())} --header '
        f'"Authorization: Bearer {escape(token)}"</code>.'
        if token
        else "Старым версиям без раздела коннекторов остаётся мостик "
        "<code>mcp-remote</code> (нужен Node.js и токен — выдай его на этом же "
        "экране кнопкой «Выдать токен для терминала»)."
    )
    return (
        "<b>Claude Desktop</b>\n\n"
        "Приложение подключает коннекторы нативно — ни Node.js, ни "
        "<code>mcp-remote</code> больше не нужны.\n\n"
        "1. <b>Настройки → Коннекторы</b> (Settings → Connectors)\n"
        "2. <b>Добавить свой коннектор</b> (Add custom connector)\n"
        "3. Вставь адрес ниже\n"
        "4. На открывшейся странице введи код из бота "
        "(кнопка «🔗 Код для подключения») и нажми «Разрешить»\n\n"
        "Проверка: в чате появится значок инструментов — спроси «покажи мои "
        "последние тренировки».\n\n"
        f"{_address()}\n\n"
        f"{tail}"
    )


def _claude_code_guide(token: str | None) -> str:
    url, tok = escape(_server_url()), escape(token or "")
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
        f"{_credentials(token or '')}"
    )


def _cursor_guide(token: str | None) -> str:
    url, tok = escape(_server_url()), escape(token or "")
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
        f"{_credentials(token or '')}"
    )


def _vscode_guide(token: str | None) -> str:
    url, tok = escape(_server_url()), escape(token or "")
    return (
        "<b>VS Code</b> (Copilot, режим агента)\n\n"
        "Без открытого проекта: палитра команд (<code>Ctrl/Cmd+Shift+P</code>) → "
        "<b>MCP: Add Server</b> → <b>HTTP</b> → адрес ниже → <b>User settings</b> — "
        "сервер будет доступен во всех папках.\n\n"
        "Внутри проекта — файл <code>.vscode/mcp.json</code>. Ключ верхнего "
        "уровня — <code>servers</code>, не <code>mcpServers</code>:\n"
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
        f"{_credentials(token or '')}"
    )


def _generic_guide(token: str | None) -> str:
    url, tok = escape(_server_url()), escape(token or "")
    return (
        "<b>Любой другой MCP-клиент</b>\n\n"
        "Всё, что ему нужно знать:\n\n"
        f"• Транспорт: <b>streamable HTTP</b> (не stdio и не SSE)\n"
        f"• URL: <code>{url}</code>\n"
        "• Аутентификация — на выбор: <b>OAuth</b> (динамическая регистрация, "
        "PKCE, подтверждение кодом из бота) или статический токен в заголовке\n"
        f"• Заголовок для второго случая: <code>Authorization: Bearer {tok}</code>\n"
        # Считаем по факту, а не пишем числом: инструмент добавят, а цифру в
        # тексте поправить забудут.
        f"• {len(mcp_server.READ_ONLY_TOOLS)} инструментов, все только на чтение\n\n"
        "Метаданные OAuth клиент найдёт сам:\n"
        f"<pre>{escape(config.MCP_PUBLIC_URL)}/.well-known/oauth-authorization-server</pre>\n"
        "Проверить, что сервер жив, можно откуда угодно:\n"
        f"<pre>curl -si -X POST {url} \\\n"
        f'  -H "Content-Type: application/json" \\\n'
        f"  -d '{{}}' | head -1</pre>\n"
        "Без токена ответит <code>401</code> — значит сервер на месте и ждёт "
        "авторизацию."
    )


GUIDES = {
    "claude_web": ("Claude в браузере", _claude_web_guide),
    "chatgpt": ("ChatGPT", _chatgpt_guide),
    "claude_desktop": ("Claude Desktop", _claude_desktop_guide),
    "claude_code": ("Claude Code", _claude_code_guide),
    "cursor": ("Cursor", _cursor_guide),
    "vscode": ("VS Code", _vscode_guide),
    "other": ("Другой клиент", _generic_guide),
}

# Инструкции, которым токен не нужен: они целиком про коннектор. Показывать их
# можно всегда — в отличие от остальных, где без токена нечего вставлять.
OAUTH_GUIDES = frozenset({kind for kind, _ in keyboards.MCP_OAUTH_CLIENTS})


def _connections_block(connections: list) -> str:
    if not connections:
        return ""
    lines = ["🔌 <b>Подключено:</b>"]
    for row in connections:
        name = mcp_oauth.client_display_name(row["metadata"], row["client_id"])
        lines.append(f"• {escape(name)} — {_when(row['last_used_at'])}")
    return "\n".join(lines)


def _screen_text(token_row, connections: list) -> str:
    blocks = [_INTRO, _address()]
    connected = _connections_block(connections)
    if connected:
        blocks.append(connected)
    if token_row is None:
        blocks.append(
            "Для Claude Code, Cursor и VS Code вместо кода нужен токен — "
            "выдать его можно тут же, кнопкой ниже."
        )
    else:
        used = token_row["last_used_at"]
        # «Ещё ни разу» — это не мелочь: сразу после настройки клиента по этой
        # строке видно, дошёл запрос или нет, и не надо гадать, где ошибка.
        blocks.append(
            _credentials(token_row["token"])
            + f"\n\n🕒 Последнее обращение по токену: {_when(used)}"
        )
    blocks.append("👇 Выбери свой клиент — покажу, что где нажать.")
    return "\n\n".join(blocks)


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
    connections = await db.list_oauth_connections(user.id)
    text = _screen_text(row, connections)
    kb = keyboards.mcp_keyboard(row is not None, bool(connections))
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
    if guide is None or not config.mcp_available():
        await _show(callback, state)
        return
    token = None
    if kind not in OAUTH_GUIDES:
        row = await db.get_mcp_token(callback.from_user.id)
        # Токен могли отозвать с другого устройства, пока этот экран висел
        # открытым: инструкция с мёртвым токеном — гарантированный «не работает».
        if row is None:
            await _show(callback, state)
            return
        token = row["token"]
    await ui.safe_edit(
        callback,
        guide[1](token),
        reply_markup=keyboards.mcp_guide_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "mcp:code")
async def mcp_code(callback: CallbackQuery, state: FSMContext):
    """Код связывания: им человек доказывает странице согласия, что это он."""
    if not config.mcp_available():
        await callback.answer("Подключение по MCP выключено.", show_alert=True)
        return
    await state.clear()
    code = await mcp_oauth.issue_link_code(callback.from_user.id)
    logger.info("MCP OAuth: link code issued for user %s", callback.from_user.id)
    await ui.safe_edit(
        callback,
        "🔗 <b>Код для подключения</b>\n\n"
        f"<code>{code}</code>\n\n"
        "Введи его на странице подтверждения, которую откроет приложение. "
        f"Код одноразовый и действует {_code_ttl()} — не успел, жми «Новый код».\n\n"
        "Если страница ещё не открыта: добавь в приложении коннектор по адресу\n"
        f"<code>{escape(_server_url())}</code>\n"
        "— оно само предложит подтвердить доступ.",
        reply_markup=keyboards.mcp_code_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "mcp:apps")
async def mcp_apps(callback: CallbackQuery, state: FSMContext):
    """Подключённые приложения и отзыв по каждому.

    Кнопка «Перевыпустить токен» на них не влияет — это разные механизмы, и
    делать вид, что один отзыв закрывает всё, нельзя.
    """
    if not config.mcp_available():
        await callback.answer("Подключение по MCP выключено.", show_alert=True)
        return
    await state.clear()
    connections = await db.list_oauth_connections(callback.from_user.id)
    if not connections:
        await _show(callback, state, alert="Подключённых приложений нет.")
        return
    lines = ["🔌 <b>Подключённые приложения</b>\n"]
    buttons = []
    for row in connections:
        name = mcp_oauth.client_display_name(row["metadata"], row["client_id"])
        lines.append(
            f"• <b>{escape(name)}</b>\n"
            f"   подключено: {_when(row['connected_at'])}\n"
            f"   последнее обращение: {_when(row['last_used_at'])}"
        )
        buttons.append((row["client_id"][:48], name))
    lines.append(
        "\n«Отключить» гасит доступ приложения сразу и целиком. Подключиться "
        "заново оно сможет — с новым кодом из бота."
    )
    await ui.safe_edit(
        callback,
        "\n".join(lines),
        reply_markup=keyboards.mcp_apps_keyboard(buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mcp:off:"))
async def mcp_disconnect(callback: CallbackQuery, state: FSMContext):
    """Отключить приложение: гасим все его пары токенов у этого пользователя.

    В callback_data client_id может не влезть целиком (64 байта на всё), поэтому
    сверяемся по началу со списком подключений самого пользователя — чужой id
    так не подставить.
    """
    prefix = callback.data.split(":", 2)[2]
    connections = await db.list_oauth_connections(callback.from_user.id)
    client_id = next((r["client_id"] for r in connections if r["client_id"].startswith(prefix)), None)
    if client_id is None:
        await _show(callback, state, alert="Это приложение уже отключено.")
        return
    revoked = await db.revoke_oauth_client_tokens(callback.from_user.id, client_id)
    logger.info(
        "MCP OAuth: user %s disconnected client %s (%s tokens)",
        callback.from_user.id,
        client_id,
        revoked,
    )
    if await db.list_oauth_connections(callback.from_user.id):
        # Осталось что отключать — человек остаётся в списке, а не выкидывается
        # на главный экран после каждой кнопки.
        await mcp_apps(callback, state)
    else:
        await _show(callback, state, alert="Приложение отключено.")


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
