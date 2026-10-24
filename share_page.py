"""Публичная страница визитки `GET /p/{token}` и её картинка для превью
`GET /og/share/{lang}/{token}.png`.

Зачем: «Поделиться программой» отдавало только `t.me/<бот>?start=sh_<токен>`,
и получатель с iPhone попадал в Telegram, а не в приложение. Теперь у визитки
есть https-адрес на сервере (`sharing.web_link`, поле `web_url` в `/v1`):

- с приложением на iPhone ссылка открывает его сразу (Universal Link, путь
  `/p/*` в AASA — coach_invite_page.aasa_json);
- без него — эта страница: что прислали (название, дни, упражнения со
  схемами — ровно из снапшота, ничего не досочиняем) и три выхода —
  «Открыть в приложении» (схема `trainingdiary://share/<токен>`: Universal
  Link на тот же домен из Safari приложение не открывает), «Скачать в App
  Store» (с кампанией `ct=share_<вид>`) и «Открыть в Telegram» (тот же deep
  link, что у кнопки визитки в боте).

Наружу — только то, что владелец сам отдал в снапшот: название, описание,
состав. Ни имени, ни username владельца на странице нет — она публичная.
Токен — 11 символов urlsafe (db.create_shared_item), перебор безнадёжен, а
потолок по IP мешал бы ботам превью мессенджеров, которые ходят с общих
адресов, поэтому его здесь нет. Отозванная визитка — строка удалена, страница
и картинка отвечают 404.

Язык — `?lang=`, затем `Accept-Language`, а без заголовка (так ходят боты
превью) — язык владельца: превью видит прежде всего его же собеседник.
Названия упражнений из каталога показываются на языке страницы, имена владельца
— как набраны (handlers.sharing.shown_exercise_name).
"""

from __future__ import annotations

import asyncio
import json
import re
from html import escape
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

import config
import db
import i18n
import seo_static
from coach_invite_page import _STYLE
from handlers import sharing

OG_PATH = "/og/share/{lang}/{token}.png"
# Токен db.create_shared_item — secrets.token_urlsafe: буквы, цифры, `-` и `_`.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

_EXTRA_STYLE = """
h2 { font-size: 19px; margin: 0 0 8px; overflow-wrap: anywhere; }
.kicker { margin: 0 0 8px; font-size: 15px; color: #c9ab94; }
.day { margin: 0 0 14px; padding: 16px; background: #2a1b16; border: 1px solid #5b3426; border-radius: 16px; }
.day ol { margin: 0; padding-left: 22px; }
.day li { margin: 0 0 4px; overflow-wrap: anywhere; }
.target { color: #c9ab94; white-space: nowrap; }
.actions { margin-top: 24px; }
"""


def _valid_token(token: str) -> bool:
    return bool(_TOKEN_RE.match(token))


async def _lang(request: Request, owner_id: Optional[int]) -> str:
    asked = request.query_params.get("lang")
    if asked in i18n.SUPPORTED:
        return asked
    header = request.headers.get("accept-language")
    if header:
        return i18n.lang_from_accept_language(header)
    if owner_id is not None:
        owner = await db.get_user(owner_id)
        if owner is not None and owner["lang"] in i18n.SUPPORTED:
            return owner["lang"]
    return i18n.lang_from_accept_language(None)


def app_store_link(kind: str) -> Optional[str]:
    """App Store с кампанией `ct=share_<вид>` поверх того, что уже в ссылке
    (там provider token `pt`, без него Apple кампанию не считает)."""
    url = config.app_store_url()
    if url is None:
        return None
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "ct"]
    query.append(("ct", f"share_{kind}"))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _exercises_total(kind: str, payload: dict[str, Any]) -> int:
    if kind == "program":
        return sum(len(day["exercises"]) for day in payload["days"])
    return len(payload.get("exercises") or [])


def summary(kind: str, payload: dict[str, Any]) -> str:
    """Короткая строка «что прислали»: под заголовком, в og:description и на
    картинке. Только счёт того, что лежит в снапшоте. Язык — текущий."""
    if kind == "program":
        in_card, _total = sharing.program_days_totals(payload)
        return i18n.t(
            "share_page.summary.program",
            days=i18n.t("btn.program_days", n=in_card),
            exercises=i18n.t("share_page.exercises", n=_exercises_total(kind, payload)),
        )
    if kind == "routine":
        return i18n.t("share_page.summary.routine", exercises=i18n.t("share_page.exercises", n=_exercises_total(kind, payload)))
    group = sharing.shown_group_name(payload.get("group"))
    return i18n.t("share_page.summary.exercise_group", group=group) if group else i18n.t("share_page.summary.exercise")


def _exercise_items(exercises: list[dict[str, Any]]) -> str:
    items = []
    for ex in exercises:
        target = sharing.shown_target(ex.get("target"))
        tail = f' <span class=target>— {escape(target)}</span>' if target else ""
        items.append(f"<li>{escape(sharing.shown_exercise_name(ex['name']))}{tail}</li>")
    return f"<ol>{''.join(items)}</ol>"


def _content(kind: str, payload: dict[str, Any]) -> str:
    if kind == "program":
        blocks = [
            f'<section class=day><h2>{escape(day["name"])}</h2>{_exercise_items(day["exercises"])}</section>'
            for day in payload["days"]
        ]
        note = sharing._omitted_days_note(payload)
        if note:
            blocks.append(f"<p class=muted>{escape(note)}</p>")
        return "".join(blocks)
    if kind == "routine":
        return f"<section class=day>{_exercise_items(payload['exercises'])}</section>"
    return ""


def _description(kind: str, payload: dict[str, Any]) -> str:
    text = (payload.get("description") or "").strip() if kind in ("program", "exercise") else ""
    return f"<p>{escape(text)}</p>" if text else ""


def _head(lang: str, title: str, description: str, *, image: Optional[str] = None, url: Optional[str] = None) -> str:
    meta = [
        '<meta name=robots content="noindex">',
        '<meta property="og:type" content="website">',
        f'<meta property="og:title" content="{escape(title)}">',
        f'<meta property="og:description" content="{escape(description)}">',
        f'<meta name=description content="{escape(description)}">',
    ]
    if url:
        meta.append(f'<meta property="og:url" content="{escape(url)}">')
    if image:
        meta += [
            f'<meta property="og:image" content="{escape(image)}">',
            f'<meta property="og:image:width" content="{seo_static.OG_SIZE[0]}">',
            f'<meta property="og:image:height" content="{seo_static.OG_SIZE[1]}">',
            '<meta name="twitter:card" content="summary_large_image">',
        ]
    return (
        f"<!doctype html><html lang={lang}><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        + "".join(meta)
        + f"<title>{escape(title)}</title><style>{_STYLE}{_EXTRA_STYLE}</style></head>"
    )


def _shell(lang: str, head: str, body: str) -> str:
    brand = escape(i18n.t_in(lang, "bot.name"))
    return f"{head}<body><main><p class=brand>{brand}</p>{body}</main></body></html>"


def render_share(lang: str, token: str, kind: str, payload: dict[str, Any], base: str, bot_username: Optional[str]) -> str:
    """Страница визитки. `base` — https-адрес сервера без слэша (для og:image)."""
    with i18n.use_lang(lang):
        t = i18n.t
        name = sharing.shown_exercise_name(payload["name"]) if kind == "exercise" else payload["name"]
        what = summary(kind, payload)
        kicker = t(f"share_page.kicker.{kind}")
        lead = t(f"share_page.lead.{kind}")
        buttons = [
            f'<a class="btn primary" href="{escape(sharing.APP_LINK_PREFIX + token)}">'
            f'{escape(t("share_page.btn.open_app"))}</a>'
        ]
        store = app_store_link(kind)
        if store:
            buttons.append(f'<a class=btn href="{escape(store)}">{escape(t("share_page.btn.app_store"))}</a>')
        if bot_username:
            buttons.append(
                f'<a class=btn href="{escape(sharing._deep_link(bot_username, token))}">'
                f'{escape(t("share_page.btn.telegram"))}</a>'
            )
        manual = f'<p class=muted>{escape(t("share_page.manual"))}</p>' if store else ""
        body = (
            f"<p class=kicker>{escape(kicker)} · {escape(what)}</p>"
            f"<h1>{escape(name)}</h1>"
            + _description(kind, payload)
            + _content(kind, payload)
            + f"<div class=actions><p>{escape(lead)}</p>"
            + "".join(buttons)
            + manual
            + "</div>"
        )
        description = t("share_page.og_description", summary=what)
        title = f"{name} · {t('bot.name')}"
    image = f"{base}{og_path(lang, token)}"
    head = _head(lang, title, description, image=image, url=f"{base}{sharing.WEB_LINK_PATH}{token}")
    return _shell(lang, head, body)


def render_not_found(lang: str) -> str:
    title = i18n.t_in(lang, "share_page.not_found.title")
    body = f"<h1>{escape(title)}</h1><p>{escape(i18n.t_in(lang, 'share_page.not_found.body'))}</p>"
    head = _head(lang, title, i18n.t_in(lang, "share_page.not_found.body"))
    return _shell(lang, head, body)


def og_path(lang: str, token: str) -> str:
    return OG_PATH.format(lang=lang, token=token)


def og_image(lang: str, token: str, kind: str, payload: dict[str, Any]) -> bytes:
    """PNG 1200×630 той же рисовалкой, что у сайта: название и «что прислали».

    Ключ кэша — hex токена: og_png приводит ключ к нижнему регистру для имени
    файла, а токены различаются регистром. Снапшот неизменен, так что картинка
    по токену верна, пока жива визитка; после отзыва маршрут отвечает 404 раньше
    кэша."""
    with i18n.use_lang(lang):
        name = sharing.shown_exercise_name(payload["name"]) if kind == "exercise" else payload["name"]
        footer = f"{i18n.t(f'share_page.kicker.{kind}')} · {summary(kind, payload)}"
        eyebrow = i18n.t("bot.name")
    key = f"share-{lang}-{token.encode().hex()}"
    return seo_static.og_png(key, name, eyebrow, footer)


async def _load(token: str) -> Optional[tuple[Any, dict[str, Any]]]:
    if not _valid_token(token):
        return None
    row = await db.get_shared_item(token)
    if row is None:
        return None
    payload = json.loads(row["payload"])
    payload.setdefault("v", 0)
    return row, payload


def _html(content: str, lang: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        content,
        status_code=status,
        headers={
            "Vary": "Accept-Language",
            "Content-Language": lang,
            # Владелец может отозвать ссылку в любую минуту — кэш не держит.
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex",
        },
    )


def _base(request: Request) -> str:
    return config.public_base_url() or str(request.base_url).rstrip("/")


async def share_route(request: Request) -> HTMLResponse:
    token = request.path_params.get("token", "")
    found = await _load(token)
    if found is None:
        lang = await _lang(request, None)
        return _html(render_not_found(lang), lang, 404)
    row, payload = found
    lang = await _lang(request, row["owner_id"])
    page = render_share(lang, token, row["kind"], payload, _base(request), sharing._bot_username or config.BOT_USERNAME)
    return _html(page, lang)


async def og_route(request: Request) -> Response:
    lang = request.path_params.get("lang", "")
    token = request.path_params.get("token", "")
    found = await _load(token) if lang in i18n.SUPPORTED else None
    if found is None:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    row, payload = found
    data = await asyncio.to_thread(og_image, lang, token, row["kind"], payload)
    return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


def register_routes(server: Any) -> None:
    server.custom_route(f"{sharing.WEB_LINK_PATH}{{token}}", methods=["GET"])(share_route)
    server.custom_route(OG_PATH, methods=["GET"])(og_route)
