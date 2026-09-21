"""Общее для всех модулей REST-слоя `/v1`: ошибки, аутентификация, разбор тела.

Выделено из api_v1.py, когда доменов стало больше одного. Причина не в
размере файла: модули доменов (`api_v1_programs`, `api_v1_food`, ...)
должны импортировать эти помощники, а `api_v1` — собирать их маршруты, и
без отдельного модуля получился бы круговой импорт.

Логика тут по-прежнему транспортная: это не бизнес-слой, он в db.py.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
import re
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

import db
import parser
import timeutil

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


# ---------- медиавложения (data: URL) ----------
#
# Разбор общий для всех вложений `/v1`: голоса (api_v1_voice.py, POST /ai/voice
# и POST /workouts/{id}/sets/voice), фото и видео вопроса тренеру (api_v1_ai.py,
# POST /ai/ask и POST /ai/video). Формат один и тот же — `data:<mime>;base64,...`
# в обычном JSON-теле, без multipart (см. докстринг api_v1_voice.py, почему).
# Разрешённый список MIME и потолок байт у каждого вложения свои, поэтому
# здесь только разбор строки и base64, а не готовое решение «слишком большое».
_DATA_URL_RE = re.compile(r"^data:([^;,]+);base64,(.+)$", re.DOTALL)


def decode_data_url(
    data_url: str,
    extension_by_mime: dict[str, str],
    *,
    field: str = "data_url",
    max_bytes: Optional[int] = None,
    too_big_error: Optional[tuple[int, str, str]] = None,
) -> tuple[bytes, str, str]:
    """`data:<mime>;base64,<payload>` → (сырые байты, mime, расширение по списку).

    `extension_by_mime` — что вызывающий готов принять; MIME не из списка —
    415, а не 400: тело запроса синтаксически валидно, просто формат вложения
    не поддержан. `field` — только для текста ошибки, чтобы «фото» и «видео»
    не путались в одном логе.

    `max_bytes`/`too_big_error` — необязательная проверка потолка размера,
    тем же кодом/текстом ошибки, что раньше собирал каждый вызывающий сам
    ПОСЛЕ декодирования (`(status, code, message)`, ровно то, что уходит в
    `ApiError(*too_big_error)`). Смысл лимитов не меняется — только момент
    проверки: сперва оценка декодированного размера по длине самой base64-
    строки, ДО base64.b64decode, а не после того, как заведомо огромное
    вложение уже целиком легло в память. Base64 кодирует 3 байта в 4 символа
    (плюс паддинг), поэтому декодированный размер не может быть больше
    `ceil(len(payload) * 3 / 4)` — это гарантированная верхняя оценка, а не
    догадка: она не может пропустить настоящее вложение мимо проверки, но
    может (и должна) отсечь заведомо большее, не читая его байты. Точная
    проверка после реального decode остаётся как второй барьер — на случай
    percent-подобных искажений оценки, а не потому что ей не доверяют.
    """
    match = _DATA_URL_RE.match((data_url or "").strip())
    if not match:
        raise ApiError(400, "bad_request", f"{field} must be a data: URL")
    mime = match.group(1).strip().lower()
    ext = extension_by_mime.get(mime)
    if ext is None:
        allowed = ", ".join(sorted(set(extension_by_mime.values())))
        raise ApiError(
            415,
            "unsupported_media_type",
            f"unsupported format {mime!r} in {field}; allowed extensions: {allowed}",
        )
    payload = match.group(2)
    if max_bytes is not None and too_big_error is not None:
        # Для валидного base64 (длина кратна 4, паддинг только "=" в конце)
        # это точный декодированный размер, не просто прикидка сверху — учёт
        # паддинга не даёт срезать честные вложения на самой границе лимита.
        # Искажённый (не кратный 4, "=" не на месте) base64 всё равно упадёт
        # чуть ниже, на настоящем b64decode, с тем же bad_request, что и
        # раньше — эта оценка его не подменяет, только избегает лишнего
        # decode для того, что уже видно как заведомо большое по длине строки.
        padding = 2 if payload.endswith("==") else 1 if payload.endswith("=") else 0
        estimated_bytes = (len(payload) * 3) // 4 - padding
        if estimated_bytes > max_bytes:
            raise ApiError(*too_big_error)
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise ApiError(400, "bad_request", f"invalid base64 payload in {field}") from exc
    if max_bytes is not None and too_big_error is not None and len(raw) > max_bytes:
        raise ApiError(*too_big_error)
    return raw, mime, ext


def exercise_json(row) -> dict[str, Any]:
    """Сериализация упражнения — общая для api_v1.py (CRUD своих упражнений)
    и api_v1_templates.py (форк каталожного шаблона возвращает уже свою
    заведённую копию тем же форматом, каким её потом отдаст GET /exercises)."""
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "original_name": row["original_name"],
        "primary_group_id": row["primary_group_id"],
        "equipment": row["equipment"],
        "unilateral": bool(row["unilateral"]),
        "attachment": row["attachment"],
        "bodyweight_load": row["bodyweight_load"],
        "description": row["description"],
        "is_archived": bool(row["is_archived"]),
    }


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


# ---------- даты ----------

def parse_date(raw: Any, field: str = "date") -> dt.date:
    """YYYY-MM-DD → date, иначе 400. Один разбор на весь `/v1`: строки ошибок
    у одинаковых полей должны совпадать между ручками."""
    if not isinstance(raw, str):
        raise ApiError(400, "bad_request", f"{field} must be a string YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise ApiError(400, "bad_request", f"{field} must be YYYY-MM-DD") from exc


async def reject_future_date(date: dt.date, user_id: int, field: str = "date") -> None:
    """«Завтра» не бывает ни у тренировки, ни у съеденного: и то и другое —
    запись о том, что УЖЕ произошло. Проверка одна на все такие ручки
    (backfill, перенос даты тренировки, запись еды), иначе одна из них
    неизбежно останется без неё.

    Сегодня — по часовому поясу пользователя, а не по UTC сервера: вечером в
    UTC+3 серверное «завтра» наступает на три часа раньше человеческого, и
    честная запись за сегодня отлетала бы с 400.
    """
    user = await db.get_user(user_id)
    if date > timeutil.user_today(user):
        raise ApiError(400, "bad_request", f"{field} is in the future")


# ---------- числа подхода ----------
#
# Границы — те же, что у parser.py, которым разбирается строка «100 8» и в
# боте, и в /v1 (POST /workouts/{id}/sets/text). Своих чисел здесь нет
# намеренно: живая запись, принимающая reps=1000000000, и её же редактор,
# отвергающий то же самое, — это один и тот же подход, который клиент может
# записать, но не может поправить.

def set_weight(value: Any, field: str = "weight") -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ApiError(400, "bad_request", f"{field} must be a number")
    weight = float(value)
    # 0 — это не «пусто», а честный вес собственного тела (подтягивания).
    if weight < 0:
        raise ApiError(400, "bad_request", f"{field} must not be negative")
    if weight > parser.MAX_WEIGHT:
        raise ApiError(400, "bad_request", f"{field} must be at most {parser.MAX_WEIGHT:.0f}")
    return weight


def set_reps(value: Any, field: str = "reps") -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ApiError(400, "bad_request", f"{field} must be a positive int")
    if value > parser.MAX_REPS:
        raise ApiError(400, "bad_request", f"{field} must be at most {parser.MAX_REPS}")
    return value


def set_rpe(value: Any, field: str = "rpe") -> Optional[float]:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ApiError(400, "bad_request", f"{field} must be a number or null")
    rpe = float(value)
    # Та же шкала, что у parser._parse_rpe: RPE — это 0…10, «99» означает
    # опечатку, а не невероятное усилие.
    if not (0 < rpe <= 10):
        raise ApiError(400, "bad_request", f"{field} must be between 0 and 10")
    return rpe
