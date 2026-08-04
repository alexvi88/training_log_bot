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

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
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
# Шесть цифр перебираются за минуты, если пробовать их можно бесконечно.
LINK_CODE_MAX_ATTEMPTS = 5

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


def client_display_name(metadata: Optional[str], client_id: str) -> str:
    """Имя приложения для человека: из метаданных регистрации, а если их нет —
    хотя бы начало client_id, чтобы две строки в списке различались."""
    if metadata:
        try:
            name = json.loads(metadata).get("client_name")
        except (ValueError, AttributeError):
            name = None
        if name:
            return str(name)
    return f"приложение {client_id[:8]}"


async def link_code(user_id: int, force_new: bool = False) -> str:
    """Код связывания для экрана /mcp.

    По умолчанию отдаётся действующий, если он есть: человек мог уже скопировать
    его и вернуться в бота перечитать шаг — выдать в этот момент новый значит
    убить тот, что у него в браузере. Новый выдаётся только по явной кнопке.
    """
    if not force_new:
        live = await db.get_live_oauth_link_code(user_id)
        if live is not None:
            return live
    return await db.issue_oauth_link_code(user_id, LINK_CODE_TTL)


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
        await db.save_oauth_client(
            client_info.client_id,
            client_info.client_secret,
            client_info.model_dump_json(),
        )
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
.row { display: flex; gap: 12px; margin-top: 18px; }
button {
  flex: 1; padding: 13px; font-size: 16px; font-weight: 600;
  border-radius: 12px; border: 1px solid transparent; cursor: pointer;
}
.allow { background: #0071e3; color: #fff; }
.deny { background: transparent; color: #6e6e73; border-color: #d2d2d7; }
@media (prefers-color-scheme: dark) {
  body { background: #16161a; color: #f5f5f7; }
  .card { background: #1f1f24; box-shadow: none; }
  .muted, .deny { color: #a1a1a6; }
  input[name="code"] { background: #2a2a30; border-color: #3a3a41; }
  .deny { border-color: #3a3a41; }
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
    # no-store: на странице лежит код связывания, и возвращаться на неё кнопкой
    # «назад» из кэша браузера ей незачем.
    return HTMLResponse(html, status_code=status_code, headers={"Cache-Control": "no-store"})


def _consent_page(request_id: str, app_name: str, error: str = "") -> HTMLResponse:
    error_block = f'<div class="error">{escape(error)}</div>' if error else ""
    return _page(
        "Дневник тренировок — подтверждение",
        "<h1>Дневник тренировок</h1>"
        f'<p><span class="app">{escape(app_name)}</span> просит доступ к твоим данным '
        "<b>только на чтение</b>:</p>"
        f"<ul>{_SHARED_DATA}</ul>"
        '<p class="muted">Записать что-либо в дневник приложение не сможет. '
        "Переписка с AI-тренером наружу не уходит.</p>"
        f"{error_block}"
        f'<form method="post" action="{CONSENT_PATH}">'
        f'<input type="hidden" name="request" value="{escape(request_id)}">'
        # Никуда не посылаем: код уже лежит в том же сообщении бота, из которого
        # человек сюда пришёл, — инструкция самодостаточна (см. handlers/mcp_access).
        # Отправлять его «открыть бота и нажать кнопку» значит противоречить экрану,
        # который у него открыт на соседнем устройстве.
        "<p>Введи код из бота — он в том же сообщении, где инструкция:</p>"
        '<input name="code" inputmode="numeric" autocomplete="off" maxlength="6" '
        'pattern="[0-9]*" placeholder="000000" autofocus>'
        '<div class="row">'
        '<button class="deny" type="submit" name="action" value="deny">Отмена</button>'
        '<button class="allow" type="submit" name="action" value="allow">Разрешить</button>'
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
    "too_many_attempts": (
        "Слишком много попыток. Начни подключение заново из приложения."
    ),
}


async def consent_route(request: Request) -> Response:
    """GET рисует страницу согласия, POST — проверяет код и уводит обратно.

    Роут публичный по определению: сюда приходит человек из браузера, у которого
    ещё нет никакого токена, — это и есть тот шаг, где он его получает.
    """
    if request.method == "GET":
        request_id = request.query_params.get("request", "")
        consent = await db.get_oauth_consent_request(request_id)
        if consent is None or consent["expires_at"] < time.time():
            return _dead_end_page("Запрос на подключение устарел или не найден.")
        client = await db.get_oauth_client(consent["client_id"])
        name = client_display_name(
            client["metadata"] if client else None, consent["client_id"]
        )
        return _consent_page(request_id, name)

    form = await request.form()
    request_id = str(form.get("request", ""))
    consent = await db.get_oauth_consent_request(request_id)
    if consent is None or consent["expires_at"] < time.time():
        return _dead_end_page("Запрос на подключение устарел или не найден.")

    if form.get("action") == "deny":
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
    verdict, user_id = await db.verify_oauth_link_code(
        request_id, entered, LINK_CODE_MAX_ATTEMPTS
    )
    if verdict != "ok":
        if verdict in ("unknown_request", "expired_request"):  # pragma: no cover
            return _dead_end_page("Запрос на подключение устарел или не найден.")
        client = await db.get_oauth_client(consent["client_id"])
        name = client_display_name(
            client["metadata"] if client else None, consent["client_id"]
        )
        logger.info(
            "MCP OAuth: consent rejected (%s) for client %s", verdict, consent["client_id"]
        )
        return _consent_page(request_id, name, error=_ERRORS[verdict])

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
