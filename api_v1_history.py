"""REST `/v1` для трёх экранов истории, которых не было (см. таск-лист):

- картинка-визитка тренировки для шаринга (в боте — «🖼 Картинка», кнопка
  `hist:card:{id}` в `handlers/history.py`, растр рисует `workout_card.py`);
- календарь истории по месяцам (в боте — «📅 По месяцам», поверх
  `db.list_finished_workouts_by_day_in_month`);
- экспорт всех подходов в CSV (в боте — «📤 Экспорт CSV» в настройках,
  собирает `csv_export.py`).

Все три — тонкий транспорт поверх того же кода, что и у бота: сама расчётная
логика для карточки и для CSV вынесена в `workout_card.py`/`csv_export.py`
именно затем, чтобы здесь не заводить вторую реализацию (см. докстринг
`progression_data.py` — тот же приём). Календарь и вовсе не требует
отдельного модуля: `db.list_finished_workouts_by_day_in_month` уже единственный
источник данных, которым в боте пользуется `handlers.history._show_history_calendar`.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

import api_v1_common as common
import csv_export
import db
import workout_card

ApiError = common.ApiError
_authed_user_id = common.authed_user_id


async def _owned_workout(workout_id: int, user_id: int):
    """Та же проверка владения, что и в api_v1.py: id из URL мог быть угадан
    и принадлежать чужой тренировке."""
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != user_id:
        raise ApiError(404, "not_found", "workout not found")
    return workout


# ---------- картинка-визитка тренировки ----------

async def get_workout_card(request: Request) -> Response:
    """PNG той же карточки, что бот шлёт кнопкой «🖼 Картинка».

    Реферальную ссылку сюда не прикладываем: в отличие от снапшотов программ/
    упражнений (api_v1_sharing.py), у которых нет иного способа узнать чужой
    контент, реферальная ссылка — это просто `t.me/<bot>?start=ref_<user_id>`
    (acquisition.referral_link), а `<user_id>` клиент уже знает из `/me` и
    свой собственный username бота — тоже. Второй ручкой это не сделать точнее,
    только продублировать константу REFERRAL_PREFIX.
    """
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    await _owned_workout(workout_id, user_id)
    user = await db.get_user(user_id)
    card = await workout_card.build(workout_id, user)
    if card is None:
        raise ApiError(404, "not_found", "workout not found")
    return Response(card.png, media_type="image/png")


# ---------- календарь истории по месяцам ----------

def _parse_year_month(request: Request) -> tuple[int, int]:
    raw_year = request.query_params.get("year")
    raw_month = request.query_params.get("month")
    if raw_year is None or raw_month is None:
        raise ApiError(400, "bad_request", "year and month are required")
    try:
        year, month = int(raw_year), int(raw_month)
    except ValueError as exc:
        raise ApiError(400, "bad_request", "year and month must be int") from exc
    if not (1 <= month <= 12):
        raise ApiError(400, "bad_request", "month must be between 1 and 12")
    return year, month


async def get_history_calendar(request: Request) -> Any:
    """«📅 По месяцам» — то же самое, что размечает клетки `keyboards.calendar_keyboard`
    в боте, тут же отдаётся как есть: клиент сам рисует сетку календаря.

    Пустой месяц (ни одной законченной тренировки) — это `{"days": {}}`, а не
    404 или ошибка: у месяца без тренировок ровно такой же смысл, как и у
    месяца с ними, — это часть обычного календаря, а не отсутствующий ресурс.
    """
    user_id = await _authed_user_id(request)
    year, month = _parse_year_month(request)
    by_day = await db.list_finished_workouts_by_day_in_month(user_id, year, month)
    return JSONResponse({"days": by_day})


# ---------- экспорт CSV ----------

async def export_csv(request: Request) -> Response:
    """«📤 Экспорт CSV» — все подходы всех законченных тренировок одним файлом,
    тем же форматом, что и у бота (csv_export.build_csv)."""
    user_id = await _authed_user_id(request)
    data = await csv_export.build_csv(user_id)
    return Response(
        data,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="training_log.csv"'},
    )


routes = [
    Route("/workouts/{workout_id:int}/card", get_workout_card, methods=["GET"]),
    Route("/workouts/calendar", get_history_calendar, methods=["GET"]),
    Route("/export/csv", export_csv, methods=["GET"]),
]
