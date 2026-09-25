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
import math
import re
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

import config
import db
import exercise_descriptions
import exercise_media
import exercise_photos
import i18n
import parser
import timeutil

logger = logging.getLogger(__name__)



def ai_consent_given(request: Request, user: Any) -> bool:
    """Можно ли отдавать данные этого атлета стороннему AI (App Store
    5.1.2(i), users.ai_consent_at) — одно правило на весь `/v1`.

    Проверка действует, только если клиент прислал `X-AI-Consent-Flow: 1`
    (сам показывает лист согласия) или включён config.AI_CONSENT_REQUIRED;
    иначе — True, как раньше: выпущенные сборки 1.0 (3)/(4) листа не знают
    (см. комментарий у флага).

    Ручки, где человек сам спрашивает тренера, отвечают на False 403-й
    (api_v1_ai._require_ai_consent). Фоновые вызовы модели, которые человек
    не заказывал (комментарий тренера после финиша, сопоставление названий
    при импорте), на False просто не делаются — молча, без ошибки клиенту:
    иначе отозванное согласие при включённом тумблере «🤖 Комментарии
    тренера» всё равно отправляло бы тренировку модели.
    """
    if not (
        config.AI_CONSENT_REQUIRED
        or request.headers.get(config.AI_CONSENT_CLIENT_HEADER) == "1"
    ):
        return True
    return user is not None and bool(user["ai_consent_at"])

class ApiError(Exception):
    """Ошибка `/v1`: машинный `code`, машинная `message` и человеческий текст.

    `message` — английская строка для разработчика (уходит в ответ полем
    `detail` и в лог), а не для экрана: приложение показывает поле `message`
    ответа как есть, и раньше там стояло «daily question limit reached» — и
    русскому атлету, и английскому. Человеческий текст теперь собирается в
    `api_error_handler` на языке запроса, в порядке:

      * `human` — уже готовая локализованная строка (её собрали на месте, под
        use_lang: «фото слишком большое», текст лимита из db.routine_budget);
      * `key` (+ `params`) — ключ каталога, когда у ошибки свой текст
        («вес не может быть отрицательным» — не то же самое, что общий 400);
      * `api.error.<code>` — общий текст на код;
      * `api.error.default` — если кода в каталоге нет.

    Каждый `code`, который поднимается без `human`/`key`, обязан иметь свой
    `api.error.<code>` в обоих каталогах — это держит
    tests/test_api_v1_language_invariant.py, а не память.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        human: str | None = None,
        key: str | None = None,
        **params: Any,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.human = human
        self.key = key
        self.params = params


def request_lang(request: Request) -> str:
    """Язык человека, которому уйдёт ответ: из users.lang, если запрос уже
    прошёл authed_user_id (он кладёт язык в request.state), иначе — из
    `Accept-Language` (вход, привязка: пользователя ещё нет)."""
    lang = getattr(request.state, "lang", None)
    if lang in i18n.SUPPORTED:
        return lang
    return i18n.lang_from_accept_language(request.headers.get("accept-language"))


# Код ошибки → уже существующий текст бота, когда у бота на этот случай
# свой текст есть (продукт говорит одним голосом, и приложение показывает
# ровно то, что бот). Остальные коды — `api.error.<code>`.
ERROR_KEY_BY_CODE: dict[str, str] = {
    "invalid_code": "oauth.error_bad_code",
    "question_limit_exceeded": "limit.question",
    "video_limit_exceeded": "limit.video.generic",
    "food_limit_exceeded": "limit.food.generic",
    "spend_limit_exceeded": "limit.spend_hard",
    "busy": "ai.screen.busy",
    "save_failed": "ai.screen.save_failed_new",
    "draft_not_found": "ai.screen.program_gone",
    "unparsed_input": "input.examples_hint",
    "import_in_progress": "import.already_uploading",
    "no_sets_found": "import.no_sets_found",
    "routine_limit_reached": "api.error.routine_limit",
    "routine_budget_exceeded": "api.error.routine_limit",
    "name_conflict": "api.error.name_taken",
}


def error_key_for_code(code: str) -> str | None:
    """Ключ каталога для кода ошибки, или None, если своего текста у кода нет."""
    key = ERROR_KEY_BY_CODE.get(code) or f"api.error.{code}"
    return key if key in i18n.catalog_keys() else None


def human_error_message(exc: ApiError, lang: str) -> str:
    if exc.human:
        return exc.human
    if exc.key:
        return i18n.t_in(lang, exc.key, **exc.params)
    return i18n.t_in(lang, error_key_for_code(exc.code) or "api.error.default")


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        {
            "error": exc.code,
            "message": human_error_message(exc, request_lang(request)),
            "detail": exc.message,
        },
        status_code=exc.status_code,
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("api_v1: unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        {
            "error": "internal_error",
            "message": i18n.t_in(request_lang(request), "api.error.internal_error"),
            "detail": "internal error",
        },
        status_code=500,
    )


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
    # Сам токен — для POST /auth/logout: он гасит только токен этого
    # устройства, а не все токены человека.
    request.state.api_token = token
    user = await db.get_user(user_id)
    # Строка пользователя — на весь запрос: язык берётся из неё здесь же, а
    # обработчикам, которым она нужна сразу после входа, незачем читать её из
    # базы второй раз (см. authed_user). Изменил пользователя по ходу
    # обработчика — перечитай сам: это снимок на момент входа.
    request.state.user = user
    request.state.lang = _set_request_lang(user)
    return user_id


async def authed_user(request: Request) -> tuple[int, Any]:
    """То же, что authed_user_id, плюс строка `users` — уже прочитанная при
    входе, без второго похода в базу. 404, если строки нет (токен пережил
    пользователя — так бывает только посреди сноса аккаунта)."""
    user_id = await authed_user_id(request)
    user = request.state.user
    if user is None:
        raise ApiError(404, "not_found", "user not found")
    return user_id, user


def _set_request_lang(user: Any) -> str:
    """Язык ответа на весь этот запрос — из users.lang.

    У бота язык выставляет middleware на каждый апдейт (main.py); у /v1 такого
    слоя не было, и любой обработчик, забывший свой `with i18n.use_lang(...)`,
    отвечал англоязычному атлету по-русски — дефолт ContextVar. Так протекали
    ответы тренера (языковой хвост промпта), описания техники, ошибки
    «фото слишком большое». Раз «кто это» выясняется только здесь, язык
    выставляется здесь же, и новому обработчику забыть его уже нечем.

    Обычный `set_lang`, а не `use_lang`: сбросить значение некому, и не нужно —
    обработчик Starlette идёт в своей задаче (BaseHTTPMiddleware
    api_v1_activity запускает приложение отдельной задачей, uvicorn — задачей
    на запрос), а задача живёт в копии контекста, так что язык не утекает ни в
    соседний запрос, ни наружу. Явные `with i18n.use_lang(...)` в модулях
    остаются: они не мешают и держат язык там, где функцию зовут не из запроса.
    """
    lang = user["lang"] if user is not None and user["lang"] in i18n.SUPPORTED else i18n.DEFAULT_LANG
    i18n.set_lang(lang)
    return lang


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
            raise _too_big(too_big_error)
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise ApiError(400, "bad_request", f"invalid base64 payload in {field}") from exc
    if max_bytes is not None and too_big_error is not None and len(raw) > max_bytes:
        raise _too_big(too_big_error)
    return raw, mime, ext


def _too_big(error: tuple[int, str, str]) -> ApiError:
    """`too_big_error` несёт уже локализованный текст (его собирают под
    use_lang у вызывающих) — он и есть человеческое сообщение."""
    status, code, text = error
    return ApiError(status, code, f"{code}: payload too large", human=text)


def exercise_json(row, last_set: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Сериализация упражнения — общая для api_v1.py (CRUD своих упражнений)
    и api_v1_templates.py (форк каталожного шаблона возвращает уже свою
    заведённую копию тем же форматом, каким её потом отдаст GET /exercises).

    `last_set` считается пачкой на весь список (db.last_best_set_by_exercise),
    поэтому приходит снаружи — отдавать его с полями ручки должны через
    exercises_json, а не звать эту функцию по строке."""
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
        # Есть ли что показать на карточке — своё описание или встроенное из
        # каталога. Та же проверка, что ставит «📝 » у кнопки упражнения в
        # боте (handlers/exercises._exercise_list_label): сам текст отдаёт
        # GET /exercises/{id}/description, а список рисует только значок.
        "has_description": bool(exercise_descriptions.effective_description(row)),
        "is_archived": bool(row["is_archived"]),
        # Миниатюра строки списка — первый каталожный кадр, публичный URL той
        # же формы, что `images` у GET /exercises/{id}/media; null — кадров нет.
        "thumb": exercise_media.thumb_url_for(row),
        # Своё фото атлета — приватные байты за токеном (GET /exercises/{id}/photo),
        # в `thumb` оно не попадает; флаг говорит приложению, что за ним есть
        # смысл сходить. True только когда файл на месте — фото, живущее пока
        # только ссылкой в Telegram, ручка /photo отдать не может.
        "has_photo": exercise_photos.path_for_exercise(row) is not None,
        # {"weight", "reps", "date"} — лучший подход последней законченной
        # тренировки («100×8 · вчера» под именем), null — истории нет.
        "last_set": last_set,
    }


async def exercises_json(user_id: int, rows) -> list[dict[str, Any]]:
    """exercise_json для пачки строк с `last_set` — одним запросом на весь
    список (плюс строка пользователя за формулой e1RM и часовым поясом), а не
    по запросу на упражнение: у атлета их 150+."""
    rows = list(rows)
    if not rows:
        return []
    user = await db.get_user(user_id)
    last = await db.last_best_set_by_exercise(
        user_id,
        [r["id"] for r in rows],
        user["e1rm_formula"] if user else "epley",
        tz_offset=timeutil.offset_hours(user),
    )
    return [exercise_json(r, last.get(r["id"])) for r in rows]


async def one_exercise_json(user_id: int, row) -> dict[str, Any]:
    """exercises_json для одной строки — ответы создания, правки, архива и
    форка отдают упражнение тем же форматом, что и список, с тем же
    `last_set`, чтобы приложение не затирало строку списка пустым полем."""
    return (await exercises_json(user_id, [row]))[0]


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
        raise ApiError(400, "bad_request", f"{field} is in the future", key="input.date_in_future")


# ---------- числа подхода ----------
#
# Границы — те же, что у parser.py, которым разбирается строка «100 8» и в
# боте, и в /v1 (POST /workouts/{id}/sets/text). Своих чисел здесь нет
# намеренно: живая запись, принимающая reps=1000000000, и её же редактор,
# отвергающий то же самое, — это один и тот же подход, который клиент может
# записать, но не может поправить.

def optional_non_negative_number(body: dict[str, Any], key: str) -> Optional[float]:
    """Необязательное неотрицательное конечное число из тела (ккал, БЖУ):
    None, если поля нет или оно null. `True` — не число (bool в Python —
    подкласс int, и без явной проверки `"protein": true` ложилось в базу как
    1 г белка), NaN/Infinity (их принимает json.loads) — тоже, отрицательное
    — опечатка, а не «съел минус 9000 ккал»."""
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(400, "bad_request", f"{key} must be a number")
    if not math.isfinite(value):
        raise ApiError(400, "bad_request", f"{key} must be a finite number")
    if value < 0:
        raise ApiError(
            400, "bad_request", f"{key} must not be negative", key="api.error.number_negative"
        )
    return value


def set_weight(value: Any, field: str = "weight") -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ApiError(400, "bad_request", f"{field} must be a number")
    weight = float(value)
    # json.loads принимает NaN и Infinity: NaN проходил обе проверки ниже
    # (любое сравнение с ним ложно) и падал 500-й уже на NOT NULL в sets.
    if not math.isfinite(weight):
        raise ApiError(400, "bad_request", f"{field} must be a finite number")
    # 0 — это не «пусто», а честный вес собственного тела (подтягивания).
    if weight < 0:
        raise ApiError(400, "bad_request", f"{field} must not be negative", key="api.error.weight_negative")
    if weight > parser.MAX_WEIGHT:
        raise ApiError(
            400, "bad_request", f"{field} must be at most {parser.MAX_WEIGHT:.0f}",
            key="input.weight_too_big", max=f"{parser.MAX_WEIGHT:.0f}",
        )
    return weight


# Пояс в users.tz_offset — целые часы, и тот же диапазон, что у пикера бота
# (keyboards.timezone_picker_keyboard) и PATCH /settings: UTC-11 … UTC+14.
TZ_OFFSET_MIN, TZ_OFFSET_MAX = -11, 14
# Настоящие офсеты устройства в минутах: от UTC-12:00 до UTC+14:00.
_DEVICE_TZ_MINUTES_MIN, _DEVICE_TZ_MINUTES_MAX = -12 * 60, 14 * 60


def device_tz_offset_hours(value: Any) -> Optional[int]:
    """Офсет устройства в минутах (TimeZone.secondsFromGMT / 60 у iOS) → часы
    для users.tz_offset, или None, если прислано не целое число в разумных
    границах.

    Модель пояса — целые часы (timeutil, db._local_day), поэтому получасовые
    пояса округляются до ближайшего часа, половина — вверх: Индия (+5:30) → +6,
    Непал (+5:45) → +6, Ньюфаундленд (-3:30) → -3. Сутки у такого человека
    режутся с ошибкой в полчаса, а не в несколько часов, как с чужим дефолтом
    config.DEFAULT_TZ_OFFSET. Клиент (DeviceTimeZone.swift в приложении)
    округляет так же, чтобы сравнивать с tz_offset из /settings.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if not _DEVICE_TZ_MINUTES_MIN <= value <= _DEVICE_TZ_MINUTES_MAX:
        return None
    hours = (value + 30) // 60
    return max(TZ_OFFSET_MIN, min(TZ_OFFSET_MAX, hours))


def bodyweight_value(value: Any, field: str = "weight") -> float:
    """Вес тела из тела запроса — та же жёсткая граница, что у ввода веса в боте
    (parser.parse_bodyweight: больше нуля и меньше _BODYWEIGHT_HARD_MAX) и тот
    же текст. Без неё POST/PATCH /bodyweight принимали 0, -80 и 10^9, и такая
    запись навсегда кривила график. Мягкий переспрос «точно 8 кг?» — дело
    клиента (BodyweightViewModel.implausibilityWarning), здесь только отказ
    невозможному."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ApiError(400, "bad_request", f"{field} must be a number", key="input.bodyweight_invalid")
    weight = float(value)
    # Сравнение «внутри», а не «снаружи»: NaN (json.loads его принимает) не
    # проходит ни одну из двух половин и тоже отлетает.
    if not (0 < weight < parser._BODYWEIGHT_HARD_MAX):
        raise ApiError(
            400, "bad_request",
            f"{field} must be greater than 0 and less than {parser._BODYWEIGHT_HARD_MAX:.0f}",
            key="input.bodyweight_out_of_range",
        )
    return weight


# Запас на «завтра» у метки взвешивания: клиент шлёт своё местное время, а у
# UTC+14 оно на 14 часов впереди серверного UTC — старые сборки приложения
# так и пишут местный полдень выбранного дня.
_LOGGED_AT_FUTURE_SLACK = dt.timedelta(days=1)


def bodyweight_logged_at(value: Any, field: str = "logged_at") -> str:
    """Метка взвешивания из тела запроса → наивный UTC ISO (как всё в базе).

    Раньше строка ложилась в базу как пришла, любая: «вчера», «abc» — и потом
    `fromisoformat` у графика в боте падал на ней целым экраном. Метка с
    поясом (`...Z`, `+03:00`) приводится к UTC; без пояса считается уже UTC —
    так её и шлёт приложение. Запись из далёкого будущего — тоже опечатка, а
    не взвешивание."""
    if not isinstance(value, str):
        raise ApiError(400, "bad_request", f"{field} must be an ISO 8601 string")
    try:
        moment = dt.datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ApiError(400, "bad_request", f"{field} must be an ISO 8601 datetime") from exc
    if moment.tzinfo is not None:
        moment = moment.astimezone(dt.timezone.utc).replace(tzinfo=None)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    if moment > now + _LOGGED_AT_FUTURE_SLACK:
        raise ApiError(400, "bad_request", f"{field} is in the future")
    return moment.isoformat(timespec="seconds")


def set_reps(value: Any, field: str = "reps") -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ApiError(400, "bad_request", f"{field} must be a positive int", key="input.reps_zero")
    if value > parser.MAX_REPS:
        raise ApiError(
            400, "bad_request", f"{field} must be at most {parser.MAX_REPS}",
            key="input.reps_too_many", max=parser.MAX_REPS,
        )
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
        raise ApiError(400, "bad_request", f"{field} must be between 0 and 10", key="input.rpe_range")
    return rpe
