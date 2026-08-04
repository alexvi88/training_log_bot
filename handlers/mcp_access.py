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

import datetime as dt
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
import timeutil
import ui

router = Router(name="mcp_access")

logger = logging.getLogger(__name__)

_DISABLED_TEXT = (
    "🔌 Подключение по MCP сейчас выключено — бот развёрнут без публичного адреса "
    "для него. Все данные по-прежнему доступны здесь, в боте."
)

_INTRO = (
    "🔌 <b>Свои данные в Claude и ChatGPT</b>\n\n"
    "Подключаешь один раз — и спрашиваешь про свои тренировки прямо там: «что у "
    "меня с жимом за полгода», «собери сплит с учётом моей истории».\n\n"
    "Отдаём <b>только на чтение</b>: тренировки и подходы, прогресс по упражнениям, "
    "объём по группам мышц, вес тела, питание, программы. Переписка с AI-тренером "
    "наружу не уходит, записать что-либо в дневник снаружи нельзя."
)


def _server_url() -> str:
    return f"{config.MCP_PUBLIC_URL}{mcp_server.MCP_PATH}"


def _copyable(value: str) -> str:
    """То, что нужно перенести в другое приложение, — блоком <pre>.

    Именно <pre>, а не инлайновый <code>: у блока кода в Telegram есть кнопка
    копирования, а инлайновый копируется тапом только на телефоне — на десктопе
    его приходится выделять мышью, и адрес с токеном там ловят по буквам.
    """
    return f"<pre>{escape(value)}</pre>"


def _address() -> str:
    return f"🌐 <b>Адрес для коннектора:</b>\n{_copyable(_server_url())}"


def _credentials(token: str, with_address: bool = True) -> str:
    """Токен и адрес — то, что нужно подставить в клиент с заголовком.

    `with_address=False` — когда адрес на этом экране уже показан выше: два
    одинаковых значения под разными подписями («для коннектора» и «сервера»)
    читаются как два разных адреса, которые нельзя перепутать.
    """
    token_block = f"🔑 <b>Токен:</b>\n{_copyable(token)}"
    if not with_address:
        return token_block
    return token_block + f"\n🌐 <b>Адрес сервера:</b>\n{_copyable(_server_url())}"


def _code_ttl() -> str:
    """«5 минут» одним источником: срок задан в mcp_oauth, и разъехаться текст с
    ним не должен — человек поверит тексту."""
    minutes = mcp_oauth.LINK_CODE_TTL_MINUTES
    return f"{minutes} {formatting.plural_ru(minutes, ('минуту', 'минуты', 'минут'))}"


def _when(value: str | None, user=None) -> str:
    """ISO-время из базы в человеческий вид, в часах пользователя.

    Сдвиг обязателен: в базе лежит серверное (UTC) время, а человек сверяет
    «последнее обращение» со своими часами — без сдвига оно на несколько часов в
    прошлом, и выглядит это как «приложение не ходило за данными».

    Секунды не показываем: это строка «когда последний раз читали», а не лог.
    """
    if not value:
        return "ещё ни разу"
    try:
        moment = timeutil.to_user_local(dt.datetime.fromisoformat(value), user)
    except ValueError:  # pragma: no cover — формат пишет db.now_iso
        return value
    return moment.strftime("%d.%m.%Y %H:%M")


# Каждая инструкция — самодостаточный экран: шаги, адрес и код лежат в одном
# сообщении, ровно там, где по ним идёт человек. Ходить за копированием на другой
# экран и возвращаться — самый быстрый способ упустить код: он живёт минуты, а
# уходит время на поиск нужного раздела в приложении.
#
# Инструкции при этом разведены по клиентам, потому что вместе они не влезают в
# одно сообщение (лимит Telegram — 4096 символов), а резать текст ради влезания
# значит выкинуть ровно те детали, из-за которых подключение и не получается.


def _claude_guide(token: str | None, code: str | None) -> str:
    """Браузер и приложение — одна инструкция, потому что путь и правда один.

    Разводить их по экранам было ошибкой: разделы называются одинаково, шаги
    совпадают до последнего, различается только где искать настройки. Два экрана
    с одинаковым текстом человек читает как «я, наверное, открыл не тот».
    """
    return (
        "☁️ <b>Claude</b> — браузер и приложение\n\n"
        "Шаги одни и те же: раздел коннекторов есть и на claude.ai, и в Claude "
        "Desktop, и называется одинаково.\n\n"
        "1. <b>Settings → Connectors</b> (Настройки → Коннекторы)\n"
        "2. <b>Add custom connector</b> (Добавить свой коннектор)\n"
        "3. Вставь адрес:\n"
        f"{_copyable(_server_url())}"
        "4. Claude откроет страницу подтверждения. Введи там код:\n"
        f"{_copyable(code or '')}"
        "5. Нажми <b>«Разрешить»</b> — готово\n\n"
        "Проверка: в новом чате спроси «покажи мои последние тренировки» — Claude "
        "попросит разрешение на вызов инструмента и вернёт данные.\n\n"
        f"Код одноразовый и живёт {_code_ttl()}. Истёк — «🔄 Новый код» ниже.\n\n"
        "Ни Node.js, ни <code>mcp-remote</code> не нужны: приложение подключает "
        "коннекторы само. Мостик остался только для сборок без раздела "
        "коннекторов — если это твой случай, скажи, дам команду."
    )


def _chatgpt_guide(token: str | None, code: str | None) -> str:
    return (
        "🤖 <b>ChatGPT</b>\n\n"
        "Коннекторы у ChatGPT спрятаны за режимом разработчика.\n\n"
        "1. <b>Settings → Connectors → Advanced</b> → включи <b>Developer mode</b>\n"
        "2. Вернись в <b>Connectors</b> → <b>Create</b>\n"
        "3. <b>MCP Server URL</b>:\n"
        f"{_copyable(_server_url())}"
        "4. <b>Authentication</b> — <b>OAuth</b>, дальше <b>Create</b>\n"
        "5. На странице подтверждения введи код:\n"
        f"{_copyable(code or '')}"
        "6. Нажми <b>«Разрешить»</b> — готово\n\n"
        "Проверка: в чате включи коннектор через «+» и спроси «покажи мои последние "
        "тренировки».\n\n"
        "Названия пунктов OpenAI периодически меняет — ищи по словам "
        "<i>Connectors</i> и <i>Developer mode</i>.\n\n"
        f"Код одноразовый и живёт {_code_ttl()}. Истёк — «🔄 Новый код» ниже."
    )


def _claude_code_guide(token: str | None, code: str | None) -> str:
    """Единственная инструкция на токене: в терминале он короче любого OAuth."""
    return (
        "🖥 <b>Claude Code</b> (терминал)\n\n"
        "Одна команда — скопируй и вставь целиком:\n"
        + _copyable(
            "claude mcp add --transport http -s user training-log \\\n"
            f"  {_server_url()} \\\n"
            f'  --header "Authorization: Bearer {token or ""}"'
        )
        + "<code>-s user</code> — чтобы сервер был доступен во всех проектах, а не "
        "только в текущей папке.\n\n"
        "Проверка: запусти <code>claude</code>, набери <code>/mcp</code> — "
        "training-log должен быть <b>connected</b>.\n\n"
        "Токен и адрес по отдельности, если нужны в другой клиент:\n"
        f"{_credentials(token or '')}"
    )


GUIDES = {
    "claude": ("Claude", _claude_guide),
    "chatgpt": ("ChatGPT", _chatgpt_guide),
    "claude_code": ("Claude Code", _claude_code_guide),
}

# Инструкции, которым токен не нужен: они целиком про коннектор. Показывать их
# можно всегда — в отличие от остальных, где без токена нечего вставлять.
OAUTH_GUIDES = frozenset({kind for kind, _ in keyboards.MCP_OAUTH_CLIENTS})


def _connections_block(connections: list, user) -> str:
    if not connections:
        return ""
    lines = ["🔌 <b>Подключено:</b>"]
    for row in connections:
        name = mcp_oauth.client_display_name(row["metadata"], row["client_id"])
        # Подпись обязательна: без неё дата читается как «когда подключено», и
        # человек, зашедший проверить, ходил ли кто-то за данными, делает
        # противоположный вывод.
        lines.append(
            f"• {escape(name)} — последний запрос: {_when(row['last_used_at'], user)}"
        )
    return "\n".join(lines)


def _screen_text(token_row, connections: list, user=None) -> str:
    blocks = [_INTRO, _address()]
    connected = _connections_block(connections, user)
    if connected:
        blocks.append(connected)
    if token_row is None:
        blocks.append(
            "Клиентам из терминала — Claude Code и любому другому, куда заголовок "
            "вписывают руками, — вместо кода нужен токен. Выдать его можно тут же."
        )
    else:
        used = token_row["last_used_at"]
        # «Ещё ни разу» — это не мелочь: сразу после настройки клиента по этой
        # строке видно, дошёл запрос или нет, и не надо гадать, где ошибка.
        blocks.append(
            _credentials(token_row["token"], with_address=False)
            + f"\n🕒 Последний запрос по токену: {_when(used, user)}"
        )
    # Последняя строка перед кнопками — про то, что кнопка ведёт не в очередной
    # список, а сразу ко всему нужному: человек, которого один раз погоняли между
    # экранами, второй раз кнопку не нажмёт.
    blocks.append("👇 Выбери приложение — там пошагово, вместе с адресом и кодом.")
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
    # Профиль нужен ровно за таймзоной: даты в базе серверные, а сверяет их
    # человек со своими часами.
    text = _screen_text(row, connections, await db.get_user(user.id))
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
    """Инструкция под конкретный клиент — со всем, что нужно скопировать.

    Код связывания показывается здесь же, на открытии экрана: он нужен на
    четвёртом шаге этой самой инструкции, и посылать за ним на другой экран значит
    гонять человека туда-обратно ровно в тот момент, когда у него на втором
    мониторе открыта страница подтверждения.

    Действующий код при этом переиспользуется, а не выдаётся заново: человек мог
    его уже скопировать и вернуться перечитать шаг — новый код в этот момент убил
    бы тот, что у него в браузере. Сменить код можно кнопкой «🔄 Новый код», и
    только ей.
    """
    parts = callback.data.split(":")
    kind = parts[2]
    # «🔄 Новый код» — тот же экран, но с явным требованием сменить код.
    force_new = len(parts) > 3 and parts[3] == "new"
    guide = GUIDES.get(kind)
    if guide is None or not config.mcp_available():
        await _show(callback, state)
        return
    token, code = None, None
    if kind in OAUTH_GUIDES:
        code = await mcp_oauth.link_code(callback.from_user.id, force_new=force_new)
        logger.info("MCP OAuth: link code shown to user %s (new=%s)", callback.from_user.id, force_new)
    else:
        row = await db.get_mcp_token(callback.from_user.id)
        # Токен могли отозвать с другого устройства, пока этот экран висел
        # открытым: инструкция с мёртвым токеном — гарантированный «не работает».
        if row is None:
            await _show(callback, state)
            return
        token = row["token"]
    await ui.safe_edit(
        callback,
        guide[1](token, code),
        reply_markup=keyboards.mcp_guide_keyboard(kind if code else None),
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
    code = await mcp_oauth.link_code(callback.from_user.id, force_new=True)
    logger.info("MCP OAuth: link code issued for user %s", callback.from_user.id)
    await ui.safe_edit(
        callback,
        "🔗 <b>Код для подключения</b>\n\n"
        f"{_copyable(code)}\n"
        "Введи его на странице подтверждения, которую откроет приложение. "
        f"Код одноразовый и действует {_code_ttl()} — не успел, жми «Новый код».\n\n"
        "Если страница ещё не открыта: добавь в приложении коннектор по адресу\n"
        f"{_copyable(_server_url())}"
        "— оно само предложит подтвердить доступ. Пошагово — на экранах "
        "«Claude в браузере», «ChatGPT» и «Claude Desktop».",
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
    user = await db.get_user(callback.from_user.id)
    buttons = []
    for row in connections:
        name = mcp_oauth.client_display_name(row["metadata"], row["client_id"])
        lines.append(
            f"• <b>{escape(name)}</b>\n"
            f"   подключено: {_when(row['connected_at'], user)}\n"
            f"   последнее обращение: {_when(row['last_used_at'], user)}"
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
