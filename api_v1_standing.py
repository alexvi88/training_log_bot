"""REST `/v1` для «Места в зале» (crowd_standing.py) — только чтение.

* `GET /v1/exercises/{id}/standing?basis=abs|rel` — объект `Standing` одного
  упражнения атлета; чужое упражнение — 404, как у остальных ручек упражнений.
* `GET /v1/standing?basis=abs|rel` — `{"basis", "intro", "items": [Standing]}`:
  упражнения атлета с подходами за 90 дней (только каталог), доступные первыми.

`Standing`: available, reason (null | few_athletes | no_data | not_catalog),
exercise_id, name, metric (e1rm | reps), basis, above_median, top,
stronger_than, label, value_text, cohort_text, hint, histogram (20 долей
0..1 или []), you_bin, axis_min, axis_median, axis_max — все ключи всегда; плюс
rel_available (можно ли сравнить к весу тела). Запрошенный `rel`, который
посчитать нельзя (повторы, нет веса тела, мало атлетов с весом), приходит как
`abs` — поле `basis` говорит, что показано на самом деле.

Сколько атлетов в корзине, ответ не говорит ни числом, ни ступенью (решение
владельца). Тексты — на языке атлета: язык выставляет authed_user.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.routing import Route

import crowd_standing
import db
from api_v1_common import ApiError, JSONResponse, authed_user


def _basis(request: Request) -> str:
    raw = request.query_params.get("basis", "abs")
    if raw not in crowd_standing.BASES:
        raise ApiError(400, "bad_standing_basis", "basis must be abs or rel", key="api.error.bad_standing_basis")
    return raw


async def exercise_standing(request: Request) -> JSONResponse:
    user_id, user = await authed_user(request)
    basis = _basis(request)
    exercise = await db.get_exercise(int(request.path_params["exercise_id"]))
    if exercise is None or exercise["user_id"] != user_id:
        raise ApiError(404, "not_found", "exercise not found")
    return JSONResponse(await crowd_standing.exercise_standing(user, exercise, basis))


async def standing_overview(request: Request) -> JSONResponse:
    _user_id, user = await authed_user(request)
    return JSONResponse(await crowd_standing.overview(user, _basis(request)))


routes = [
    Route("/exercises/{exercise_id:int}/standing", exercise_standing, methods=["GET"]),
    Route("/standing", standing_overview, methods=["GET"]),
]
