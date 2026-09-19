"""REST `/v1` для зала славы (домен: достижения) — только чтение.

Тот же расчёт, что бот показывает текстом на экране «🏆 Достижения»
(handlers/history.py.build_hall_of_fame_text) — личные рекорды по каждому
упражнению, общий тоннаж со шуткой-эквивалентом, лучшая серия недель подряд,
самая длинная тренировка, звание. Считается он одним местом на обоих
потребителей — hall_of_fame_data.collect, — здесь остаётся только перевод его
полей в JSON, тем же приёмом, что и у api_v1_dashboard/api_v1_progress.

Тексты (шутка-эквивалент тоннажа, готовая строка «сколько не хватает до
следующего звания») приходят уже локализованными, поэтому сбор обёрнут в
i18n.use_lang(user["lang"]) — без него язык ответа был бы тем, который первым
дёрнул модуль в этом процессе (CLAUDE.md, «Ловушка, встретившаяся шесть раз»).
"""

from __future__ import annotations

from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import db
import formatting
import hall_of_fame_data
import i18n
from api_v1_common import ApiError, authed_user_id


def _lift_json(entry: tuple[str, float, int, float]) -> dict[str, Any]:
    """(имя, вес лучшего подхода, повторы, e1RM) — вес 0 значит «свой вес»,
    тем же признаком, что и formatting._hall_of_fame_lift. `record` — готовая
    строка подхода/повторов, чтобы клиенту не пересобирать русское/английское
    согласование самому."""
    name, weight, reps, e1rm = entry
    is_bodyweight = weight <= 0
    record = (
        i18n.t("progress.total_reps", n=reps)
        if is_bodyweight
        else formatting.format_set(weight, reps)
    )
    return {
        "exercise": name,
        "is_bodyweight": is_bodyweight,
        "weight": None if is_bodyweight else weight,
        "reps": reps,
        "e1rm": None if is_bodyweight else round(e1rm, 1),
        "record": record,
    }


def _rank_json(rank, gap) -> Optional[dict[str, Any]]:
    if rank is None:
        return None
    return {
        "name": rank.name,
        "level": rank.level,
        "gap_text": formatting.format_rank_gap(gap) if gap else None,
    }


def _hall_of_fame_json(hof: "hall_of_fame_data.HallOfFame") -> dict[str, Any]:
    return {
        "total_workouts": hof.total_workouts,
        "rank": _rank_json(hof.rank, hof.rank_gap),
        "tonnage": {
            # В единицах пользователя, как и остальные веса в /v1 — клиент уже
            # знает users.unit из /me и подписывает сам.
            "value": round(hof.tonnage_kg, 1),
            "equivalent": hof.tonnage_equivalent,
        },
        "best_week_streak": hof.best_week_streak,
        "longest_workout_seconds": hof.longest_workout_seconds,
        "top_lifts": [_lift_json(t) for t in hof.top_lifts],
    }


async def get_hall_of_fame(request: Request) -> JSONResponse:
    """Зал славы — или `null`, если законченных тренировок ещё нет.

    `null`, а не структура из нулей: у новичка нет ни рекордов, ни тоннажа, ни
    звания выше стартового, и приложение в этом случае показывает приглашение
    начать — тот же приём, что и `GET /dashboard` (api_v1_dashboard.get_dashboard).
    """
    user_id = await authed_user_id(request)
    user = await db.get_user(user_id)
    if user is None:
        raise ApiError(404, "not_found", "user not found")
    with i18n.use_lang(user["lang"]):
        hof = await hall_of_fame_data.collect(user_id)
        if hof.total_workouts == 0:
            return JSONResponse(None)
        return JSONResponse(_hall_of_fame_json(hof))


routes = [
    Route("/hall-of-fame", get_hall_of_fame, methods=["GET"]),
]
