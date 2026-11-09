"""REST `/v1` для «Года в подвале» (year_review.py) — только чтение.

`GET /year-review?year=YYYY` — числа года и строки слайдов уже на языке
атлета (язык ставит authed_user из users.lang), плюс ссылка для шаринга с
кампанией `ct=year_review`. Без параметра — текущий год атлета. `null`
целиком — за год нет законченных тренировок: экран зовёт начать, а не рисует
нули (как /dashboard и /weekly-summary).

`GET /year-review/slides/{index}.png?year=YYYY` — та же картинка, что бот
присылает альбомом: приложение показывает слайды как есть и отдаёт их в
«Поделиться», а не рисует вторую копию колоды на Swift.
"""

from __future__ import annotations

from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

import i18n
import timeutil
import year_review
from api_v1_common import ApiError, JSONResponse, authed_user

FIRST_YEAR = 2015


def _year(request: Request, user: Any) -> int:
    today = timeutil.user_today(user)
    raw = request.query_params.get("year")
    if raw is None:
        return today.year
    try:
        year = int(raw)
    except ValueError:
        year = 0
    if not FIRST_YEAR <= year <= today.year:
        raise ApiError(400, "year_invalid", "year is outside the diary", key="api.error.year_invalid")
    return year


def _slide_kind(slide: year_review.Slide) -> str:
    if isinstance(slide, year_review.StatSlide):
        return "stat"
    if isinstance(slide, year_review.fs.BarbellSlide):
        return "barbell"
    return "body"


def review_json(facts: year_review.YearFacts, deck: year_review.Deck, user_id: int) -> dict[str, Any]:
    lift: Optional[dict[str, Any]] = None
    if facts.lift is not None:
        lift = {
            "exercise": facts.lift.exercise,
            "before": round(facts.lift.before, 1),
            "after": round(facts.lift.after, 1),
            "growth_pct": facts.lift.growth_pct,
        }
    return {
        "year": facts.year,
        "short": facts.short,
        "workouts": facts.workouts,
        "active_weeks": facts.active_weeks,
        "best_streak_weeks": facts.best_streak,
        "sets": facts.sets,
        "tonnage": {"value": round(facts.tonnage, 1), "label": year_review.tonnage_text(facts)},
        "records": facts.records,
        "top_lift": lift,
        "groups": [{"name": g.name, "sets": g.sets} for g in facts.groups],
        "weekday": facts.weekday,
        "hour": facts.hour,
        "persona": {"kind": deck.persona.kind, "title": deck.persona.title, "body": deck.persona.body},
        "slides": [
            {"index": index, "kind": _slide_kind(slide)} for index, slide in enumerate(deck.slides)
        ],
        "share_text": i18n.t("year.share.text", year=facts.year, workouts=facts.workouts),
        "share_url": year_review.share_url(user_id),
    }


async def get_year_review(request: Request) -> JSONResponse:
    user_id, user = await authed_user(request)
    year = _year(request, user)
    facts = await year_review.collect(user_id, year, user)
    if facts is None:
        return JSONResponse(None)
    with i18n.use_lang(user["lang"]):
        deck = year_review.assemble(facts)
        return JSONResponse(review_json(facts, deck, user_id))


async def get_year_review_slide(request: Request) -> Response:
    user_id, user = await authed_user(request)
    year = _year(request, user)
    index = int(request.path_params["index"])
    facts = await year_review.collect(user_id, year, user)
    if facts is None:
        raise ApiError(404, "not_found", "no workouts this year")
    with i18n.use_lang(user["lang"]):
        deck = year_review.assemble(facts)
    if not 0 <= index < len(deck.slides):
        raise ApiError(404, "not_found", "no such slide")
    import asyncio

    png = await asyncio.to_thread(year_review.render_slide, deck.slides[index], deck.signature, deck.lang)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


routes = [
    Route("/year-review", get_year_review, methods=["GET"]),
    Route("/year-review/slides/{index:int}.png", get_year_review_slide, methods=["GET"]),
]
