"""REST `/v1` для оценки прогресса по неделям — только чтение.

Формула — progress_score (там же почему так); здесь только перевод в JSON и
подписи. Тексты приходят уже на языке атлета (authed_user ставит язык на весь
запрос, плюс явный use_lang тем же приёмом, что api_v1_weekly). Имена
упражнений — `display_name` атлета, группы — каталожные, переведённые здесь.
"""

from __future__ import annotations

from typing import Any, Optional

from starlette.requests import Request
from starlette.routing import Route

import i18n
import progress_score
import seed_data
import timeutil
from api_v1_common import ApiError, JSONResponse, authed_user

DEFAULT_WEEKS = 12
MAX_WEEKS = 26


def _lever_text(lever: Optional[progress_score.Lever]) -> Optional[str]:
    if lever is None:
        return None
    params = dict(lever.params)
    if "group" in params:
        params["group"] = seed_data.localized_muscle_group_name(params["group"], i18n.get_lang())
    return i18n.t(f"progress_score.lever.{lever.kind}", **params)


def _pct(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def week_json(w: progress_score.WeekScore) -> dict[str, Any]:
    tier = w.tier
    note: Optional[str]
    if w.score is None:
        note = i18n.t("progress_score.not_enough")
    elif w.deload:
        note = i18n.t("progress_score.deload")
    elif w.provisional:
        note = i18n.t("progress_score.provisional")
    else:
        note = None
    return {
        "week_start": w.week_start.isoformat(),
        "score": w.score,
        "provisional": w.provisional,
        "deload": w.deload,
        "sessions": w.sessions,
        "tier": tier,
        "tier_title": i18n.t(f"progress_score.tier.{tier}") if tier else None,
        "parts": [
            {
                "key": p.key,
                "title": i18n.t(f"progress_score.part.{p.key}"),
                "points": p.points,
                "max": p.max,
            }
            for p in w.parts
        ],
        "note": note,
        "lever": _lever_text(w.lever),
        "best_lift": (
            i18n.t("progress_score.best_lift", exercise=w.best_lift[0], pct=_pct(w.best_lift[1]))
            if w.best_lift else None
        ),
        "weak_lift": (
            i18n.t("progress_score.weak_lift", exercise=w.weak_lift[0], pct=_pct(w.weak_lift[1]))
            if w.weak_lift else None
        ),
    }


async def get_progress_score(request: Request) -> JSONResponse:
    """Оценки последних `?weeks=` недель (по умолчанию 12, не больше 26),
    старые сначала. `current` — оценка текущей недели (предварительная),
    `delta` — последняя законченная против предыдущей, `best` — лучшая в ряду.
    Пустой `weeks` — законченных тренировок нет вовсе."""
    user_id, user = await authed_user(request)
    raw = request.query_params.get("weeks")
    weeks = DEFAULT_WEEKS
    if raw is not None:
        if not raw.isdigit() or not 1 <= int(raw) <= MAX_WEEKS:
            raise ApiError(400, "bad_weeks", f"weeks must be 1..{MAX_WEEKS}", key="api.error.bad_weeks")
        weeks = int(raw)
    today = timeutil.user_today(user)
    with i18n.use_lang(user["lang"]):
        scored = await progress_score.collect(user_id, user, today, weeks)
        items = [week_json(w) for w in scored]
    finished = [w.score for w in scored if not w.provisional and w.score is not None]
    current = scored[-1].score if scored and scored[-1].provisional else None
    delta = round(finished[-1] - finished[-2], 1) if len(finished) >= 2 else None
    graded = [w.score for w in scored if w.score is not None]
    return JSONResponse({
        "weeks": items,
        "current": current,
        "last": finished[-1] if finished else None,
        "delta": delta,
        "best": max(graded) if graded else None,
    })


routes = [
    Route("/progress/score", get_progress_score, methods=["GET"]),
]
