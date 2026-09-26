"""Публичные страницы `/privacy` и `/terms` — политика конфиденциальности и
условия использования, — и `/support`: как связаться с поддержкой.

Зачем: App Store (Guideline 5.1.1(i)) требует публичный URL политики — он
вписывается в App Store Connect — и ссылку на неё внутри приложения. Страницы
живут на том же хосте, что и `/v1` (приложение открывает
`<адрес сервера>/privacy?lang=<язык аккаунта>`), и отдаются без авторизации:
их читает и ревьюер Apple, и человек до входа.

Висят на приложении MCP-сервера через `custom_route` — тот же приём, что у
страницы согласия OAuth (`mcp_oauth.register_routes`): это приложение по
умолчанию у `mcp_server._PrefixDispatch`, всё, что не `/v1`, уходит к нему.

Сам текст — не в Python, а в `legal/<страница>.<язык>.html`: юридический
текст правится как документ, целиком, и в коде ему нечего делать (а русский
литерал в модуле ловил бы храповик `i18n_coverage`). Каждое утверждение в нём
сверено с кодом — меняешь, что хранится или кому уходит (новый провайдер
модели, новая таблица с данными атлета, другой срок чистки в config.py), —
поправь обе языковые версии политики тем же PR.

Язык: явный `?lang=ru|en` побеждает (так зовёт приложение — язык аккаунта, а
не телефона), иначе `Accept-Language` (браузер ревьюера), иначе русский — как
у всего продукта (`i18n.DEFAULT_LANG`).
"""

from __future__ import annotations

import re
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse

import i18n

_DIR = Path(__file__).resolve().parent / "legal"

PAGES = ("privacy", "terms")
# Страница «как связаться с поддержкой» — рядом с юридическими, тот же вид и
# тот же выбор языка, но без даты вступления в силу: это не документ, а
# справка (ссылка на неё — Support URL в App Store Connect).
SUPPORT_PAGE = "support"
ALL_PAGES = PAGES + (SUPPORT_PAGE,)
LANGS = ("ru", "en")

# Контактный e-mail для страниц. Пустой нарочно: владелец адрес ещё не выбрал,
# а выдумывать его нельзя. Пока пусто — связь только через «Отзыв» в
# приложении и /feedback в боте (они уже в тексте). Впишешь адрес — он сам
# появится строкой в разделе «Связь» обеих страниц и на странице поддержки,
# на обоих языках.
CONTACT_EMAIL = ""

_EMAIL_MARKER = "<!-- contact-email -->"

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px 16px 48px;
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #f5f5f7; color: #1d1d1f;
}
main {
  max-width: 720px; margin: 0 auto; background: #fff; border-radius: 16px;
  padding: 28px 24px; box-shadow: 0 8px 32px rgba(0,0,0,.08);
}
h1 { font-size: 26px; line-height: 1.25; margin: 0 0 8px; }
h2 { font-size: 19px; margin: 28px 0 8px; }
p, li { overflow-wrap: anywhere; }
ul { padding-left: 22px; }
li { margin: 6px 0; }
a { color: #0066cc; }
.muted { color: #6e6e73; font-size: 14px; }
@media (prefers-color-scheme: dark) {
  body { background: #16161a; color: #f5f5f7; }
  main { background: #1f1f24; box-shadow: none; }
  a { color: #4da3ff; }
  .muted { color: #a1a1a6; }
}
"""

_H1 = re.compile(r"<h1>(.*?)</h1>", re.S)


@lru_cache(maxsize=None)
def _fragment(page: str, lang: str) -> str:
    return (_DIR / f"{page}.{lang}.html").read_text(encoding="utf-8")


def page_lang(request: Request) -> str:
    explicit = (request.query_params.get("lang") or "").strip().lower()
    if explicit in LANGS:
        return explicit
    return i18n.lang_from_accept_language(request.headers.get("accept-language"))


def render(page: str, lang: str) -> str:
    body = _fragment(page, lang)
    if CONTACT_EMAIL:
        address = escape(CONTACT_EMAIL)
        link = f'E-mail: <a href="mailto:{address}">{address}</a>'
        # В политике «Связь» и на странице поддержки — список, в условиях —
        # одна строка текстом.
        snippet = f"<li>{link}</li>" if page in ("privacy", SUPPORT_PAGE) else f" {link}."
        body = body.replace(_EMAIL_MARKER, snippet)
    match = _H1.search(body)
    title = re.sub(r"<[^>]+>", "", match.group(1)) if match else page
    return (
        f"<!doctype html><html lang={lang}><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def _response(page: str, request: Request) -> HTMLResponse:
    lang = page_lang(request)
    return HTMLResponse(
        render(page, lang),
        headers={
            # Язык выбирается и по заголовку, а не только по ?lang — кэшу надо
            # это знать, иначе русскую страницу отдаст англоязычному.
            "Vary": "Accept-Language",
            "Cache-Control": "public, max-age=3600",
            "Content-Language": lang,
        },
    )


async def privacy_route(request: Request) -> HTMLResponse:
    return _response("privacy", request)


async def terms_route(request: Request) -> HTMLResponse:
    return _response("terms", request)


async def support_route(request: Request) -> HTMLResponse:
    return _response(SUPPORT_PAGE, request)


def register_routes(server: Any) -> None:
    """Повесить страницы на приложение MCP-сервера. `custom_route` кладёт роут
    без требования токена — страницы публичные по смыслу."""
    server.custom_route("/privacy", methods=["GET"])(privacy_route)
    server.custom_route("/terms", methods=["GET"])(terms_route)
    server.custom_route("/support", methods=["GET"])(support_route)
