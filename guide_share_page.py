"""Ссылка «поделиться гайдом» `GET /g/<slug>` — гайд другу.

Зачем: в приложении гайд — сторис (`GET /v1/guides`), а другу нужна одна
https-ссылка, которая сработает у кого угодно. Её отдаёт `/v1/guides` полем
`share_url` (guide_stories.share_url), и сервер решает по устройству:

- iPhone/iPad → страница-развилка, а не редирект: обложка, название гайда,
  строка тренера и выходы по весу — «Открыть в приложении» (схема
  `trainingdiary://g/<slug>`), «Скачать в App Store» с кампанией
  `ct=guide_<slug>` (acquisition.with_campaign_token, App Store Connect
  считает её отдельно; нет ссылки на App Store — нет и кнопки) и мелко
  «Читать на сайте». Путь `/g/*` есть в AASA
  (coach_invite_page._universal_link_paths), но дошедший сюда iPhone — не
  значит «приложения нет»: из встроенного браузера Telegram Universal Link не
  срабатывает, а CDN Apple отдаёт новый AASA с опозданием. Слепой 302 в App
  Store уводил владельца с установленным приложением в магазин;
- всё прочее (компьютер, Android, превью мессенджеров) → 302 на статью сайта
  `/<язык>/guides/<slug>`.

Язык — из `?lang=` или `Accept-Language`, как у корня сайта
(legal_pages.page_lang): без заголовка — английский. Строки страницы —
`guide_share_page.*`, кнопки — общие с реферальной страницей
(`referral_page.btn.*`), оформление — coach_invite_page. Незнакомый адрес
отвечает страницей «не найдено» сайта на языке браузера (404). Переход
пишется в счётчик сайта (site_stats) ключом `/g` — одной строкой на все
гайды, куда ушёл — в `target` (page — развилка iPhone, site — статья);
людьми развилка считается по маячку браузера (site_stats.beacon_script) тем
же ключом.
"""

from __future__ import annotations

from html import escape
from typing import Any

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import acquisition
import coach_invite_page
import config
import guide_stories
import i18n
import legal_pages
import seo_pages
import site_stats

# Ключ в счётчике сайта (site_events): один на все `/g/<slug>`.
PAGE_KEY = acquisition.GUIDE_SHARE_PAGE_KEY
CAMPAIGN_PREFIX = "guide_"
# Схема приложения: гайд открывается сторис (iOS разбирает `g/<slug>`).
APP_LINK_PREFIX = "trainingdiary://g/"


def campaign(slug: str) -> str:
    """Кампания App Store `guide_<slug>` — не длиннее потолка Apple."""
    return f"{CAMPAIGN_PREFIX}{slug}"[:acquisition.APP_STORE_CAMPAIGN_MAX_LEN]


def site_path(slug: str, lang: str) -> str:
    """Статья сайта на языке `lang` (чужой сайту язык — английский)."""
    return seo_pages.url_for(seo_pages.Page("guide", slug), guide_stories.site_lang(lang))


def cover_url(slug: str) -> str | None:
    """Абсолютный адрес обложки (раздача `/v1/media/guides/`) или None без файла."""
    path = guide_stories.cover_path(slug)
    if not path:
        return None
    return f"{config.site_base_url() or guide_stories.DEFAULT_SITE}/v1{path}"


def render_landing(slug: str, lang: str) -> str:
    t = lambda key: i18n.t_in(lang, key)  # noqa: E731
    title = t(f"seo.guide.{slug}.h1")
    lead = t("guide_share_page.lead")
    body = ""
    cover = cover_url(slug)
    if cover:
        body += (
            f'<img src="{escape(cover)}" alt="" width=1024 height=1024 '
            'style="display:block;width:100%;height:auto;margin:0 0 20px;border-radius:16px">'
        )
    body += (
        f"<h1>{escape(title)}</h1><p>{escape(lead)}</p>"
        f'<a class="btn primary" href="{escape(APP_LINK_PREFIX + slug)}">'
        f'{escape(t("referral_page.btn.open_app"))}</a>'
    )
    store = config.app_store_url()
    if store:
        body += (
            f'<a class=btn href="{escape(acquisition.with_campaign_token(store, campaign(slug)))}">'
            f'{escape(t("referral_page.btn.app_store"))}</a>'
        )
    body += (
        f'<p class=muted><a class="btn small" href="{escape(site_path(slug, lang))}">'
        f'{escape(t("guide_share_page.btn.site"))}</a></p>'
    )
    return coach_invite_page._page(lang, title, body, description=lead)


async def guide_share_route(request: Request) -> Response:
    lang = guide_stories.site_lang(legal_pages.page_lang(request))
    slug = request.path_params.get("slug", "")
    if slug not in guide_stories.ORDER:
        return await seo_pages._not_found_route(lang)(request)
    device, _ = site_stats.classify_ua(request.headers.get("user-agent"))
    if device == "ios":
        # Не редирект в App Store: приложение может стоять, а Universal Link —
        # не сработать (встроенный браузер Telegram, кэш AASA у Apple).
        await seo_pages._count("view", PAGE_KEY, lang, request, target="page")
        html = render_landing(slug, lang).replace(
            "</body>", site_stats.beacon_script(PAGE_KEY) + "</body>", 1)
        resp = coach_invite_page._html(html, lang)
        resp.headers["Vary"] = "User-Agent, Accept-Language"
        return resp
    await seo_pages._count("view", PAGE_KEY, lang, request, target="site")
    return RedirectResponse(site_path(slug, lang), status_code=302, headers={
        "Cache-Control": "no-store", "Vary": "User-Agent, Accept-Language",
    })


def register_routes(server: Any) -> None:
    server.custom_route(f"{guide_stories.SHARE_PATH}{{slug}}", methods=["GET"])(guide_share_route)
