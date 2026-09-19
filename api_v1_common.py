"""Общее для всех модулей REST-слоя `/v1`: ошибки, аутентификация, разбор тела.

Выделено из api_v1.py, когда доменов стало больше одного. Причина не в
размере файла: модули доменов (`api_v1_programs`, `api_v1_food`, ...)
должны импортировать эти помощники, а `api_v1` — собирать их маршруты, и
без отдельного модуля получился бы круговой импорт.

Логика тут по-прежнему транспортная: это не бизнес-слой, он в db.py.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

import db

logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status_code)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("api_v1: unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"error": "internal_error", "message": "internal error"}, status_code=500)


async def authed_user_id(request: Request) -> int:
    """Bearer-токен → telegram_id, либо ApiError(401).

    Разрезолвленный id кладётся в `request.state.user_id` — оттуда его берёт
    middleware лога действий (`api_v1_activity`), которая работает уже ПОСЛЕ
    обработчика и сама токен не разбирает. Иначе за каждый запрос было бы два
    похода в базу за одним и тем же ответом, а на запрос без токена — ещё и
    лишний. Заодно это единственное место, где «кто это» вообще выясняется:
    любой новый обработчик получает пометку в ленте просто потому, что зовёт
    эту функцию.
    """
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise ApiError(401, "unauthorized", "missing bearer token")
    token = auth[len("bearer "):].strip()
    user_id = await db.resolve_api_token(token)
    if user_id is None:
        raise ApiError(401, "unauthorized", "invalid or revoked token")
    request.state.user_id = user_id
    return user_id


async def json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise ApiError(400, "bad_request", "invalid JSON body") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", "JSON object expected")
    return body


def _type_name(expected_type: type | tuple[type, ...]) -> str:
    """Имя типа для текста ошибки — и для кортежа тоже.

    `require(body, "weight", (int, float))` — обычный вызов (вес приходит и
    целым, и дробным), а у кортежа нет `__name__`: до этого хелпера строка
    собиралась прямо в f-строке, и запрос со строковым весом падал 500-й,
    хотя проверка типа как раз сработала правильно.
    """
    if isinstance(expected_type, tuple):
        return " or ".join(t.__name__ for t in expected_type)
    return expected_type.__name__


def require(body: dict[str, Any], key: str, expected_type: type | tuple[type, ...]) -> Any:
    if key not in body:
        raise ApiError(400, "bad_request", f"missing field: {key}")
    value = body[key]
    if not isinstance(value, expected_type) or isinstance(value, bool) and expected_type is not bool:
        raise ApiError(400, "bad_request", f"field {key} must be {_type_name(expected_type)}")
    return value


def optional_str(body: dict[str, Any], key: str) -> str | None:
    """Необязательная строка: отсутствует или null — None, пустая после
    strip — тоже None. Клиенту незачем различать «не прислал поле» и
    «прислал пустую строку»: и то и другое значит «нет значения»."""
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(400, "bad_request", f"field {key} must be str")
    value = value.strip()
    return value or None


def optional_int(body: dict[str, Any], key: str) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ApiError(400, "bad_request", f"field {key} must be int")
    return value


def query_int(request: Request, key: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    """Числовой query-параметр с потолком. Потолок обязателен там, где
    параметр управляет размером выборки: `?limit=1000000` иначе тянет из
    базы всё подряд на каждый запрос."""
    raw = request.query_params.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(400, "bad_request", f"{key} must be int") from exc
    if value < minimum:
        raise ApiError(400, "bad_request", f"{key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        return maximum
    return value
