"""REST `/v1` для экрана «Итог недели» (домен: сводка недели) — только чтение.

Экран открывается из воскресного пуша (push_ios.ios_route кладёт в маршрут
`week` — понедельник недели, про которую пуш) и показывает одну неделю пн–вс.
Числа считает weekly_summary.collect, здесь — только перевод в JSON.

Тексты приходят уже локализованными, как у /dashboard: вердикт, подписи
рекордов, имена групп, подсказка. Язык — из users.lang (authed_user ставит его
на весь запрос, плюс явный use_lang тем же приёмом, что api_v1_dashboard).
Числа идут числами, чтобы клиент мог сравнивать и рисовать полосы сам.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.routing import Route

import analytics
import i18n
import timeutil
import weekly_summary
from api_v1_common import ApiError, JSONResponse, authed_user, parse_date


def summary_json(s: weekly_summary.WeeklySummary) -> dict[str, Any]:
    return {
        "week_start": s.week_start.isoformat(),
        "week_end": s.week_end.isoformat(),
        "closed": s.closed,
        "verdict": {
            "kind": s.verdict.kind,
            "greeting": s.verdict.greeting,
            "title": s.verdict.title,
            "body": s.verdict.body,
        },
        "workouts": {
            "this": s.workouts_this,
            "last": s.workouts_last,
            "usual": s.workouts_usual,
            "days": s.days,
        },
        "tonnage": {
            "this": round(s.tonnage_this, 1),
            "last": round(s.tonnage_last, 1),
            "label": s.tonnage_label,
            "last_label": s.tonnage_last_label,
            "delta_pct": s.tonnage_delta_pct,
        },
        "records": {"count": len(s.records), "items": s.records},
        "volume": {
            "min": analytics.WEEKLY_VOLUME_MIN,
            "max": analytics.WEEKLY_VOLUME_MAX,
            "rows": s.volume_rows,
        },
        "top_lift": s.top_lift,
        "streak_weeks": s.streak_weeks,
        "hint": s.hint,
        "next_day": s.next_day,
        "coach_text": s.coach_text,
    }


async def get_weekly_summary(request: Request) -> JSONResponse:
    """Итог недели `?week=YYYY-MM-DD` (любой день недели — берётся её
    понедельник), без параметра — последняя законченная неделя, а если атлета
    в ней ещё не было (первая тренировка позже её воскресенья) — текущая.

    `null` целиком, если законченных тренировок у атлета нет вовсе — как у
    /dashboard: у новичка итога нет, и клиент зовёт начать, а не рисует нули.
    Тот же `null` — у недели, которая целиком раньше первой тренировки.
    Неделя, которая ещё не началась, — 400 с текстом на языке атлета.
    """
    user_id, user = await authed_user(request)
    today = timeutil.user_today(user)
    raw = request.query_params.get("week")
    if raw:
        week = weekly_summary.week_monday(parse_date(raw, "week"))
        if week > today:
            raise ApiError(400, "weekly_future", "week has not started yet", key="api.error.weekly_future")
    else:
        first_day = await weekly_summary.first_workout_day(user_id, user)
        week = weekly_summary.default_week(today, first_day)
    with i18n.use_lang(user["lang"]):
        summary = await weekly_summary.collect(user_id, week, user)
        return JSONResponse(summary_json(summary) if summary is not None else None)


routes = [
    Route("/weekly-summary", get_weekly_summary, methods=["GET"]),
]
