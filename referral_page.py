"""Страница реферальной ссылки `GET /i/ref_<id>` — приглашение атлета атлетом.

Зачем: реферальная ссылка была только `t.me/<бот>?start=ref_<id>`, а человеку
с iPhone Telegram не нужен ни в одном сценарии. Теперь в приложении (`/v1/me`
→ `invite_url`) раздаётся https-ссылка на эту страницу:

- с приложением на iPhone ссылка открывает его сразу (Universal Link, путь
  `/i/*` в AASA — coach_invite_page.aasa_json), и приложение отдаёт код в
  `POST /v1/me/referral`;
- без приложения — эта страница и три выхода по весу: «Открыть в приложении»
  (схема `trainingdiary://i/<код>`), «Скачать в App Store» с кампанией
  `ct=ref_<id>` и, мелко третьей, бот (`/start ref_<id>` — та же атрибуция,
  что и раньше).

Кто зовёт, страница не говорит и в базу не ходит: код — это id автора, и
отвечать на него «такой есть» или «такого нет» значило бы стать бесплатным
оракулом по чужим аккаунтам. Поэтому и ограничителя по IP тут нет — искать
нечего. Разбор кода — acquisition.referrer_from_code, оформление — то же, что
у страницы приглашения тренера (coach_invite_page). Язык — из
`Accept-Language`, строк, вычисленных на импорте, нет.
"""

from __future__ import annotations

from html import escape
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse

import acquisition
import coach_invite_page
import config
import i18n
from handlers import sharing


def render_invite(lang: str, referrer_id: int, bot_username: str | None) -> str:
    t = lambda key, **kw: i18n.t_in(lang, key, **kw)  # noqa: E731
    code = acquisition.referral_payload(referrer_id)
    title = t("referral_page.title")
    lead = t("referral_page.lead")
    buttons = [
        f'<a class="btn primary" href="{escape(acquisition.REFERRAL_APP_LINK_PREFIX + code)}">'
        f'{escape(t("referral_page.btn.open_app"))}</a>'
    ]
    store = config.app_store_url()
    if store:
        buttons.append(
            f'<a class=btn href="{escape(acquisition.with_campaign(store, referrer_id))}">'
            f'{escape(t("referral_page.btn.app_store"))}</a>'
        )
    body = f"<h1>{escape(title)}</h1><p>{escape(lead)}</p>" + "".join(buttons)
    if store:
        body += f'<p class=muted>{escape(t("referral_page.manual"))}</p>'
    if bot_username:
        # Третьей и мелко: бот — запасной путь, а не главный.
        tg = acquisition.referral_link(bot_username, referrer_id)
        body += (
            f'<p class=muted><a class="btn small" href="{escape(tg)}">'
            f'{escape(t("referral_page.btn.telegram"))}</a></p>'
        )
    return coach_invite_page._page(lang, title, body, description=lead)


def render_dead_end(lang: str) -> str:
    title = i18n.t_in(lang, "referral_page.not_found.title")
    body = f"<h1>{escape(title)}</h1><p>{escape(i18n.t_in(lang, 'referral_page.not_found.body'))}</p>"
    store = config.app_store_url()
    if store:
        body += f'<a class="btn primary" href="{escape(store)}">{escape(i18n.t_in(lang, "referral_page.btn.app_store"))}</a>'
    return coach_invite_page._page(lang, title, body)


async def invite_route(request: Request) -> HTMLResponse:
    lang = coach_invite_page._lang(request)
    referrer_id = acquisition.referrer_from_code(request.path_params.get("code", ""))
    if referrer_id is None:
        return coach_invite_page._html(render_dead_end(lang), lang, 404)
    return coach_invite_page._html(render_invite(lang, referrer_id, sharing._bot_username), lang)


def register_routes(server: Any) -> None:
    server.custom_route(f"{acquisition.REFERRAL_WEB_PATH}{{code}}", methods=["GET"])(invite_route)
