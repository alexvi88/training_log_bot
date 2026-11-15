"""REST `/v1` для раздела «Гайды» (сторис внизу «Меню» приложения) — только
чтение.

Статьи сайта слайдами, полки и короткий ряд «Меню» собирает guide_stories;
здесь — только вход и ответ. Язык — users.lang (authed_user ставит его на
весь запрос): тексты статей, подписи кнопок и полок, личные строки (плато,
новичок, объём) — на языке атлета, ссылка «Читать полностью» — страница сайта
на том же языке. Та же статья целиком для чтения внутри приложения —
`/guides/{slug}/article` (guide_stories.article).
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.routing import Route

import guide_stories
import i18n
from api_v1_common import ApiError, JSONResponse, authed_user


async def get_guides(request: Request) -> JSONResponse:
    """`{"guides": [...], "menu": [slug…], "shelves": [{key, title, slugs}]}`.

    У каждого гайда: slug, title, card, cover, featured (у одного или ни у
    кого — он первый), reason (строка или null), url, share_url
    (`<сайт>/g/<slug>` — ссылка «поделиться», строка всегда), slides. Слайд — kind
    cover|section|takeaways, title, text, image, chip (строка или null),
    items (строки), action (null или kind, label, exercise_id, question —
    все четыре всегда, ненужные — null). Пути картинок относительные
    (`/media/exercises/<кадр>`), как у остальных ручек."""
    user_id, user = await authed_user(request)
    with i18n.use_lang(user["lang"]):
        return JSONResponse(await guide_stories.build_response(user_id, user))


async def get_guide_article(request: Request) -> JSONResponse:
    """«Читать полностью» внутри приложения: статья сайта целиком.

    `{"slug", "title" (h1), "lead" (текст без разметки), "cover" (та же
    обложка, что у сторис), "sections": [{"title", "html"}], "takeaways":
    [строки], "url" (страница сайта, как url у гайда в /guides)}`. Разделы —
    все, что показывает сайт, по порядку; `html` — подмножество p, ul, ol, li,
    b, strong, i, em, a[href], br, ссылки — абсолютные https. Нет такой
    статьи — 404 `guide_not_found`."""
    _user_id, user = await authed_user(request)
    slug = request.path_params["slug"]
    body = guide_stories.article(slug, user["lang"] or "en")
    if body is None:
        raise ApiError(404, "guide_not_found", f"unknown guide {slug!r}")
    return JSONResponse(body)


routes = [
    Route("/guides", get_guides, methods=["GET"]),
    Route("/guides/{slug}/article", get_guide_article, methods=["GET"]),
]
