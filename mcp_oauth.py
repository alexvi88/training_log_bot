"""OAuth 2.1 для MCP-сервера: бот подключается как обычный коннектор.

Зачем это рядом со статическим токеном (`db.mcp_tokens`). Токен в заголовке
умеют слать клиенты, где заголовок можно вписать руками: Claude Code, Cursor,
VS Code. Браузерный claude.ai, нативные коннекторы Claude Desktop и ChatGPT
статический токен не принимают вовсе — им нужен OAuth. А это ровно те клиенты,
где человек не настраивает ничего: вставил адрес, подтвердил, готово. Поэтому
работают оба способа, и ни один не отменяет другой.

Как устроено:

* Протокол закрывает SDK. `MCPServer(auth_server_provider=...)` сам поднимает
  `/authorize`, `/token`, `/register`, `/revoke` и оба `.well-known`, проверяет
  PKCE, сверяет `redirect_uri` и срок кода. Наша часть — хранилище (см. четыре
  таблицы `oauth_*` в db.py) и страница согласия.
* Кто перед нами. У пользователя бота ровно один аккаунт — телеграмный, и
  подтвердить владение им можно только через сам бот. Поэтому на странице
  согласия человек называет шестизначный код, который ему выдал бот на экране
  /mcp. Ни пароля, ни регистрации, ни второго канала связи не нужно.
* Личность внутри инструмента. `AccessToken.subject` — это `telegram_id`, и
  кладётся он туда обоими путями: и OAuth-токеном, и статическим. У сервера
  ровно один способ узнать, чьи данные отдавать (см. `mcp_server._user_id`).
* Значения токенов и кодов не логируются — ни в `info`, ни в тексте ошибок.
  В базе они лежат открытыми (отдельного хранилища секретов у бота нет), и
  единственное, что можно сделать сверх этого, — не разносить их по логам.
"""

import json
import logging
import secrets
import time
from html import escape
from typing import Any, Optional
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

import config
import db

logger = logging.getLogger(__name__)

# Страница согласия. Живёт под /mcp, чтобы весь протокол на домене занимал одну
# ветку пути: так проще и проксировать, и объяснять.
CONSENT_PATH = "/mcp/consent"

# Сроки жизни. Access короткий — утёкший токен перестаёт работать сам; refresh
# длинный, иначе человеку пришлось бы переподключать коннектор каждый час.
ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 30 * 24 * 3600
# Код авторизации живёт ровно до похода клиента на /token — это секунды.
AUTH_CODE_TTL = 300
# Заявка на согласие — до того, как человек сходит в бота за кодом.
CONSENT_REQUEST_TTL = 900

# Десять минут, а не пять: столько уходит на то, чтобы найти в приложении раздел
# коннекторов, вставить адрес и дождаться страницы подтверждения. Код при этом
# одноразовый, гасится при успехе, а перебор упирается в лимит попыток — цена
# лишних пяти минут жизни куда ниже, чем «код истёк, начинай сначала».
LINK_CODE_TTL = 600
LINK_CODE_TTL_MINUTES = LINK_CODE_TTL // 60

# Три предела на ввод кода, и только два последних что-то ограничивают.
#
# Пять попыток на заявку — про опечатки: заявку создаёт бесплатный GET /authorize,
# так что перебирающий берёт новую и счёт начинается заново (так и было устроено,
# и это ничего не защищало). Настоящий замок — скользящее окно неудач: с одного
# адреса десять за десять минут, суммарно шестьдесят. Двадцать бит кода при таком
# счёте не перебрать, а живой человек столько не ошибается — он ошибается два раза.
LINK_CODE_MAX_ATTEMPTS = 5
CONSENT_FAILURE_WINDOW = 600
CONSENT_FAILURE_LIMIT_PER_IP = 10
CONSENT_FAILURE_LIMIT_TOTAL = 60

# Регистрация клиента (POST /register, RFC 7591) — анонимный эндпоинт по
# спецификации: SDK и db.save_oauth_client ограничивают только размер
# метаданных (см. db.OAUTH_CLIENT_METADATA_LIMIT), не частоту. Без счётчика
# анонимный флуд может нарастить oauth_clients за часы — прополка неиспользуемых
# регистраций идёт раз в сутки (db.purge_expired_oauth). Окно и предел — те же
# порядки, что и у CONSENT_FAILURE_*, тот же класс защиты.
REGISTER_RATE_LIMIT_WINDOW = 600
REGISTER_RATE_LIMIT_PER_IP = 10

# `client_id` статического токена. Токен выпускает бот, а не приложение, так что
# клиента у него нет — но `AccessToken.client_id` обязателен, и осмысленное
# значение лучше пустой строки: оно видно в логах вызовов.
STATIC_CLIENT_ID = "static"

# Адрес для issuer, когда публичный не задан. Сервер в этом случае и не
# поднимается (см. config.mcp_available), но собрать приложение должно быть
# можно — на этом стоят тесты, а issuer у AuthSettings обязателен и обязан быть
# либо HTTPS, либо локальным.
_LOCAL_ISSUER = "http://localhost"


def public_base_url() -> str:
    return config.MCP_PUBLIC_URL or _LOCAL_ISSUER


def auth_settings(resource_path: str) -> AuthSettings:
    """Настройки OAuth для MCPServer.

    `issuer_url` — сам домен: на нём SDK развесит `/authorize`, `/token` и
    метаданные. `resource_server_url` — адрес самого MCP: он попадает в
    `.well-known/oauth-protected-resource` и в заголовок `WWW-Authenticate`,
    по которому клиент и находит, куда идти авторизовываться.
    """
    base = public_base_url()
    return AuthSettings(
        issuer_url=base,
        resource_server_url=f"{base}{resource_path}",
        # Динамическая регистрация обязательна: ни Claude, ни ChatGPT не дают
        # человеку вписать client_id руками — они регистрируются сами.
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )


# Имя приложения приходит из регистрации, то есть его пишет тот, кто
# регистрируется, — кто угодно. Обрезаем: без предела 300 символов лезут в подпись
# кнопки Telegram и в вёрстку страницы согласия.
CLIENT_NAME_LIMIT = 40


def client_display_name(metadata: Optional[str], client_id: str) -> str:
    """Имя приложения для человека.

    Из метаданных регистрации, если там есть непустое имя; иначе — хост, куда
    клиент просит вернуть код (он говорит человеку больше, чем что-либо ещё);
    в последнюю очередь — начало client_id, чтобы две строки в списке различались.
    """
    parsed: dict = {}
    if metadata:
        try:
            loaded = json.loads(metadata)
            parsed = loaded if isinstance(loaded, dict) else {}
        except ValueError:
            parsed = {}
    name = str(parsed.get("client_name") or "").strip()
    if name:
        return name[:CLIENT_NAME_LIMIT]
    host = redirect_host(parsed.get("redirect_uris") or [])
    if host:
        return host
    return f"приложение {client_id[:8]}"


def redirect_host(redirect_uris: Any) -> str:
    """Хост, куда уедет код авторизации.

    Единственное на странице согласия, что нельзя подделать вместе с именем: имя
    приложения атакующий пишет какое хочет, а адрес возврата — тот, куда данные
    реально уйдут.
    """
    if isinstance(redirect_uris, str):
        redirect_uris = [redirect_uris]
    for uri in redirect_uris or []:
        host = urlparse(str(uri)).hostname
        if host:
            return host
    return ""


async def link_code(user_id: int, force_new: bool = False) -> str:
    """Код связывания для экрана /mcp.

    По умолчанию отдаётся действующий, если он есть: человек мог уже скопировать
    его и вернуться в бота перечитать шаг — выдать в этот момент новый значит
    убить тот, что у него в браузере. Новый выдаётся только по явной кнопке.
    """
    return await db.issue_oauth_link_code(
        user_id, LINK_CODE_TTL, reuse_live=not force_new
    )


class TrainingLogOAuthProvider:
    """`OAuthAuthorizationServerProvider` поверх SQLite.

    Состояние всё до последней строки лежит в базе, а не в памяти процесса:
    контейнер перезапускается по воле хостинга, и перезапуск посреди
    подключения не должен выглядеть как «коннектор не работает».
    """

    # ---------- клиенты ----------

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        row = await db.get_oauth_client(client_id)
        if row is None:
            return None
        return OAuthClientInformationFull.model_validate_json(row["metadata"])

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Записать регистрацию, отвергнув то, чем пользоваться нельзя.

        Схему адреса возврата проверяем сами: SDK принимает любой URI, а `http:`
        означает код авторизации открытым текстом по сети (OAuth 2.1 это прямо
        запрещает), `javascript:` и `data:` — попытку получить исполнение на
        нашем домене. Loopback оставляем: на нём живут локальные клиенты.
        """
        for uri in client_info.redirect_uris or []:
            parsed = urlparse(str(uri))
            loopback = parsed.hostname in ("localhost", "127.0.0.1", "::1", "[::1]")
            if parsed.scheme == "https" or (parsed.scheme == "http" and loopback):
                continue
            raise RegistrationError(
                "invalid_redirect_uri",
                f"redirect_uri must use https (got {parsed.scheme or 'no'} scheme)",
            )
        try:
            await db.save_oauth_client(
                client_info.client_id,
                client_info.client_secret,
                client_info.model_dump_json(),
            )
        except ValueError as e:
            raise RegistrationError("invalid_client_metadata", str(e)) from e
        logger.info("MCP OAuth: client registered (%s)", client_info.client_id)

    # ---------- согласие ----------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Запомнить запрос и отправить человека на страницу согласия.

        Код авторизации здесь не выдаётся: сначала надо узнать, кто перед нами.
        Всё, что понадобится для редиректа обратно, — включая `state`, которого
        нет в модели кода, — уезжает в заявку.
        """
        request_id = secrets.token_urlsafe(16)
        await db.create_oauth_consent_request(
            request_id=request_id,
            client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge,
            scopes=json.dumps(params.scopes or []),
            resource=params.resource,
            state=params.state,
            expires_at=time.time() + CONSENT_REQUEST_TTL,
        )
        return f"{CONSENT_PATH}?request={request_id}"

    # ---------- код авторизации ----------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[AuthorizationCode]:
        row = await db.get_oauth_auth_code(authorization_code)
        if row is None:
            return None
        return AuthorizationCode(
            code=row["code"],
            scopes=json.loads(row["scopes"]),
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["redirect_uri_provided_explicitly"]),
            resource=row["resource"],
            subject=str(row["user_id"]),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """Код → пара токенов. Код гасится тем же запросом, которым обменян.

        SDK к этому моменту уже проверил PKCE, `redirect_uri` и срок — но не
        одноразовость: код он читал через `load_authorization_code`, а гасить
        его некому, кроме нас.
        """
        row = await db.consume_oauth_auth_code(authorization_code.code)
        if row is None:
            raise TokenError("invalid_grant", "authorization code has already been used")
        if row["expires_at"] < time.time():
            raise TokenError("invalid_grant", "authorization code has expired")
        return await self._issue_pair(
            client_id=row["client_id"],
            user_id=row["user_id"],
            scopes=json.loads(row["scopes"]),
            resource=row["resource"],
        )

    # ---------- refresh ----------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[RefreshToken]:
        row = await db.get_oauth_refresh_token(refresh_token)
        if row is None:
            return None
        expires_at = row["refresh_expires_at"]
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=int(expires_at) if expires_at is not None else None,
            subject=str(row["user_id"]),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Обновление с ротацией обоих токенов: старая пара умирает целиком."""
        row = await db.consume_oauth_refresh_token(refresh_token.token)
        if row is None:
            raise TokenError("invalid_grant", "refresh token has already been used")
        return await self._issue_pair(
            client_id=row["client_id"],
            user_id=row["user_id"],
            scopes=scopes or json.loads(row["scopes"]),
            resource=row["resource"],
            # Дата подключения переезжает в новую пару: человек подтвердил доступ
            # один раз, а обновление токена — служебная механика, и показывать её
            # как «подключено только что» неправда.
            connected_at=row["connected_at"] or row["created_at"],
        )

    async def _issue_pair(
        self,
        client_id: str,
        user_id: int,
        scopes: list[str],
        resource: Optional[str],
        connected_at: Optional[str] = None,
    ) -> OAuthToken:
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        now = time.time()
        await db.create_oauth_token(
            access_token=access_token,
            refresh_token=refresh_token,
            client_id=client_id,
            user_id=user_id,
            scopes=json.dumps(scopes),
            resource=resource,
            expires_at=now + ACCESS_TOKEN_TTL,
            refresh_expires_at=now + REFRESH_TOKEN_TTL,
            connected_at=connected_at,
        )
        logger.info("MCP OAuth: tokens issued for user %s, client %s", user_id, client_id)
        return OAuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes) if scopes else None,
        )

    # ---------- проверка токена ----------

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        """Оба вида токенов приходят сюда: сперва ищем OAuth, потом статический.

        Это единственная точка, где токен превращается в личность, и потому
        единственное место, где статический токен продолжает работать. Наружу
        оба вида уходят одинаковыми: `subject` — telegram_id.
        """
        if not token:
            return None
        row = await db.get_oauth_access_token(token)
        if row is not None:
            if row["expires_at"] < time.time():
                # Различаем «истёк» и «не найден» в логе — обе ветки раньше
                # возвращали один и тот же None, и разобрать жалобу «почему
                # отвалился коннектор» можно было только руками по базе.
                logger.info("MCP OAuth: access token expired (client %s)", row["client_id"])
                return None
            await db.touch_oauth_token(token)
            return AccessToken(
                token=token,
                client_id=row["client_id"],
                scopes=json.loads(row["scopes"]),
                expires_at=int(row["expires_at"]),
                resource=row["resource"],
                subject=str(row["user_id"]),
            )
        user_id = await db.resolve_mcp_token(token)
        if user_id is None:
            logger.info("MCP OAuth: access token not found (neither OAuth nor static)")
            return None
        # Срок не выставляем: статический токен бессрочен, пока его не отозвали
        # в боте.
        return AccessToken(
            token=token, client_id=STATIC_CLIENT_ID, scopes=[], subject=str(user_id)
        )

    async def revoke_token(self, token: Any) -> None:
        """Отзыв гасит пару целиком — что именно принёс клиент, access или
        refresh, значения не имеет: в базе они лежат одной строкой."""
        await db.revoke_oauth_token(getattr(token, "token", ""))


# ---------- страница согласия ----------
#
# Две страницы на весь проект — шаблонизатор ради них не нужен. Вся разметка
# самодостаточная: ни одного внешнего файла, потому что отдаёт её тот же
# процесс, что и MCP, и статику ему раздавать нечем.

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; display: flex; align-items: center;
  justify-content: center; padding: 24px;
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #f5f5f7; color: #1d1d1f;
}
.card {
  width: 100%; max-width: 480px; background: #fff; border-radius: 16px;
  padding: 28px; box-shadow: 0 8px 32px rgba(0,0,0,.08);
}
h1 { font-size: 22px; margin: 0 0 4px; }
.app { font-weight: 600; }
p { margin: 12px 0; }
ul { margin: 12px 0; padding-left: 22px; }
li { margin: 4px 0; }
.muted { color: #6e6e73; font-size: 14px; }
.error {
  background: #fdecea; color: #b3261e; border-radius: 10px;
  padding: 10px 12px; margin: 12px 0; font-size: 14px;
}
input[name="code"] {
  width: 100%; font-size: 28px; letter-spacing: 6px; text-align: center;
  padding: 12px; border: 1px solid #d2d2d7; border-radius: 12px;
  background: #fff; color: inherit;
}
/* row-reverse: «Разрешить» стоит в разметке ПЕРВОЙ, чтобы Enter в поле кода
   отправлял согласие, а не отказ (первая submit-кнопка формы — кнопка по
   умолчанию), но человек видит её справа, как принято. */
.row { display: flex; flex-direction: row-reverse; gap: 12px; margin-top: 18px; }
button {
  flex: 1; padding: 13px; font-size: 16px; font-weight: 600;
  border-radius: 12px; border: 1px solid transparent; cursor: pointer;
}
.allow { background: #0071e3; color: #fff; }
.deny { background: transparent; color: #515156; border-color: #86868b; }
.dest { font-weight: 600; word-break: break-all; }
.app { word-break: break-word; }
@media (prefers-color-scheme: dark) {
  body { background: #16161a; color: #f5f5f7; }
  .card { background: #1f1f24; box-shadow: none; }
  .muted { color: #a1a1a6; }
  .deny { color: #c7c7cc; border-color: #6a6a70; }
  input[name="code"] { background: #2a2a30; border-color: #3a3a41; }
  .error { background: #3a1d1a; color: #ff9f95; }
}
"""

_SHARED_DATA = (
    "<li>тренировки и подходы, включая заметки</li>"
    "<li>прогресс по упражнениям и рекорды</li>"
    "<li>объём по группам мышц</li>"
    "<li>вес тела и дневник питания</li>"
    "<li>сохранённые программы</li>"
)


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    html = (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><div class=card>{body}</div></body></html>"
    )
    return HTMLResponse(
        html,
        status_code=status_code,
        headers={
            # no-store: на странице лежит код связывания, и возвращаться на неё
            # кнопкой «назад» из кэша браузера ей незачем.
            "Cache-Control": "no-store",
            # Страницу, на которой человек отдаёт доступ, нельзя вкладывать в
            # чужой интерфейс: в iframe она выглядит частью сайта атакующего.
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "frame-ancestors 'none'",
        },
    )


def _consent_page(
    request_id: str, app_name: str, destination: str = "", error: str = ""
) -> HTMLResponse:
    error_block = (
        f'<div class="error" role="alert" id="err">{escape(error)}</div>' if error else ""
    )
    # Хост назначения — единственное на странице, что нельзя подделать заодно с
    # именем: назваться «Claude» может кто угодно, а код уедет туда, куда просил
    # клиент при регистрации. Человеку это и надо сверить.
    destination_block = (
        f'<p>Код и доступ уйдут на <span class="dest">{escape(destination)}</span>. '
        "Если это не то приложение, которое ты подключаешь, — нажми «Отмена».</p>"
        if destination
        else ""
    )
    return _page(
        "Дневник тренировок — подтверждение",
        "<h1>Дневник тренировок</h1>"
        f'<p><span class="app">{escape(app_name)}</span> просит доступ к твоим данным:</p>'
        f"<ul>{_SHARED_DATA}</ul>"
        f"{destination_block}"
        '<p class="muted">Читать может всё это. Писать — вес, еду, убрать неверную '
        "запись из дневника, новое упражнение, копию программы. Переименовать "
        "что-то снаружи нельзя — это, как и переписку с AI-тренером, можно только "
        "в самом боте.</p>"
        f"{error_block}"
        # action="" — на текущий адрес: так форма не ломается, если бот когда-нибудь
        # окажется за префиксом пути.
        '<form method="post" action="">'
        f'<input type="hidden" name="request" value="{escape(request_id)}">'
        # Никуда не посылаем: код уже лежит в том же сообщении бота, из которого
        # человек сюда пришёл, — инструкция самодостаточна (см. handlers/mcp_access).
        # Отправлять его «открыть бота и нажать кнопку» значит противоречить экрану,
        # который у него открыт на соседнем устройстве.
        '<p><label for="code">Введи код из бота — он в том же сообщении, '
        "где инструкция:</label></p>"
        # maxlength с запасом и без pattern: сервер сам выбрасывает из кода всё,
        # кроме цифр, а браузер, обрезающий вставленное «123 456» до шести
        # символов, ломает ровно то, что сервер терпит.
        '<input id="code" name="code" inputmode="numeric" autocomplete="off" '
        'maxlength="16" placeholder="000000" required autofocus'
        f'{" aria-describedby=err" if error else ""}>'
        '<div class="row">'
        '<button class="allow" type="submit" name="action" value="allow">Разрешить</button>'
        '<button class="deny" type="submit" name="action" value="deny" '
        'formnovalidate>Отмена</button>'
        "</div></form>"
        # Для того, кто начал с приложения, а бота ещё не открывал: сказать, где
        # код берётся, надо — но мелким шрифтом и после поля, чтобы не посылать
        # туда того, у кого код уже есть.
        '<p class="muted">Кода нет под рукой? В боте: <b>/mcp</b> → выбери своё '
        "приложение, код будет в том же сообщении.</p>",
        status_code=200 if not error else 400,
    )


def _dead_end_page(message: str) -> HTMLResponse:
    return _page(
        "Дневник тренировок",
        f"<h1>Дневник тренировок</h1><p>{escape(message)}</p>"
        '<p class="muted">Вернись в приложение и начни подключение заново.</p>',
        status_code=400,
    )


_ERRORS = {
    # Куда идти за новым — самое полезное, что тут можно сказать: чаще всего код
    # не подошёл именно потому, что устарел, пока человек искал раздел коннекторов.
    "bad_code": (
        "Код не подошёл — скорее всего устарел. В боте нажми «🔄 Новый код» "
        "на том же экране и введи ещё раз."
    ),
    "empty_code": "Введи шесть цифр из бота.",
}

# Тупики: заявку уже не оживить, и форма на ней только водит по кругу.
_DEAD_ENDS = {
    "unknown_request": "Запрос на подключение устарел или не найден.",
    "expired_request": "Запрос на подключение устарел или не найден.",
    "too_many_attempts": "Слишком много неверных попыток по этому запросу.",
    "rate_limited": (
        "Слишком много попыток ввода за последние минуты. Подожди немного и "
        "начни подключение заново из приложения."
    ),
}


def _client_ip(request: Request) -> Optional[str]:
    """Адрес, по которому считаются неудачные попытки.

    За прокси хостинга `request.client` — это сам прокси, поэтому берём последний
    элемент X-Forwarded-For: его дописывает прокси, и подделать его клиент не
    может (всё, что он пришлёт сам, окажется левее). Без прокси остаётся адрес
    соединения.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip() or None
    return request.client.host if request.client else None


class RegisterRateLimitMiddleware:
    """Ограничивает POST /register по IP — единственная защита, которой у
    динамической регистрации клиентов не было вовсе (см. REGISTER_RATE_LIMIT_*).

    Лимит держится в памяти процесса, а не в БД: это грубый предохранитель от
    анонимного флуда, а не точный аудит вроде oauth_consent_failures, и не
    обязан переживать рестарт контейнера.
    """

    def __init__(self, app):
        self._app = app
        self._hits: dict[str, list[float]] = {}

    def _allow(self, ip: Optional[str]) -> bool:
        key = ip or "(unknown)"
        now = time.monotonic()
        window_start = now - REGISTER_RATE_LIMIT_WINDOW
        hits = [t for t in self._hits.get(key, []) if t > window_start]
        hits.append(now)
        self._hits[key] = hits
        return len(hits) <= REGISTER_RATE_LIMIT_PER_IP

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/register":
            await self._app(scope, receive, send)
            return
        request = Request(scope, receive)
        if self._allow(_client_ip(request)):
            await self._app(scope, receive, send)
            return
        response = Response(
            json.dumps({"error": "too_many_requests", "error_description": "Слишком много регистраций, попробуй позже."}),
            status_code=429,
            media_type="application/json",
        )
        await response(scope, receive, send)


async def _named_client(consent) -> tuple[str, str]:
    """(имя приложения, хост назначения) для страницы согласия."""
    client = await db.get_oauth_client(consent["client_id"])
    metadata = client["metadata"] if client else None
    name = client_display_name(metadata, consent["client_id"])
    host = ""
    if metadata:
        try:
            host = redirect_host(json.loads(metadata).get("redirect_uris"))
        except ValueError:  # pragma: no cover — метаданные пишет pydantic
            host = ""
    return name, host or redirect_host([consent["redirect_uri"]])


async def consent_route(request: Request) -> Response:
    """GET рисует страницу согласия, POST — проверяет код и уводит обратно.

    Роут публичный по определению: сюда приходит человек из браузера, у которого
    ещё нет никакого токена, — это и есть тот шаг, где он его получает.
    """
    if request.method == "GET":
        request_id = request.query_params.get("request", "")
        consent = await db.get_oauth_consent_request(request_id)
        if consent is None or consent["expires_at"] < time.time():
            return _dead_end_page(_DEAD_ENDS["unknown_request"])
        name, destination = await _named_client(consent)
        return _consent_page(request_id, name, destination)

    form = await request.form()
    request_id = str(form.get("request", ""))
    consent = await db.get_oauth_consent_request(request_id)
    if consent is None or consent["expires_at"] < time.time():
        # Сюда же приходит второй клик по «Разрешить»: заявка уже погашена первым.
        return _dead_end_page(
            "Запрос на подключение устарел, не найден — или доступ уже выдан. "
            "Проверь приложение: возможно, всё уже подключено."
        )

    # Согласие — только по явной кнопке. Любая другая отправка (потерялось
    # значение кнопки, самодельный запрос) — отказ: fail-closed на том шаге, где
    # человек отдаёт доступ ко всей своей истории.
    if form.get("action") != "allow":
        await db.delete_oauth_consent_request(request_id)
        return RedirectResponse(
            construct_redirect_uri(
                consent["redirect_uri"],
                error="access_denied",
                error_description="user denied the request",
                state=consent["state"],
            ),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    # Пробелы и дефисы человек вставляет сам, копируя код из чата.
    entered = "".join(ch for ch in str(form.get("code", "")) if ch.isdigit())
    if not entered:
        # Пустая отправка — промах пальцем, а не попытка угадать: считать её
        # попыткой значит запирать заявку тому, кто ещё ничего не вводил.
        name, destination = await _named_client(consent)
        return _consent_page(request_id, name, destination, error=_ERRORS["empty_code"])
    verdict, user_id = await db.verify_oauth_link_code(
        request_id,
        entered,
        LINK_CODE_MAX_ATTEMPTS,
        client_ip=_client_ip(request),
        window_seconds=CONSENT_FAILURE_WINDOW,
        window_limit_per_ip=CONSENT_FAILURE_LIMIT_PER_IP,
        window_limit_total=CONSENT_FAILURE_LIMIT_TOTAL,
    )
    if verdict != "ok":
        logger.info(
            "MCP OAuth: consent rejected (%s) for client %s", verdict, consent["client_id"]
        )
        if verdict in _DEAD_ENDS:
            return _dead_end_page(_DEAD_ENDS[verdict])
        name, destination = await _named_client(consent)
        return _consent_page(request_id, name, destination, error=_ERRORS[verdict])

    code = secrets.token_urlsafe(32)
    await db.create_oauth_auth_code(
        code=code,
        client_id=consent["client_id"],
        user_id=user_id,
        redirect_uri=consent["redirect_uri"],
        redirect_uri_provided_explicitly=bool(consent["redirect_uri_provided_explicitly"]),
        code_challenge=consent["code_challenge"],
        scopes=consent["scopes"],
        resource=consent["resource"],
        expires_at=time.time() + AUTH_CODE_TTL,
    )
    await db.delete_oauth_consent_request(request_id)
    logger.info(
        "MCP OAuth: consent granted for user %s, client %s", user_id, consent["client_id"]
    )
    return RedirectResponse(
        construct_redirect_uri(consent["redirect_uri"], code=code, state=consent["state"]),
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


def register_routes(server: Any) -> None:
    """Повесить страницу согласия на приложение MCP-сервера.

    `custom_route` кладёт роут без требования токена — иначе страница, на
    которой токен как раз и добывается, была бы недостижима.
    """
    server.custom_route(CONSENT_PATH, methods=["GET", "POST"])(consent_route)
