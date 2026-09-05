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
import i18n
import keyboards
import mcp_oauth
import mcp_server
import state_scaffold
import timeutil
import ui

router = Router(name="mcp_access")

logger = logging.getLogger(__name__)


def _server_url() -> str:
    return f"{config.MCP_PUBLIC_URL}{mcp_server.MCP_PATH}"


def _copyable(value: str) -> str:
    """То, что нужно перенести в другое приложение, — блоком <pre>.

    Именно <pre>, а не инлайновый <code>: у блока кода в Telegram есть кнопка
    копирования, а инлайновый копируется тапом только на телефоне — на десктопе
    его приходится выделять мышью, и адрес с токеном там ловят по буквам.
    """
    return f"<pre>{escape(value)}</pre>"


def _credentials(token: str, with_address: bool = True) -> str:
    """Токен и адрес — то, что нужно подставить в клиент с заголовком.

    `with_address=False` — когда адрес на этом экране уже показан выше: два
    одинаковых значения под разными подписями («для коннектора» и «сервера»)
    читаются как два разных адреса, которые нельзя перепутать.
    """
    token_block = f"🔑 <b>{i18n.t('mcp.token_label')}</b>\n{_copyable(token)}"
    if not with_address:
        return token_block
    return token_block + f"\n🌐 <b>{i18n.t('mcp.server_address_label')}</b>\n{_copyable(_server_url())}"


def _code_ttl() -> str:
    """«5 минут» одним источником: срок задан в mcp_oauth, и разъехаться текст с
    ним не должен — человек поверит тексту."""
    minutes = mcp_oauth.LINK_CODE_TTL_MINUTES
    return i18n.t("mcp.code_ttl", n=minutes)


def _when(value: str | None, user=None) -> str:
    """ISO-время из базы в человеческий вид, в часах пользователя.

    Сдвиг обязателен: в базе лежит серверное (UTC) время, а человек сверяет
    «последнее обращение» со своими часами — без сдвига оно на несколько часов в
    прошлом, и выглядит это как «приложение не ходило за данными».

    Секунды не показываем: это строка «когда последний раз читали», а не лог.
    """
    if not value:
        return i18n.t("mcp.never")
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
    return i18n.t(
        "mcp.guide.claude",
        address=_copyable(_server_url()), code=_copyable(code or ""), ttl=_code_ttl(),
    )


def _chatgpt_guide(token: str | None, code: str | None) -> str:
    return i18n.t(
        "mcp.guide.chatgpt",
        address=_copyable(_server_url()), code=_copyable(code or ""), ttl=_code_ttl(),
    )


def _claude_code_guide(token: str | None, code: str | None) -> str:
    """И в терминале токен не обязателен: Claude Code умеет OAuth сам.

    Раньше здесь была команда с заголовком, будто иначе нельзя. Можно: `claude
    mcp add` без заголовка, дальше `/mcp` → Authenticate, браузер, наша страница
    согласия — и Claude Code сам хранит выданные токены и обновляет их по
    refresh. Токен остаётся ровно для того, где браузера рядом нет: облачные
    сессии, скрипты, curl.
    """
    tail = (
        i18n.t(
            "mcp.guide.claude_code.tail_with_token",
            command=_copyable(
                f'claude mcp add --transport http -s user training-log {_server_url()} '
                f'--header "Authorization: Bearer {token}"'
            ),
        )
        if token
        else i18n.t("mcp.guide.claude_code.tail_no_token")
    )
    return i18n.t(
        "mcp.guide.claude_code",
        add_command=_copyable(f"claude mcp add --transport http -s user training-log {_server_url()}"),
        code=_copyable(code or ""),
        tail=tail,
    )


GUIDES = {
    "claude": ("Claude", _claude_guide),
    "chatgpt": ("ChatGPT", _chatgpt_guide),
    "claude_code": ("Claude Code", _claude_code_guide),
}

# Токен не требуется ни одной инструкции: коннектор по OAuth умеют все три
# клиента, включая терминальный. Поэтому и код связывания показывается на каждой.
OAUTH_GUIDES = frozenset(GUIDES)


def _connections_block(connections: list, user) -> str:
    if not connections:
        return ""
    lines = [f"🔌 <b>{i18n.t('mcp.connected_title')}</b>"]
    for row in connections:
        name = mcp_oauth.client_display_name(row["metadata"], row["client_id"])
        # Подпись обязательна: без неё дата читается как «когда подключено», и
        # человек, зашедший проверить, ходил ли кто-то за данными, делает
        # противоположный вывод.
        lines.append(
            i18n.t("mcp.connected_line", name=escape(name), when=_when(row["last_used_at"], user))
        )
    return "\n".join(lines)


def _screen_text(token_row, connections: list, user=None) -> str:
    # Адреса здесь нет нарочно: он есть в каждой инструкции, внутри того шага,
    # где его вставляют. На этом экране с ним делать нечего, а места он занимает
    # больше всех — и вместе с токеном превращал экран в свалку значений.
    blocks = [i18n.t("mcp.intro")]
    connected = _connections_block(connections, user)
    if connected:
        blocks.append(connected)
    if token_row is None:
        blocks.append(i18n.t("mcp.no_token_hint"))
    else:
        used = token_row["last_used_at"]
        # «Ещё ни разу» — это не мелочь: сразу после настройки клиента по этой
        # строке видно, дошёл запрос или нет, и не надо гадать, где ошибка.
        blocks.append(
            _credentials(token_row["token"], with_address=False)
            + "\n"
            + i18n.t("mcp.token_last_used", when=_when(used, user))
        )
    # Последняя строка перед кнопками — про то, что кнопка ведёт не в очередной
    # список, а сразу ко всему нужному: человек, которого один раз погоняли между
    # экранами, второй раз кнопку не нажмёт.
    blocks.append(i18n.t("mcp.pick_app_hint"))
    return "\n\n".join(blocks)


async def _show(target, state: FSMContext, alert: str | None = None) -> None:
    """target — Message (команда) или CallbackQuery (кнопка)."""
    user = target.from_user
    is_callback = isinstance(target, CallbackQuery)
    if not config.mcp_available():
        disabled_text = i18n.t("mcp.disabled")
        if is_callback:
            await ui.safe_edit(target, disabled_text, reply_markup=keyboards.mcp_keyboard(False))
            await target.answer()
        else:
            await target.answer(disabled_text, reply_markup=keyboards.mcp_keyboard(False))
        return
    # /mcp долетает из любого состояния (роутер подключён раньше workout.router),
    # в том числе из середины тренировки: снимаем поток, но не её каркас.
    await state_scaffold.clear_state_keep_workout(state)
    row = await db.get_mcp_token(user.id)
    connections = await db.list_oauth_connections(user.id)
    # Профиль нужен ровно за таймзоной: даты в базе серверные, а сверяет их
    # человек со своими часами.
    text = _screen_text(row, connections, await db.get_user(user.id))
    kb = keyboards.mcp_keyboard(row is not None, bool(connections), token=row["token"] if row else None)
    if is_callback:
        await ui.safe_edit(target, text, reply_markup=kb, parse_mode="HTML")
        await target.answer(alert, show_alert=bool(alert))
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("mcp"))
async def cmd_mcp(message: Message, state: FSMContext):
    await db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.language_code
    )
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
    code = None
    if kind in OAUTH_GUIDES:
        code = await mcp_oauth.link_code(callback.from_user.id, force_new=force_new)
        logger.info(
            "MCP OAuth: link code shown to user %s (new=%s)", callback.from_user.id, force_new
        )
    # Токен — по наличию, а не по требованию: ни одна инструкция без него уже не
    # ломается, он нужен только в хвосте про скрипты и облачные сессии.
    row = await db.get_mcp_token(callback.from_user.id)
    token = row["token"] if row else None
    # Адрес отдельной кнопкой — только там, где он показан отдельным блоком, а не
    # спрятан внутри команды для терминала: у Claude Code адрес есть только
    # внутри `claude mcp add ...` (см. keyboards.mcp_guide_keyboard).
    address = _server_url() if kind != "claude_code" else None
    await ui.safe_edit(
        callback,
        guide[1](token, code),
        reply_markup=keyboards.mcp_guide_keyboard(kind if code else None, address=address, code=code),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "mcp:code")
async def mcp_code(callback: CallbackQuery, state: FSMContext):
    """Код связывания: им человек доказывает странице согласия, что это он."""
    if not config.mcp_available():
        await callback.answer(i18n.t("mcp.disabled_alert"), show_alert=True)
        return
    # Тот же расчёт, что в _show: экран кода — не повод забыть открытую тренировку.
    await state_scaffold.clear_state_keep_workout(state)
    code = await mcp_oauth.link_code(callback.from_user.id, force_new=True)
    logger.info("MCP OAuth: link code issued for user %s", callback.from_user.id)
    await ui.safe_edit(
        callback,
        i18n.t(
            "mcp.code_screen",
            code=_copyable(code), ttl=_code_ttl(), address=_copyable(_server_url()),
        ),
        reply_markup=keyboards.mcp_code_keyboard(address=_server_url(), code=code),
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
        await callback.answer(i18n.t("mcp.disabled_alert"), show_alert=True)
        return
    # Тот же расчёт, что в _show.
    await state_scaffold.clear_state_keep_workout(state)
    connections = await db.list_oauth_connections(callback.from_user.id)
    if not connections:
        await _show(callback, state, alert=i18n.t("mcp.apps.empty_alert"))
        return
    lines = [f"🔌 <b>{i18n.t('mcp.apps.title')}</b>\n"]
    user = await db.get_user(callback.from_user.id)
    buttons = []
    for row in connections:
        name = mcp_oauth.client_display_name(row["metadata"], row["client_id"])
        lines.append(
            i18n.t(
                "mcp.apps.line",
                name=escape(name),
                connected=_when(row["connected_at"], user),
                last_used=_when(row["last_used_at"], user),
            )
        )
        buttons.append((row["client_id"][:48], name))
    lines.append("\n" + i18n.t("mcp.apps.tail"))
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
        await _show(callback, state, alert=i18n.t("mcp.disconnect.already"))
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
        await _show(callback, state, alert=i18n.t("mcp.disconnect.done"))


@router.callback_query(F.data == "mcp:issue")
async def mcp_issue(callback: CallbackQuery, state: FSMContext):
    """Выдать токен — он же перевыпуск: db.issue_mcp_token гасит прежний."""
    if not config.mcp_available():
        await callback.answer(i18n.t("mcp.disabled_alert"), show_alert=True)
        return
    had_token = await db.get_mcp_token(callback.from_user.id) is not None
    await db.issue_mcp_token(callback.from_user.id)
    logger.info("MCP token issued for user %s (reissue=%s)", callback.from_user.id, had_token)
    await _show(
        callback,
        state,
        alert=i18n.t("mcp.issue.reissued") if had_token else None,
    )


@router.callback_query(F.data == "mcp:revoke")
async def mcp_revoke(callback: CallbackQuery, state: FSMContext):
    revoked = await db.revoke_mcp_token(callback.from_user.id)
    if revoked:
        logger.info("MCP token revoked for user %s", callback.from_user.id)
    await _show(
        callback,
        state,
        alert=i18n.t("mcp.revoke.done") if revoked else None,
    )
