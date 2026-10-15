"""REST `/v1/food*` — дневника еды больше нет нигде.

Функцию убрали целиком: приложение (iOS, «Убрал дневник еды из приложения
целиком», 2026-09-22, ещё на сборке 1), бот (/food_diary), AI-разбор еды и
инструменты тренера, MCP. Ручки оставлены только затем, чтобы самая ранняя
сборка, у которой экран еды ещё был, получила не 404 и не молчание, а 410
`food_gone` с человеческим текстом на языке атлета (`api.error.food_gone`).

Записи, которые люди успели сделать, никуда не деваются и больше не
пополняются: они в `food_entries`, уезжают в полную выгрузку (full_export,
food.csv) и стираются «Удалить аккаунт», как и раньше.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.routing import Route

import api_v1_common as common
from api_v1_common import JSONResponse

ApiError = common.ApiError


async def food_gone(request: Request) -> JSONResponse:
    """Любая `/v1/food*` → 410 `food_gone` на языке атлета."""
    await common.authed_user_id(request)
    raise ApiError(410, "food_gone", "the food diary was removed from the app")


routes = [
    Route("/food", food_gone, methods=["GET", "POST"]),
    Route("/food/days", food_gone, methods=["GET"]),
    Route("/food/parse", food_gone, methods=["POST"]),
    Route("/food/goal", food_gone, methods=["POST"]),
    Route("/food/{entry_id:int}", food_gone, methods=["GET", "DELETE"]),
]
