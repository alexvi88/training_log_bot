"""Страница приглашения тренера `GET /coach/{code}` и
`/.well-known/apple-app-site-association` для Universal Links.

Зачем: раньше тренер раздавал `trainingdiary://coach/<код>`. Мессенджеры такую
схему не делают кликабельной, а без приложения она не делает ничего. Теперь в
тексте «Поделиться» — https-ссылка на эту страницу (`coach_service.web_link`):

- с приложением на iPhone ссылка открывает его сразу (Universal Link, файл
  AASA ниже), и приложение ведёт в тот же лист «Разрешить», что и схема;
- без приложения — эта страница: кто зовёт, крупный код и три выхода —
  «Открыть в приложении» (схема из Safari по нажатию кнопки работает),
  «Скачать в App Store» (если ссылка задана) и «Продолжить в Telegram»
  (`/start coach_<код>` в бота).

Наружу — только имя тренера, под которым его видят атлеты, ничего больше.
Страница публичная, без входа, поэтому поиск кодов ограничен по IP
(`LOOKUPS_PER_WINDOW`): кодов 31^8, и перебор и так безнадёжен, но
бесплатным оракулом страница не становится.

Висит на приложении MCP-сервера через `custom_route`, как `/privacy`
(legal_pages.py). Язык — из `Accept-Language`, как у `/v1/auth/*`.
Строк, вычисленных на импорте, нет — язык берётся в момент запроса.
"""

from __future__ import annotations

import json
import time
from html import escape
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

import coach_service
import config
import db
import i18n
import review_demo
from handlers import sharing

AASA_PATH = "/.well-known/apple-app-site-association"

# Поиск кода по IP: столько попыток за окно. Живой человек открывает одну
# ссылку пару раз; перебор упирается в потолок сразу.
LOOKUPS_PER_WINDOW = 30
WINDOW_SECONDS = 600
# Счётчики в памяти процесса — грубый предохранитель, как у
# mcp_oauth.RegisterRateLimitMiddleware; рестарт их обнуляет, и это нормально.
_MAX_TRACKED_IPS = 10_000
_hits: dict[str, list[float]] = {}

_STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; padding: 32px 16px 48px;
  font: 17px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  color: #f3e6d8;
  background: #1a1210 radial-gradient(circle at 50% -10%, #5a2a1c 0%, #1a1210 60%) no-repeat;
}
main { max-width: 460px; margin: 0 auto; }
.brand { margin: 0 0 20px; font-size: 13px; letter-spacing: .12em; font-weight: 700; color: #f2a541; }
h1 { font-size: 27px; line-height: 1.2; margin: 0 0 12px; overflow-wrap: anywhere; }
p { margin: 0 0 16px; overflow-wrap: anywhere; }
.card {
  margin: 24px 0; padding: 20px 16px; text-align: center;
  background: #2a1b16; border: 1px solid #5b3426; border-radius: 16px;
}
.label { font-size: 13px; color: #c9ab94; text-transform: uppercase; letter-spacing: .08em; }
.code {
  margin: 8px 0 14px; font: 700 36px/1.1 ui-monospace, SFMono-Regular, Menlo, monospace;
  letter-spacing: .14em; color: #fff4e6; user-select: all; -webkit-user-select: all;
}
.btn {
  display: block; width: 100%; margin: 0 0 12px; padding: 15px 16px; text-align: center;
  font: 600 17px/1.2 inherit; font-family: inherit; text-decoration: none; cursor: pointer;
  color: #f3e6d8; background: #3a251e; border: 1px solid #6b3d2c; border-radius: 12px;
}
.btn.primary { color: #1a1210; background: #f2a541; border-color: #f2a541; }
.btn.small { display: inline-block; width: auto; margin: 0; padding: 9px 16px; font-size: 15px; }
.muted { color: #c9ab94; font-size: 15px; }
"""

# Кнопка App Store тоже кладёт код в буфер: ссылка из App Store до приложения
# не доезжает (отложенных deep link у Apple нет), и после входа код останется
# вставить в лист «Код тренера». Комментариев в самом скрипте нет — он уходит
# на страницу как есть.
_COPY_SCRIPT = """
document.getElementById('copy').addEventListener('click', function () {
  var b = this, code = document.getElementById('code').textContent;
  var done = function () { b.textContent = b.dataset.done; };
  if (navigator.clipboard) { navigator.clipboard.writeText(code).then(done, function () {}); }
  else { var r = document.createRange(); r.selectNodeContents(document.getElementById('code'));
    var s = getSelection(); s.removeAllRanges(); s.addRange(r); document.execCommand('copy'); done(); }
});
var store = document.getElementById('store');
if (store && navigator.clipboard) {
  store.addEventListener('click', function () {
    navigator.clipboard.writeText(document.getElementById('code').textContent).catch(function () {});
  });
}
"""


def _lang(request: Request) -> str:
    return i18n.lang_from_accept_language(request.headers.get("accept-language"))


def _allow(ip: str) -> bool:
    now = time.monotonic()
    start = now - WINDOW_SECONDS
    hits = [t for t in _hits.get(ip, []) if t > start]
    if len(hits) >= LOOKUPS_PER_WINDOW:
        _hits[ip] = hits
        return False
    if ip not in _hits and len(_hits) >= _MAX_TRACKED_IPS:
        # Протухшие адреса вычищаем, только когда словарь распух.
        for key in [k for k, v in _hits.items() if not v or v[-1] <= start]:
            del _hits[key]
    hits.append(now)
    _hits[ip] = hits
    return True


def _page(lang: str, title: str, body: str, *, description: str = "") -> str:
    meta = ""
    if description:
        meta = (
            f'<meta property="og:title" content="{escape(title)}">'
            f'<meta property="og:description" content="{escape(description)}">'
        )
    return (
        f"<!doctype html><html lang={lang}><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        '<meta name=robots content="noindex">'
        f"<title>{escape(title)}</title>{meta}<style>{_STYLE}</style></head>"
        f'<body><main><p class=brand>{escape(i18n.t_in(lang, "bot.name"))}</p>{body}</main></body></html>'
    )


def render_invite(lang: str, name: str, code: str, bot_username: str | None) -> str:
    t = lambda key, **kw: i18n.t_in(lang, key, **kw)  # noqa: E731
    title = t("coach_page.title", name=name)
    lead = t("coach_page.lead", name=name)
    buttons = [f'<a class="btn primary" href="{escape(coach_service.APP_LINK_PREFIX + code)}">'
               f'{escape(t("coach_page.btn.open_app"))}</a>']
    store = config.app_store_url()
    if store:
        buttons.append(
            f'<a class=btn id=store href="{escape(store)}">{escape(t("coach_page.btn.app_store"))}</a>'
        )
    if bot_username:
        tg = coach_service.telegram_link(bot_username, code)
        buttons.append(f'<a class=btn href="{escape(tg)}">{escape(t("coach_page.btn.telegram"))}</a>')
    body = (
        f"<h1>{escape(title)}</h1><p>{escape(lead)}</p>"
        f'<div class=card><div class=label>{escape(t("coach_page.code_label"))}</div>'
        f"<div class=code id=code>{escape(code)}</div>"
        f'<button class="btn small" id=copy type=button data-done="{escape(t("coach_page.copied"))}">'
        f'{escape(t("coach_page.copy"))}</button></div>'
        + "".join(buttons)
        + f'<p class=muted>{escape(t("coach_page.manual"))}</p>'
        f"<script>{_COPY_SCRIPT}</script>"
    )
    return _page(lang, title, body, description=lead)


def render_dead_end(lang: str, kind: str) -> str:
    """`not_found` — кода нет или тренер выдал новый; `rate_limited` — много
    попыток с одного адреса."""
    title = i18n.t_in(lang, f"coach_page.{kind}.title")
    body = f"<h1>{escape(title)}</h1><p>{escape(i18n.t_in(lang, f'coach_page.{kind}.body'))}</p>"
    return _page(lang, title, body)


def _html(content: str, lang: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        content,
        status_code=status,
        headers={
            "Vary": "Accept-Language",
            "Content-Language": lang,
            # Код могут сменить в любую минуту — старую страницу кэш держать
            # не должен.
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


async def invite_route(request: Request) -> HTMLResponse:
    lang = _lang(request)
    if not _allow(review_demo.client_ip(request)):
        return _html(render_dead_end(lang, "rate_limited"), lang, 429)
    code = coach_service.normalize_invite_code(request.path_params.get("code", ""))
    valid = len(code) == coach_service.INVITE_LENGTH and all(
        ch in coach_service.INVITE_ALPHABET for ch in code
    )
    invite = await db.get_coach_invite(code) if valid else None
    profile = await db.get_coach_profile(invite["owner_id"]) if invite is not None else None
    if invite is None or profile is None:
        return _html(render_dead_end(lang, "not_found"), lang, 404)
    page = render_invite(lang, profile["display_name"], invite["code"], sharing._bot_username)
    return _html(page, lang)


def aasa_json() -> dict[str, Any] | None:
    app_id = config.apple_app_id()
    if not app_id:
        return None
    return {
        "applinks": {
            # Новый формат (iOS 13+) и старый `paths` рядом — второй ничему не
            # мешает, а старым системам нужен он.
            "apps": [],
            "details": [{
                "appIDs": [app_id],
                "components": [{"/": f"{coach_service.WEB_LINK_PATH}*"}],
                "appID": app_id,
                "paths": [f"{coach_service.WEB_LINK_PATH}*"],
            }],
        }
    }


async def aasa_route(request: Request) -> Response:
    """Отдаётся как есть, без редиректа и с application/json — так его
    забирает CDN Apple. Без Team ID — 404."""
    content = aasa_json()
    if content is None:
        return Response(status_code=404)
    return Response(
        json.dumps(content),
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def register_routes(server: Any) -> None:
    server.custom_route(f"{coach_service.WEB_LINK_PATH}{{code}}", methods=["GET"])(invite_route)
    server.custom_route(AASA_PATH, methods=["GET"])(aasa_route)
