"""Ссылка «поделиться гайдом» `GET /g/<slug>` — гайд другу.

Зачем: в приложении гайд — сторис (`GET /v1/guides`), а другу нужна одна
https-ссылка, которая сработает у кого угодно. Её отдаёт `/v1/guides` полем
`share_url` (guide_stories.share_url), и сервер решает по устройству:

- iPhone с приложением сюда не доходит вовсе: путь `/g/*` в AASA
  (coach_invite_page._universal_link_paths), iOS открывает гайд в приложении;
- iPhone/iPad без приложения → App Store с кампанией `ct=guide_<slug>`
  (acquisition.with_campaign_token, App Store Connect считает её отдельно);
  ссылки на App Store нет — статья сайта, как у всех остальных;
- всё прочее (компьютер, Android, превью мессенджеров) → статья сайта
  `/<язык>/guides/<slug>` на языке из `?lang=` или `Accept-Language` — как
  у корня сайта (legal_pages.page_lang): без заголовка — английский.

Своих строк нет: незнакомый адрес отвечает страницей «не найдено» сайта на
языке браузера (404). Переход пишется в счётчик сайта (site_stats) ключом
`/g` — одной строкой на все гайды, куда ушёл — в `target` (appstore|site).
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import acquisition
import config
import guide_stories
import legal_pages
import seo_pages
import site_stats

# Ключ в счётчике сайта (site_events): один на все `/g/<slug>`.
PAGE_KEY = "/g"
CAMPAIGN_PREFIX = "guide_"


def campaign(slug: str) -> str:
    """Кампания App Store `guide_<slug>` — не длиннее потолка Apple."""
    return f"{CAMPAIGN_PREFIX}{slug}"[:acquisition.APP_STORE_CAMPAIGN_MAX_LEN]


def site_path(slug: str, lang: str) -> str:
    """Статья сайта на языке `lang` (чужой сайту язык — английский)."""
    return seo_pages.url_for(seo_pages.Page("guide", slug), guide_stories.site_lang(lang))


async def guide_share_route(request: Request) -> Response:
    lang = guide_stories.site_lang(legal_pages.page_lang(request))
    slug = request.path_params.get("slug", "")
    if slug not in guide_stories.ORDER:
        return await seo_pages._not_found_route(lang)(request)
    device, _ = site_stats.classify_ua(request.headers.get("user-agent"))
    store = config.app_store_url()
    if device == "ios" and store:
        target, location = "appstore", acquisition.with_campaign_token(store, campaign(slug))
    else:
        target, location = "site", site_path(slug, lang)
    await seo_pages._count("view", PAGE_KEY, lang, request, target=target)
    return RedirectResponse(location, status_code=302, headers={
        "Cache-Control": "no-store", "Vary": "User-Agent, Accept-Language",
    })


def register_routes(server: Any) -> None:
    server.custom_route(f"{guide_stories.SHARE_PATH}{{slug}}", methods=["GET"])(guide_share_route)
