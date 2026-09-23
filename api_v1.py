"""REST-слой `/v1` для iOS-клиента (training_log_bot_ios).

Тот же приём, что у mcp_server.py: это транспорт поверх db.py, а не вторая
реализация бизнес-логики. Отличие от MCP — пишущих эндпоинтов тут много
(логирование подхода — основной сценарий приложения), поэтому у каждого
свой маршрут, а не общий execute_tool.

Аутентификация — отдельный токен (`db.api_tokens`), не mcp_tokens: это разные
клиенты одного человека, и перевыпуск одного не должен разлогинивать другой.
Связка аккаунта делается кодом, который бот показывает по запросу
(`db.issue_oauth_link_code`) — тот же код связывания, что и у OAuth, только
без веб-страницы согласия: клиент обменивает его на токен напрямую
(`db.consume_link_code`).

Транспорт — Starlette (уже тянется как зависимость mcp), без lifespan: у
приложения нет собственного состояния для запуска/остановки, соединение с
базой держит db.py.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import re
from typing import Any, Optional

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import achievement_sync
import achievements
import ai_trainer
import analytics
import api_v1_account
import api_v1_achievements
import api_v1_activity
import api_v1_ai
import api_v1_common as common
import api_v1_dashboard
import api_v1_feedback
import api_v1_food
import api_v1_hall_of_fame
import api_v1_history
import api_v1_import
import api_v1_media
import api_v1_programs
import api_v1_progress
import api_v1_sharing
import api_v1_templates
import api_v1_voice
import apple_signin
import dashboard_data
import db
import formatting
import history_search_data
import i18n
import mcp_oauth
import parser
import seed_data
import server_timing
import timeutil
import view_builder
import voice_parse
from workout_edit_data import on_workout_edited

logger = logging.getLogger(__name__)


# Общие помощники живут в api_v1_common — их же импортируют модули доменов
# (api_v1_programs и соседи). Имена ниже оставлены прежними: на них ссылается
# весь файл и тесты, а переименование ради переезда ничего не улучшает.
ApiError = common.ApiError
_api_error_handler = common.api_error_handler
_unhandled_error_handler = common.unhandled_error_handler
_authed_user_id = common.authed_user_id
_json_body = common.json_body
_require = common.require


# ---------- сериализация ----------

# Сериализация упражнения переехала в api_v1_common.exercise_json — на неё же
# теперь опирается api_v1_templates.py (форк шаблона отдаёт тот же формат).
_exercise_json = common.exercise_json


def _set_json(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "exercise_id": row["exercise_id"],
        "round_index": row["round_index"],
        "weight": row["weight"],
        "reps": row["reps"],
        "rpe": row["rpe"],
        "load_weight": row["load_weight"] if row["load_weight"] is not None else row["weight"],
        "created_at": row["created_at"],
    }


def _workout_json(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "note": row["note"],
        "routine_id": row["routine_id"],
    }


# ---------- auth ----------

async def auth_link(request: Request) -> JSONResponse:
    """Обменять код связывания (показал бот) на токен доступа к /v1.

    Лимит попыток — теми же окнами, что у OAuth-страницы согласия
    (mcp_oauth.CONSENT_FAILURE_*): код всего 6-8 цифр, и без лимита скрипт
    перебрал бы весь диапазон за секунды, пока код ещё не истёк.
    """
    body = await _json_body(request)
    _set_pre_auth_lang(body, request)
    code = str(_require(body, "code", str)).strip()
    client_ip = request.client.host if request.client else None
    status, user_id = await db.consume_link_code(
        code,
        client_ip=client_ip,
        window_seconds=mcp_oauth.CONSENT_FAILURE_WINDOW,
        window_limit_per_ip=mcp_oauth.CONSENT_FAILURE_LIMIT_PER_IP,
        window_limit_total=mcp_oauth.CONSENT_FAILURE_LIMIT_TOTAL,
    )
    if status == "rate_limited":
        raise ApiError(429, "rate_limited", "too many attempts, try again later")
    if status != "ok" or user_id is None:
        raise ApiError(400, "invalid_code", "code is invalid or expired")
    return await _issue_token_response(user_id)


async def _issue_token_response(user_id: int) -> JSONResponse:
    token = await db.issue_api_token(user_id)
    user = await db.get_user(user_id)
    return JSONResponse(
        {
            "token": token,
            "user_id": user_id,
            "unit": user["unit"] if user else "kg",
            "lang": user["lang"] if user else "ru",
        }
    )


def _signup_language_code(body: dict[str, Any], request: Request) -> str:
    """Язык НОВОГО app-only аккаунта (только для заведения, существующему
    пользователю язык никогда не переписываем — его он мог выбрать сам).

    У бота язык приезжает с каждым апдейтом (`language_code` телеграма), у
    приложения такого сигнала нет, и до этой функции каждый аккаунт из
    приложения заводился русским — атлет с английским телефоном получал
    русские тексты со всех экранов. Порядок: поле `lang` в теле (клиент шлёт
    язык устройства — "en", "ru", "en-US"), иначе заголовок `Accept-Language`
    (URLSession ставит его сам по языкам устройства), иначе дефолт. Сырой код
    отдаётся в `i18n.normalize` уже внутри db.create_app_only_user.
    """
    raw = body.get("lang")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return i18n.lang_from_accept_language(request.headers.get("accept-language"))


def _set_pre_auth_lang(body: dict[str, Any], request: Request) -> None:
    """Язык ошибок входа («код не подошёл»): пользователя ещё нет, users.lang
    взять неоткуда, поэтому тот же сигнал, что и у заведения аккаунта —
    `lang` из тела или `Accept-Language` (см. api_v1_common.request_lang)."""
    request.state.lang = i18n.normalize(_signup_language_code(body, request))


async def auth_apple(request: Request) -> JSONResponse:
    """Sign In with Apple.

    Тело всегда несёт `identity_token`. Если этот Apple ID уже привязан
    (обычно так — человек включил Face ID вместо кода раньше), пересылка
    `link_code` не нужна: `resolve_auth_identity` сам находит владельца.

    Если это первый раз для этого Apple ID, есть два пути:
      - пришёл `link_code` (тот же код, что выдаёт `/ios` в боте) — это
        пересвязка уже существующего telegram-аккаунта, ровно как раньше;
      - `link_code` не пришёл — App Review заворачивает приложения, которые
        нельзя завести без стороннего мессенджера, так что без кода из бота
        заводим НОВЫЙ аккаунт без Telegram (см. db.create_app_only_user) и
        сразу выдаём токен. Telegram к нему можно привязать позже — см.
        request_telegram_link_code ниже и handlers/ios_link.cmd_link_app.

    Необязательное поле `lang` (строка: "en", "ru", "en-US") — язык устройства
    для НОВОГО аккаунта; без него — `Accept-Language` (см. _signup_language_code).
    """
    body = await _json_body(request)
    _set_pre_auth_lang(body, request)
    identity_token = str(_require(body, "identity_token", str))
    try:
        identity = apple_signin.verify_identity_token(identity_token)
    except apple_signin.AppleTokenError as exc:
        raise ApiError(401, "invalid_apple_token", str(exc)) from exc

    user_id = await db.resolve_auth_identity("apple", identity.apple_user_id)
    if user_id is None:
        link_code = body.get("link_code")
        if link_code and isinstance(link_code, str):
            status, code_user_id = await db.consume_link_code(
                link_code.strip(),
                client_ip=request.client.host if request.client else None,
                window_seconds=mcp_oauth.CONSENT_FAILURE_WINDOW,
                window_limit_per_ip=mcp_oauth.CONSENT_FAILURE_LIMIT_PER_IP,
                window_limit_total=mcp_oauth.CONSENT_FAILURE_LIMIT_TOTAL,
            )
            if status == "rate_limited":
                raise ApiError(429, "rate_limited", "too many attempts, try again later")
            if status != "ok" or code_user_id is None:
                raise ApiError(400, "invalid_code", "code is invalid or expired")
            user_id = code_user_id
        else:
            new_user = await db.create_app_only_user(
                language_code=_signup_language_code(body, request)
            )
            user_id = new_user["telegram_id"]
        await db.link_auth_identity(user_id, "apple", identity.apple_user_id, identity.email)

    return await _issue_token_response(user_id)


async def request_telegram_link_code(request: Request) -> JSONResponse:
    """Код, которым app-only аккаунт (заведён Apple ID без Telegram) связывает
    себя с настоящим Telegram — направление, обратное `/auth/link`: там код
    показывает бот, а вводит приложение, тут код выдаёт приложение (этот
    эндпоинт), а вводит его человек боту (`handlers.ios_link.cmd_link_app`),
    потому что у app-only аккаунта нет чата, куда бот мог бы что-то прислать
    сам.

    Код и хранилище те же (db.issue_oauth_link_code / db.oauth_link_codes),
    что и у обычной привязки — это тот же приём «докажи владение», направление
    роли на него не влияет.
    """
    user_id = await _authed_user_id(request)
    user = await db.get_user(user_id)
    if user is None:
        raise ApiError(404, "not_found", "user not found")
    if user["telegram_linked"]:
        raise ApiError(409, "already_linked", "account is already linked to Telegram")
    code = await mcp_oauth.link_code(user_id, force_new=True)
    return JSONResponse({"code": code, "ttl_minutes": mcp_oauth.LINK_CODE_TTL_MINUTES})


async def me(request: Request) -> JSONResponse:
    user_id, user = await common.authed_user(request)
    return JSONResponse(
        {
            "user_id": user_id,
            "username": user["username"],
            "unit": user["unit"],
            "lang": user["lang"],
            "telegram_linked": bool(user["telegram_linked"]),
        }
    )


# ---------- push-уведомления (APNs) ----------
#
# Только приём и хранение device token — само отправление пушей ждёт
# .p8-ключа APNs (платный Apple Developer Program) и решения, что вообще
# слать. См. db.push_tokens.

async def register_push_token(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    body = await _json_body(request)
    device_token = str(_require(body, "device_token", str)).strip()
    if not device_token:
        raise ApiError(400, "bad_request", "device_token must not be empty")
    await db.register_push_token(user_id, "ios", device_token)
    return JSONResponse({"registered": True}, status_code=201)


async def unregister_push_token(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    await db.unregister_push_token(user_id, "ios")
    return JSONResponse({"unregistered": True})


# ---------- каталог ----------

async def list_muscle_groups(request: Request) -> JSONResponse:
    """Группы мышц пользователя, самые используемые первыми.

    `days_ago` — сколько дней прошло с последней законченной тренировки, где
    эта группа была (по местному дню, как у тренера в
    ai_trainer `_muscle_recovery`: тот же db.last_session_by_group), `null` —
    ни разу. Приложение рисует им плитки групп на экране старта тренировки
    («Спина — 6 дней»), чтобы выбор, что качать, начинался с того, что давно
    не делал, а не с поиска по каталогу.
    """
    user_id = await _authed_user_id(request)
    groups = await db.list_muscle_groups(user_id, order_by_usage=True)
    last = await db.last_session_by_group(user_id)
    today = timeutil.user_today(await db.get_user(user_id))

    def days_ago(group_id: int) -> Optional[int]:
        entry = last.get(group_id)
        if entry is None:
            return None
        return max((today - dt.date.fromisoformat(entry[0])).days, 0)

    return JSONResponse(
        [
            {"id": g["id"], "name": _group_name(g), "emoji": g["emoji"], "days_ago": days_ago(g["id"])}
            for g in groups
        ]
    )


def _group_name(group) -> str:
    """Имя группы на языке атлета. Встроенные группы глобальные и в базе
    навсегда русские (см. seed_data.localized_muscle_group_name) — сырое
    `name` отдавало англоязычному «Грудь»; своя группа проходит как есть.
    Язык — из контекста запроса (api_v1_common.authed_user_id)."""
    return seed_data.localized_muscle_group_name(group["name"], i18n.get_lang())


async def create_muscle_group(request: Request) -> JSONResponse:
    """Своя группа мышц — не у всех каталог бота совпадает с тем, как
    называют группы в конкретном зале."""
    user_id = await _authed_user_id(request)
    body = await _json_body(request)
    name = str(_require(body, "name", str)).strip()
    if not name:
        raise ApiError(400, "bad_request", "name must not be empty", key="api.error.name_empty")
    emoji = body.get("emoji")
    if emoji is not None and not isinstance(emoji, str):
        raise ApiError(400, "bad_request", "emoji must be a string")
    # Имя встроенной группы на любом языке («Chest», «Грудь») — это та самая
    # встроенная группа, а не новая: клиент показывает локализованные имена и
    # может прислать их обратно. Без сверки у англоязычного заводилась бы
    # своя «Chest» рядом со встроенной, и упражнения расползались бы по двум.
    canonical = seed_data.canonical_muscle_group_name(name)
    if canonical is not None:
        for existing in await db.list_muscle_groups(user_id):
            if existing["user_id"] is None and existing["name"] == canonical:
                return JSONResponse(
                    {"id": existing["id"], "name": _group_name(existing), "emoji": existing["emoji"]}
                )
    group_id = await db.create_muscle_group(user_id, name, emoji)
    group = await db.get_muscle_group(group_id)
    return JSONResponse({"id": group["id"], "name": _group_name(group), "emoji": group["emoji"]}, status_code=201)


async def list_exercises(request: Request) -> JSONResponse:
    """Четыре режима, как в боте: архив (отдельный список, как «🗄 Архив» в
    меню — обычный каталог его никогда не подмешивает), по группе мышц (обзор
    для тех, кто не помнит точное название), текстовым поиском, или весь
    каталог. Параметры не комбинируются — archived, затем group_id, затем
    query побеждают в этом порядке, раз пришли."""
    user_id = await _authed_user_id(request)
    archived_param = request.query_params.get("archived")
    group_id_param = request.query_params.get("group_id")
    query = request.query_params.get("query")
    if archived_param and archived_param.lower() in ("1", "true", "yes"):
        rows = await db.list_archived_exercises(user_id)
    elif group_id_param:
        try:
            group_id = int(group_id_param)
        except ValueError as exc:
            raise ApiError(400, "bad_request", "group_id must be int") from exc
        rows = await db.list_user_exercises_in_group(user_id, group_id)
    elif query:
        rows = await db.search_exercises(user_id, query, limit=50)
    else:
        rows = await db.list_user_exercises(user_id)
    return JSONResponse([_exercise_json(r) for r in rows])


async def create_exercise(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    body = await _json_body(request)
    name = str(_require(body, "name", str)).strip()
    if not name:
        raise ApiError(400, "bad_request", "name must not be empty", key="api.error.name_empty")
    group_id = body.get("group_id")
    if group_id is not None:
        if not isinstance(group_id, int):
            raise ApiError(400, "bad_request", "group_id must be int")
        group = await db.get_muscle_group(group_id)
        # Группа либо глобальная (user_id IS NULL, каталог бота), либо своя —
        # чужую личную группу подставить нельзя: id угадывается, и без этой
        # проверки чужое упражнение молча привязалось бы к чужому разбиению.
        if group is None or (group["user_id"] is not None and group["user_id"] != user_id):
            raise ApiError(404, "not_found", "muscle group not found")
    exercise_id = await db.create_exercise(user_id, name, group_id)
    row = await db.get_exercise(exercise_id)
    return JSONResponse(_exercise_json(row), status_code=201)


async def update_exercise(request: Request) -> JSONResponse:
    """PATCH — частичное обновление, как /settings: только присланные поля
    меняются. То же самое меню бота (handlers/exercises.py), только без
    диалога — переименование, смена группы мышц и описание техники."""
    user_id = await _authed_user_id(request)
    exercise_id = int(request.path_params["exercise_id"])
    await _owned_exercise(exercise_id, user_id)
    body = await _json_body(request)

    if "name" in body:
        name = str(_require(body, "name", str)).strip()
        if not name:
            raise ApiError(400, "bad_request", "name must not be empty", key="api.error.name_empty")
        # Клэш по display_name — та же ловушка, что update_exercise_name
        # разбирает в docstring: переименование в уже занятое имя должно
        # остаться отдельным ответом, а не молча слиться с чужой историей.
        if not await db.update_exercise_name(exercise_id, name):
            raise ApiError(409, "name_taken", "another exercise already has this name")

    if "group_id" in body:
        group_id = body["group_id"]
        if not isinstance(group_id, int) or isinstance(group_id, bool):
            raise ApiError(400, "bad_request", "group_id must be int")
        group = await db.get_muscle_group(group_id)
        if group is None or (group["user_id"] is not None and group["user_id"] != user_id):
            raise ApiError(404, "not_found", "muscle group not found")
        await db.update_exercise_group(exercise_id, group_id)

    if "description" in body:
        description = common.optional_str(body, "description")
        await db.set_exercise_description(exercise_id, description)

    row = await db.get_exercise(exercise_id)
    return JSONResponse(_exercise_json(row))


async def archive_exercise(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    exercise_id = int(request.path_params["exercise_id"])
    await _owned_exercise(exercise_id, user_id)
    await db.archive_exercise(exercise_id)
    row = await db.get_exercise(exercise_id)
    return JSONResponse(_exercise_json(row))


async def unarchive_exercise(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    exercise_id = int(request.path_params["exercise_id"])
    await _owned_exercise(exercise_id, user_id)
    await db.unarchive_exercise(exercise_id)
    row = await db.get_exercise(exercise_id)
    return JSONResponse(_exercise_json(row))


async def merge_exercises(request: Request) -> JSONResponse:
    """Слить дубликат в целевое упражнение — самое ценное из всей карточки:
    «Жим лёжа» и «жим штанги лёжа», занесённые порознь, иначе делят историю и
    график пополам. `target_id` — тот, что остаётся (его история, фото,
    описание побеждают при конфликте), `source_id` — тот, что удаляется;
    вся бизнес-логика и проверки (не своё, цель в архиве, открытая тренировка)
    уже в db.merge_exercises, ровно как у кнопки «Объединить» в боте."""
    user_id = await _authed_user_id(request)
    body = await _json_body(request)
    target_id = _require(body, "target_id", int)
    source_id = _require(body, "source_id", int)
    # 404 раньше вызова db.merge_exercises: та под "invalid" склеивает и чужое,
    # и несуществующее, и совпадение id — снаружи это должно выглядеть как
    # обычное отсутствие ресурса, а не как единая проверка позже.
    await _owned_exercise(target_id, user_id)
    await _owned_exercise(source_id, user_id)
    outcome = await db.merge_exercises(user_id, keep_id=target_id, drop_id=source_id)
    if outcome != db.MERGE_OK:
        code, message = {
            db.MERGE_TARGET_ARCHIVED: ("target_archived", "target exercise is archived"),
            db.MERGE_IN_ACTIVE_WORKOUT: ("active_workout", "one of the exercises is in the active workout"),
        }.get(outcome, ("bad_request", "cannot merge these exercises"))
        raise ApiError(409, code, message)
    row = await db.get_exercise(target_id)
    return JSONResponse(_exercise_json(row))


async def exercise_progress(request: Request) -> JSONResponse:
    """Все подходы по упражнению за всё время, старые сначала — для графика
    прогресса. Тот же db.list_sets_for_exercise, что у аналитики бота."""
    user_id = await _authed_user_id(request)
    exercise_id = int(request.path_params["exercise_id"])
    await _owned_exercise(exercise_id, user_id)
    rows = await db.list_sets_for_exercise(exercise_id)
    return JSONResponse(
        [
            {
                "workout_id": r["workout_id"],
                "started_at": r["started_at"],
                "weight": r["weight"],
                "reps": r["reps"],
                "rpe": r["rpe"],
                "load_weight": r["load_weight"] if r["load_weight"] is not None else r["weight"],
            }
            for r in rows
        ]
    )


# ---------- тренировки ----------

async def _owned_workout(workout_id: int, user_id: int):
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != user_id:
        raise ApiError(404, "not_found", "workout not found")
    return workout


_SUGGESTION_COOLDOWN_DAYS = 2
_SUGGESTION_RECENT_LIMIT = 2


async def _suggested_next_exercise(
    user_id: int, last_finished_id: Optional[int], done_ids: tuple[int, ...]
) -> Optional[dict[str, Any]]:
    """Что этот человек делал сразу после `last_finished_id` в прошлый раз —
    та же подсказка одним тапом, что бот строит в `_idle_view`
    (handlers/workout.py), только для REST: те же db-вызовы, то же решение,
    просто без aiogram-клавиатуры вокруг."""
    if last_finished_id is None:
        return None
    workout_id = await db.find_last_finished_workout_with_exercise(user_id, last_finished_id)
    if workout_id is None:
        return None
    nxt = await db.get_next_exercise_in_workout(workout_id, last_finished_id)
    if nxt is None or nxt["exercise_id"] == last_finished_id or nxt["exercise_id"] in done_ids:
        return None
    ex = await db.get_exercise(nxt["exercise_id"])
    if ex is None or ex["is_archived"]:
        return None
    return {"id": ex["id"], "name": ex["display_name"]}


async def next_exercise_suggestions(request: Request) -> JSONResponse:
    """Подсказки на экране «упражнение не выбрано» без плана — те же два
    источника, что и в боте (`handlers/workout._idle_view`): «что шло следом
    в прошлый раз» одним упражнением и до двух «обычно идёт после» / просто
    недавних, без повтора того, что уже открыто в этой тренировке или
    сделано за последние двое суток.

    Только для незапланированной тренировки: план (день программы, повтор)
    приложение уже строит на своей стороне (`ActiveWorkoutViewModel.plan`) и
    сюда за подсказкой не ходит вовсе, поэтому здесь ничего о плане не знают.
    """
    user_id = await _authed_user_id(request)
    last_finished_param = request.query_params.get("last_finished_id")
    last_finished_id: Optional[int] = None
    if last_finished_param:
        try:
            last_finished_id = int(last_finished_param)
        except ValueError as exc:
            raise ApiError(400, "bad_request", "last_finished_id must be int") from exc
        await _owned_exercise(last_finished_id, user_id)
    done_param = request.query_params.get("done_ids", "")
    try:
        done_ids = tuple(int(x) for x in done_param.split(",") if x)
    except ValueError as exc:
        raise ApiError(400, "bad_request", "done_ids must be a comma-separated list of ints") from exc

    suggested = await _suggested_next_exercise(user_id, last_finished_id, done_ids)
    exclude = done_ids + ((suggested["id"],) if suggested else ())
    cooldown = (dt.datetime.now() - dt.timedelta(days=_SUGGESTION_COOLDOWN_DAYS)).isoformat(timespec="seconds")
    rows: list = []
    if last_finished_id is not None:
        rows = await db.list_common_followups(
            user_id, last_finished_id, limit=_SUGGESTION_RECENT_LIMIT,
            exclude_ids=exclude, not_used_since=cooldown,
        )
    if not rows:
        rows = await db.list_recent_exercises(
            user_id, limit=_SUGGESTION_RECENT_LIMIT, exclude_ids=exclude, not_used_since=cooldown
        )
    return JSONResponse(
        {
            "suggested": suggested,
            "recent": [{"id": r["id"], "name": r["display_name"]} for r in rows],
        }
    )


async def _owned_exercise(exercise_id: int, user_id: int):
    exercise = await db.get_exercise(exercise_id)
    if exercise is None or exercise["user_id"] != user_id:
        raise ApiError(404, "not_found", "exercise not found")
    return exercise


# Тот же потолок, что у кнопок «⚡ {название}» в живом трекере
# (keyboards.py:504-507): экран отводит под партнёров по суперсету не больше
# двух кнопок, третья туда физически не влезает.
_SUPERSET_PARTNER_LIMIT = 2


async def superset_partners(request: Request) -> JSONResponse:
    """Кандидаты на «⚡ партнёр по суперсету» для экрана выбора упражнения —
    те же db-вызов и те же исключения, что строит бот
    (`handlers/workout.py`, ветка `if open_ids:` в `_picker_screen_groups`),
    просто без aiogram-клавиатуры вокруг.

    Кандидатов считает `db.list_superset_partners` — переиспользуем её же,
    а не переписываем отбор по окнам подходов здесь: другой код,
    выбирающий тот же список другим SQL, разошёлся бы с ботом первым же
    изменением одной из копий.

    Исключения — ровно две, как у бота:
    - `open_ids` (query, через запятую) — вкладки, открытые в трекере прямо
      сейчас на клиенте. Это клиентское состояние (какие упражнения открыты
      табами), а не то, что можно вычислить по базе: свежеоткрытая без
      единого подхода вкладка на сервере может ещё не завести блок.
    - `db.list_opened_exercise_ids_for_workout(workout_id)` — всё, что в этой
      тренировке уже открывали, включая закрытые до этого момента вкладки
      без подходов. Без этого исключения кнопка предложила бы то, что
      человек только что закрыл.
    """
    user_id = await _authed_user_id(request)
    exercise_id = int(request.path_params["exercise_id"])
    await _owned_exercise(exercise_id, user_id)

    workout_param = request.query_params.get("workout_id")
    if not workout_param:
        raise ApiError(400, "bad_request", "workout_id is required")
    try:
        workout_id = int(workout_param)
    except ValueError as exc:
        raise ApiError(400, "bad_request", "workout_id must be int") from exc
    await _owned_workout(workout_id, user_id)

    open_param = request.query_params.get("open_ids", "")
    try:
        open_ids = tuple(int(x) for x in open_param.split(",") if x)
    except ValueError as exc:
        raise ApiError(400, "bad_request", "open_ids must be a comma-separated list of ints") from exc

    already_opened = await db.list_opened_exercise_ids_for_workout(workout_id)
    exclude_ids = tuple(set(open_ids) | set(already_opened))
    partners = await db.list_superset_partners(
        user_id, exercise_id, limit=_SUPERSET_PARTNER_LIMIT, exclude_ids=exclude_ids
    )
    return JSONResponse({"partners": [{"id": p["id"], "name": p["display_name"]} for p in partners]})


async def _find_block_for_exercise(workout_id: int, exercise_id: int) -> Optional[int]:
    for block in await db.list_blocks_for_workout(workout_id):
        for be in await db.get_block_exercises(block["id"]):
            if be["exercise_id"] == exercise_id:
                return block["id"]
    return None


async def _block_for_exercise(workout_id: int, exercise_id: int) -> int:
    """Блок этого упражнения в тренировке — существующий (первый попавшийся,
    без суперсетов на этом этапе) или новый одиночный.

    Поиск и создание — одним вызовом db, под её `_write_lock`: делать это
    двумя вызовами отсюда нельзя, между ними параллельные запросы «запиши
    подход» успевали завести по блоку каждый (см. докстринг
    db.get_or_create_single_block_for_exercise)."""
    return await db.get_or_create_single_block_for_exercise(workout_id, exercise_id)


def _require_open(workout) -> None:
    """Писать подходы можно в незаконченную тренировку — и в живую, и в
    заносимую задним числом. Статусов ровно три (`active`, `backfill`,
    `finished`), поэтому проверяется именно `finished`, а не равенство
    `active`: иначе занесение задним числом отвергалось бы как «уже
    закончена», хотя закончена она не была."""
    if workout["status"] == "finished":
        raise ApiError(409, "workout_finished", "workout is already finished")


async def active_workout(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    workout = await db.get_active_workout(user_id)
    if workout is None:
        return JSONResponse(None)
    return JSONResponse(await _workout_detail_json(workout))


async def start_workout(request: Request) -> JSONResponse:
    """`routine_id` в теле — необязательный: старое приложение шлёт пустое
    тело и получает тренировку «с нуля», как раньше. Без привязки к дню
    программы `db.next_program_day` не может продвинуться дальше первого
    дня — ровно то, что уже делает бот в _begin_routine_workout, здесь тот
    же db.create_workout(routine_id=...) через get_or_create_active_workout."""
    user_id = await _authed_user_id(request)
    body = await _json_body(request) if await request.body() else {}
    routine_id = common.optional_int(body, "routine_id")
    if routine_id is not None:
        await api_v1_programs._owned_routine(routine_id, user_id)
    workout_id, created = await db.get_or_create_active_workout(user_id, routine_id=routine_id)
    workout = await db.get_workout(workout_id)
    return JSONResponse(_workout_json(workout), status_code=201 if created else 200)


async def discard_active_workout(request: Request) -> JSONResponse:
    """Снести активную тренировку целиком — «начал по ошибке» или «передумал».
    Законченную так не тронуть: у неё уже есть история, отменять нечего, это
    правит только finish_workout (заметка) или ничего вовсе."""
    user_id = await _authed_user_id(request)
    workout = await db.get_active_workout(user_id)
    if workout is None:
        raise ApiError(404, "not_found", "no active workout")
    await db.discard_workout(workout["id"])
    return JSONResponse({"discarded": True})


BACKFILL_HOUR = "T12:00:00"
"""Полдень — то же время, что ставит бот (handlers.backfill._date_chosen).

Не полночь: тренировка, записанная на 00:00, у пользователя с отрицательным
офсетом попадает по местному времени во вчера, и день в истории разъезжается
с тем, который человек выбрал в календаре. Полдень таких сдвигов не даёт ни
при одном обитаемом офсете (UTC-11 … UTC+14)."""


async def backfill_workout(request: Request) -> JSONResponse:
    """Открытая тренировка, заносимая задним числом, или `null`.

    Отдельно от `/workouts/active`: у них разные статусы и разный смысл на
    экране. Живая тренировка идёт прямо сейчас и показывает таймер, занесение
    задним числом — это форма за прошлый день, и подсовывать её под кнопку
    «Продолжить тренировку» значило бы врать про то, что происходит.
    """
    user_id = await _authed_user_id(request)
    workout = await db.get_backfill_workout(user_id)
    if workout is None:
        return JSONResponse(None)
    return JSONResponse(await _workout_detail_json(workout))


async def start_backfill_workout(request: Request) -> JSONResponse:
    """Начать занесение тренировки за прошедший день.

    Незаконченное занесение уже есть — отдаём его, а не заводим второе: две
    открытые формы за разные дни человек различить не сможет, а брошенная
    останется в базе навсегда (у неё нет ни таймера, ни экрана, который о ней
    напомнит). Хочется другой день — сначала это, кнопкой «Отменить».

    Проверка и вставка — одним вызовом db.get_or_create_backfill_workout под
    общим _write_lock, а не get_backfill_workout + create_workout по
    отдельности: иначе два параллельных запроса (бот и приложение, или
    двойной тап) оба видели «занесения нет» и заводили по своему — второй
    навсегда зависал призраком, невидимым для get_backfill_workout
    (ORDER BY id LIMIT 1 всегда возвращает первый). Тот же инцидент уже был
    закрыт для активной тренировки в db.get_or_create_active_workout.
    """
    user_id = await _authed_user_id(request)
    body = await _json_body(request)
    date = common.parse_date(_require(body, "date", str))
    # Будущим днём тренировки не бывает: это занесение того, что уже сделано.
    # Сегодня — по часовому поясу пользователя, а не по UTC сервера.
    await common.reject_future_date(date, user_id)

    workout_id, created = await db.get_or_create_backfill_workout(
        user_id, f"{date.isoformat()}{BACKFILL_HOUR}"
    )
    workout = await db.get_workout(workout_id)
    return JSONResponse(await _workout_detail_json(workout), status_code=201 if created else 200)


async def discard_backfill_workout(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    workout = await db.get_backfill_workout(user_id)
    if workout is None:
        raise ApiError(404, "not_found", "no backfill workout")
    await db.discard_workout(workout["id"])
    return JSONResponse({"discarded": True})


async def log_set(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    workout = await _owned_workout(workout_id, user_id)
    _require_open(workout)
    body = await _json_body(request)
    exercise_id = _require(body, "exercise_id", int)
    # Те же границы, что у parser.py и у правки уже записанного подхода
    # (api_v1_account): живая запись не должна принимать вес -100 и 10^9
    # повторов, которые её же редактор отвергает с 400.
    if "weight" not in body:
        raise ApiError(400, "bad_request", "missing field: weight")
    weight = common.set_weight(body["weight"])
    if "reps" not in body:
        raise ApiError(400, "bad_request", "missing field: reps")
    reps = common.set_reps(body["reps"])
    rpe = common.set_rpe(body.get("rpe"))
    # Ключ попытки — необязательный: старые сборки приложения его не шлют, и
    # тогда append_set работает буквально как раньше (см. её докстринг). Когда
    # он есть, это ключ, заведённый в момент нажатия кнопки на телефоне (а не
    # в момент отправки) — так повтор из офлайн-очереди после отвалившегося
    # интернета несёт тот же ключ и не заводит второй подход.
    idempotency_key = common.optional_str(body, "idempotency_key")
    await _owned_exercise(exercise_id, user_id)
    block_id = await _block_for_exercise(workout_id, exercise_id)
    set_id = await db.append_set(
        block_id, exercise_id, 0, weight, reps, rpe,
        user_id=user_id, idempotency_key=idempotency_key,
    )
    cur = await db.conn().execute("SELECT * FROM sets WHERE id = ?", (set_id,))
    row = await cur.fetchone()
    user = await db.get_user(user_id)
    # Тот же 🔥, что бот ставит реакцией на сообщение с подходом
    # (handlers.workout._sets_beat_record) — здесь это поле в ответе, а не
    # реакция: у приложения нет своих Telegram-сообщений на подход.
    is_record = await view_builder.sets_beat_record(
        exercise_id, workout_id, [(weight, reps, rpe)], user["e1rm_formula"]
    )
    return JSONResponse({**_set_json(row), "is_record": is_record}, status_code=201)


async def log_sets_from_text(request: Request) -> JSONResponse:
    """Подход (или несколько) одной строкой — «100 8», «100 8, 100 7, 95 8»,
    «100x8x3», «8», «100 8 @9», «80 кг 8 раз».

    Это главный способ записи в боте, и он не переносится в три числовых поля:
    строка умеет то, чего поля не умеют физически — несколько подходов за один
    ввод, повтор веса с прошлого подхода голыми повторами, RPE суффиксом,
    единицы словами. Поэтому разбор живёт на сервере и ровно тот же
    (`parser.parse_sets_line`), что у бота: два разных разбора одной и той же
    строки разъехались бы на первой же правке грамматики.

    Ошибка разбора отдаётся 400 с текстом ИЗ ИСКЛЮЧЕНИЯ, а не машинным кодом:
    `ParseError.message` уже локализован и написан голосом тренера («Не понял
    вес. Напиши число, например 80»), и клиенту его надо показать дословно.
    Единственное место в `/v1`, где сервер отдаёт человеческий текст ошибки, —
    и поэтому язык берём явно из users.lang через `i18n.use_lang`: без него
    сообщение приедет на языке того, кто первым дёрнул модуль в этом процессе
    (ловушка описана в CLAUDE.md).

    Бот на подозрительном вводе ещё и переспрашивает («не перепутаны ли вес и
    повторы», handlers.workout._weight_confirm_prompt). Здесь переспросить
    некому: HTTP-ответ не диалог. Подход пишется как разобран, а исправляется
    правкой или отменой последнего — оба маршрута уже есть.
    """
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    workout = await _owned_workout(workout_id, user_id)
    _require_open(workout)
    body = await _json_body(request)
    exercise_id = _require(body, "exercise_id", int)
    text = str(_require(body, "text", str)).strip()
    if not text:
        raise ApiError(400, "bad_request", "text must not be empty", key="api.error.text_empty")
    idempotency_key = common.optional_str(body, "idempotency_key")
    await _owned_exercise(exercise_id, user_id)

    user = await db.get_user(user_id)
    with i18n.use_lang(user["lang"] if user else "ru"):
        try:
            parsed = parser.parse_sets_line(text)
        except parser.ParseError as exc:
            # Машинный код всё равно есть — клиенту иногда надо отличить
            # «не разобрал» от «сервер лёг», — но показывать он должен message.
            raise ApiError(400, "unparsed_input", "set line not parsed", human=exc.message) from exc

    created = await _store_parsed_sets(
        workout_id, exercise_id, parsed, user_id=user_id, idempotency_key=idempotency_key
    )
    return JSONResponse({"sets": created}, status_code=201)


async def _store_parsed_sets(
    workout_id: int,
    exercise_id: int,
    parsed,
    *,
    user_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Общий хвост log_sets_from_text и log_set_from_voice: `parser.parse_sets_line`
    (или голос → та же структура через voice_parse) уже дал список подходов —
    остаётся разнести голые повторы по весу прошлого подхода и записать блок.

    Вынесено при добавлении голосового ввода (`log_set_from_voice`), чтобы у
    двух источников одной и той же строки (текст и расшифрованный голос) не
    завелось двух копий этой логики.

    Сама запись и разнесение голых повторов теперь в db.store_parsed_sets: это
    даёт одну строку одному ключу попытки, а не по подходу, и всей пачке —
    одну атомарную критическую секцию (см. её докстринг).
    """
    block_id = await _block_for_exercise(workout_id, exercise_id)
    items = [(p.weight_omitted, p.weight, p.reps, p.rpe) for p in parsed]
    set_ids = await db.store_parsed_sets(
        workout_id, block_id, exercise_id, items,
        user_id=user_id, idempotency_key=idempotency_key,
    )
    created = []
    for set_id in set_ids:
        row = await db.get_set(set_id)
        created.append(_set_json(row))
    return created


async def log_set_from_voice(request: Request) -> JSONResponse:
    """Тот же подход, что `log_sets_from_text`, только голосом («сто на
    восемь» вместо «100 8») — HTTP-версия `handlers/workout.py::log_set_voice`.

    Три шага ровно как у бота, никаких новых:
    1. расшифровка (`api_v1_voice.transcribe` → `ai_trainer.transcribe_voice`,
       та же функция, что зовёт бот — лимиты размера/длительности и формат
       см. в докстринге `api_v1_voice`);
    2. текст расшифровки → числа (`voice_parse.transcript_to_sets_line_with_hint`,
       тот же парсер слов-чисел, что у бота, — распознаёт "сто на восемь" в
       "100 8" и отдельно сигналит про отброшенное число подходов, см. его
       докстринг);
    3. "100 8" → подходы (`parser.parse_sets_line` + `_store_parsed_sets`) —
       то же самое, что делает `log_sets_from_text` с введённым текстом.

    Неразобранное (пустая расшифровка ИЛИ расшифровка без узнаваемых чисел) —
    один и тот же 400 `unparsed_input`, как и у бота (`workout.voice_parse_failed`
    не различает эти два случая, см. `handlers/workout.py::log_set_voice`).
    """
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    workout = await _owned_workout(workout_id, user_id)
    _require_open(workout)
    body = await _json_body(request)
    exercise_id = _require(body, "exercise_id", int)
    idempotency_key = common.optional_str(body, "idempotency_key")
    await _owned_exercise(exercise_id, user_id)

    user = await db.get_user(user_id)
    with i18n.use_lang(user["lang"] if user else "ru"):
        transcript = await api_v1_voice.transcribe(
            body,
            user_id,
            not_configured_message=i18n.t("ai.screen.voice_not_configured"),
            too_long_message=i18n.t("ai.screen.voice_too_long"),
            too_big_message=i18n.t(
                "ai.screen.voice_too_big", mb=api_v1_voice.MAX_VOICE_BYTES // (1024 * 1024)
            ),
            transcribe_failed_message=i18n.t("ai.screen.voice_transcribe_failed"),
        )
        line, dropped_sets = voice_parse.transcript_to_sets_line_with_hint(transcript)
        parsed = None
        if line:
            try:
                parsed = parser.parse_sets_line(line)
            except parser.ParseError:
                parsed = None
        if not parsed:
            raise ApiError(400, "unparsed_input", "empty transcript", key="ai.screen.voice_empty")

    created = await _store_parsed_sets(
        workout_id, exercise_id, parsed, user_id=user_id, idempotency_key=idempotency_key
    )
    return JSONResponse(
        {"sets": created, "transcript": transcript, "dropped_sets": dropped_sets},
        status_code=201,
    )


async def delete_last_set(request: Request) -> JSONResponse:
    """Убрать последний подход этого упражнения — правка опечатки веса/повторов
    сразу после записи, тем же приёмом, что «↩️ Отменить» в боте."""
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    exercise_id = int(request.path_params["exercise_id"])
    workout = await _owned_workout(workout_id, user_id)
    _require_open(workout)
    block_id = await _find_block_for_exercise(workout_id, exercise_id)
    if block_id is None:
        raise ApiError(404, "not_found", "exercise has no sets in this workout")
    # Не delete_last_set_in_block: в суперсете это снесло бы последний подход
    # ЛЮБОГО упражнения блока, если его логировали позже — здесь нужен именно
    # последний подход exercise_id.
    deleted = await db.delete_last_set_for_exercise_in_block(block_id, exercise_id)
    if deleted is None:
        raise ApiError(404, "not_found", "exercise has no sets in this workout")
    # Тот же хвост, что у DELETE /workouts/{id}/sets/{set_id}: снятый
    # последний подход не должен оставлять за собой блок-призрак
    # («упражнение есть, подходов нет»). Два способа отменить подход обязаны
    # давать один и тот же экран.
    await on_workout_edited(workout_id)
    return JSONResponse(_set_json(deleted))


# ---------- награды за завершённую тренировку ----------

_ai_comment_tasks: set[asyncio.Task] = set()
"""Живые ссылки на фоновые задачи генерации комментария.

asyncio держит задачу только слабой ссылкой: без этого множества сборщик
мусора вправе убить её на середине запроса к модели, и комментарий не
появится никогда — молча, без единой строки в логе.
"""


async def _write_ai_comment(user_id: int, workout_id: int) -> None:
    """Та же пара шагов, что делает бот в фоне после финиша
    (handlers.workout._attach_ai_comment): спросить модель и положить ответ в
    workouts.ai_comment. Правки сообщения, которая есть у бота, здесь нет —
    приложение забирает готовое через GET /workouts/{id}/ai-comment.

    Любое исключение гасится здесь: задача уже отвязана от запроса, ронять ей
    нечего, а необработанное исключение в задаче видно только под конец
    процесса строкой «Task exception was never retrieved».
    """
    try:
        comment = await ai_trainer.comment_on_workout(user_id, workout_id)
        await db.set_workout_ai_comment(workout_id, comment)
    except Exception:
        logger.exception("AI trainer workout comment failed for workout %s", workout_id)


def _spawn_ai_comment(user_id: int, workout_id: int, user, workout) -> None:
    """Запустить генерацию комментария в фоне — если он нужен и возможен.

    Условия ровно те же, что у бота (`needs_ai_comment` в
    handlers.workout._finalize_workout): комментария ещё нет, тумблер
    `ai_comments_enabled` включён и провайдер настроен. Без проверки
    is_configured каждая тренировка заводила бы задачу, которая сразу падает.

    Сам запуск обёрнут в try/except: ответ finish — это карточка итога, ради
    которой человек и жал кнопку, и уронить её из-за необязательного
    комментария нельзя ни при каком состоянии event loop.
    """
    if workout["ai_comment"] is not None:
        return
    if not user["ai_comments_enabled"] or not ai_trainer.is_configured():
        return
    try:
        task = asyncio.create_task(_write_ai_comment(user_id, workout_id))
    except Exception:
        logger.exception("failed to spawn AI comment task for workout %s", workout_id)
        return
    _ai_comment_tasks.add(task)
    task.add_done_callback(_ai_comment_tasks.discard)


_HTML_TAG = re.compile(r"<[^>]+>")


def _plain(text: Optional[str]) -> Optional[str]:
    """Убрать телеграмную разметку из готовой строки.

    Часть формулировок в locales/ несёт <b> — они писались для карточки бота,
    где разметка и есть оформление. Приложение рисует текст само, и тег в нём
    показался бы как есть, буквами. Разбирать HTML нечем и незачем: в этих
    строках он ровно один уровень простых тегов без атрибутов, зато сущности
    (&amp; в названии упражнения) раскрыть обязательно — иначе человек увидит
    «&amp;» вместо «&».
    """
    if text is None:
        return None
    return html.unescape(_HTML_TAG.sub("", text)).strip()


async def _finish_rewards_json(
    workout, user, new_codes: list[str], was_backfill: bool
) -> dict[str, Any]:
    """Итоги только что завершённой тренировки — то же, что бот собирает на
    карточке завершения (handlers.workout._finalize_workout).

    Отдаётся ключом `rewards` рядом с прежними полями тренировки, а не вместо
    них: у ответа finish уже есть потребители, читающие блоки и подходы.

    Тексты приходят готовыми и локализованными (тоннаж, «это как два
    холодильника», названия значков), числа — числами, по тем же доводам, что
    в api_v1_achievements: формулировки живут в locales/*.json одним
    экземпляром, а не ещё раз внутри старой версии приложения. Поэтому весь
    сбор идёт под i18n.use_lang(user["lang"]) — без него язык ответа был бы
    тем, который первым дёрнул модуль в этом процессе (CLAUDE.md, «Ловушка,
    встретившаяся шесть раз»).

    Тоннаж считается по нагрузке (BlockView.load_tonnage), а не по записанному
    весу: подтягивания «0×12» — это не ноль тонн, и зал славы считает их так же.

    `was_backfill` — статус тренировки ДО финиша (после него он у всех
    `finished`). У занесения задним числом милестоун «N-я тренировка» не
    показывается, как и в боте: такие записи вносятся не по порядку, и счётчик
    по ним сообщал бы не то, что человек подумает.
    """
    blocks = await view_builder.build_block_views(workout["id"], user["e1rm_formula"])
    tonnage = sum(block.load_tonnage for block in blocks)
    with i18n.use_lang(user["lang"]):
        milestone = None
        if not was_backfill:
            total_finished = await db.count_workouts(workout["user_id"])
            if analytics.is_workout_milestone(total_finished):
                milestone = _plain(formatting.format_milestone_line(total_finished))
        promotion = await dashboard_data.rank_promotion(workout["user_id"], user)
        return {
            "sets": sum(len(block.sets) for block in blocks),
            "exercises": len(blocks),
            "tonnage": _plain(formatting.format_tonnage(tonnage, user["unit"])),
            "tonnage_equivalent": _plain(
                formatting.format_tonnage_equivalent(
                    tonnage, seed=workout["id"], unit=user["unit"]
                )
            ),
            "new_achievements": [
                {
                    "code": achievements.BY_CODE[code].code,
                    "name": _plain(achievements.BY_CODE[code].title),
                    "description": _plain(achievements.BY_CODE[code].description),
                }
                for code in new_codes
                if code in achievements.BY_CODE
            ],
            "rank_promotion": (
                None if promotion is None
                else {
                    "name": _plain(promotion.name),
                    "level": promotion.level,
                    "emoji": promotion.emoji,
                }
            ),
            "milestone": milestone,
        }


async def finish_workout(request: Request) -> JSONResponse:
    """Завершить тренировку и вернуть её же — плюс `rewards` с итогами сессии.

    Итоги отдаются тут, а не отдельным чтением, потому что завершение — лучший
    момент сессии, и приложение строит карточку сразу: тоннаж «как N слонов»,
    новые значки, повышение звания, милестоун. Второй запрос за ними означал бы
    пустую карточку на время его полёта.

    Исключение — комментарий AI-тренера: он приходит от модели и к моменту
    ответа существовать не может. Генерация запускается отсюда в фон
    (_spawn_ai_comment), а забирается отдельным GET /workouts/{id}/ai-comment.
    """
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    workout = await _owned_workout(workout_id, user_id)
    _require_open(workout)
    body = await _json_body(request) if await request.body() else {}
    # "note" отсутствует в теле — не значит "очисти её": iOS зовёт finish без
    # note, когда её уже поставили раньше через PATCH /note, и молчаливая
    # перезапись на NULL стёрла бы то, что пользователь только что написал.
    note = body["note"] if "note" in body else workout["note"]
    # У занесения задним числом время окончания берётся из его же даты, а не
    # с часов сервера: иначе тренировка за прошлый вторник закончилась бы
    # сегодня и растянулась в истории на неделю.
    started_at = dt.datetime.fromisoformat(workout["started_at"])
    finished_at = (
        f"{started_at.date().isoformat()}{BACKFILL_HOUR}"
        if workout["status"] == "backfill"
        else None
    )
    await db.delete_empty_blocks(workout_id)
    # Пустая тренировка не сохраняется — она удаляется, ровно как в боте
    # (handlers.workout, сообщение workout.empty_deleted). Строка «Без
    # упражнений · 0 подходов» в истории не сообщает ничего, кроме того, что
    # человек открыл экран и передумал, а портит она и список, и все счётчики,
    # которые считают тренировки, — включая серию недель и звание.
    if not await db.list_exercise_ids_for_workout(workout_id):
        await db.discard_workout(workout_id)
        return JSONResponse({"discarded": True, "reason": "empty"})
    if not await db.finish_workout(workout_id, note=note, finished_at=finished_at):
        # Тренировку успели закончить с другого клиента, пока шёл этот запрос.
        raise ApiError(409, "workout_finished", "workout is already finished")
    # Значки присваиваются здесь, а не при чтении экрана достижений: тем же
    # вызовом, что и в боте (handlers.workout), с теми же агрегатами. Без него
    # у человека, который пользуется только приложением, сетка достижений не
    # заполнилась бы никогда.
    #
    # Длительности у занесения задним числом нет и быть не может — форму
    # заполняют потом, — поэтому `None`: значки «за длинную тренировку»
    # такая запись честно не получает, ровно как в боте.
    duration_seconds = None
    if workout["status"] == "active" and workout["started_at"]:
        finished = dt.datetime.fromisoformat((await db.get_workout(workout_id))["finished_at"])
        duration_seconds = (finished - started_at).total_seconds()
    new_codes = await achievement_sync.evaluate_after_finish(
        user_id, workout_id, started_at, duration_seconds
    )
    was_backfill = workout["status"] == "backfill"
    workout = await db.get_workout(workout_id)
    user = await db.get_user(user_id)
    payload = await _workout_detail_json(workout, user)
    payload["rewards"] = await _finish_rewards_json(workout, user, new_codes, was_backfill)
    _spawn_ai_comment(user_id, workout_id, user, workout)
    return JSONResponse(payload)


async def get_ai_comment(request: Request) -> JSONResponse:
    """Комментарий AI-тренера к тренировке — `{"comment": ...}` или `null` в нём.

    Отдельным чтением, а не полем в ответе finish: комментарий генерирует
    модель, это секунды, и ждать их финишем значило бы держать человека на
    крутилке ровно в тот момент, ради которого он и жал «Завершить». Бот решает
    то же самое так же — отправляет карточку сразу и дописывает комментарий
    правкой сообщения позже (handlers.workout._attach_ai_comment). Приложению
    редактировать нечего, поэтому оно опрашивает этот адрес через несколько
    секунд после финиша.

    `null` — это три разных состояния разом: ещё генерируется, генерация
    сорвалась, комментарии выключены (тумблером или отсутствующим ключом
    провайдера). Клиенту от них нужно одно и то же — не показывать блок, — а
    различать их значило бы выставить наружу внутренности AI-слоя.
    """
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    workout = await _owned_workout(workout_id, user_id)
    return JSONResponse({"comment": workout["ai_comment"]})


async def update_exercise_note(request: Request) -> JSONResponse:
    """Заметка к одному упражнению ВНУТРИ тренировки («📝 Заметка» на живом
    экране, live:note в handlers/workout.py) — не путать с PATCH .../note
    выше, которая правит заметку ко всей тренировке. Ключ — пара
    (workout_id, exercise_id), хранилище то же самое, что у бота
    (db.set_workout_exercise_note/exercise_notes), поэтому запись из
    приложения сразу видна в живом трекере бота и наоборот.

    Тренировку можно уже закончить — заметка техники относится к прошедшей
    сессии не хуже, чем к идущей, и `_require_open` здесь нарочно не зовётся,
    ровно как у update_note для заметки всей тренировки."""
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    exercise_id = int(request.path_params["exercise_id"])
    await _owned_workout(workout_id, user_id)
    await _owned_exercise(exercise_id, user_id)
    # Требовать, чтобы упражнение уже было в тренировке, нельзя: заметку
    # пишут РАНЬШЕ первого подхода — «болит плечо, следи за локтями»
    # появляется до того, как заведён блок. Проверка «есть блок» ломала бы
    # ровно тот случай, ради которого заметка и нужна, поэтому её здесь нет,
    # в отличие от delete_last_set: там блок обязан существовать по смыслу
    # операции.
    body = await _json_body(request)
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise ApiError(400, "bad_request", "note must be a string or null")
    await db.set_workout_exercise_note(workout_id, exercise_id, note)
    # Пустая строка чистит заметку так же, как None (см. db.set_workout_
    # exercise_note) — перечитываем сохранённое, а не отдаём эхо тела запроса,
    # чтобы "" в ответе не выглядело действующей заметкой.
    saved = await db.get_workout_exercise_note(workout_id, exercise_id)
    return JSONResponse({"exercise_id": exercise_id, "note": saved})


async def update_note(request: Request) -> JSONResponse:
    """Правка заметки уже сохранённой тренировки — «📝 Заметка» на карточке
    завершения в боте работает так же: заметку можно поставить или переписать
    и после финиша, не только в момент его."""
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    await _owned_workout(workout_id, user_id)
    body = await _json_body(request)
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise ApiError(400, "bad_request", "note must be a string or null")
    await db.update_workout_note(workout_id, note)
    workout = await db.get_workout(workout_id)
    return JSONResponse(await _workout_detail_json(workout))


async def _history_extras_by_exercise(workout, user) -> dict[int, dict[str, Any]]:
    """Готовая строка рекорда 🔥 и подходы прошлой сессии на упражнение — тем
    же путём, что карточка бота: view_builder.build_block_views(mark_records=True)
    с previous_before уже считает и то, и другое за один проход, и
    _workout_detail_json раньше выбрасывал вторую половину (`prev_sets`),
    хотя история бота её печатает под каждым упражнением («[прошлая: …]»).

    `record_text` собирается formatting.format_block_record — второй раз эта
    фраза нигде не пишется, она живёт в locales/*.json одним экземпляром.
    `previous_sets_text` — то же самое форматирование подхода
    (formatting.format_set), что и у самой строки записанных сегодня: без
    обёртки «[прошлая: …]» бота (там это часть иконки-тегом всего блока),
    здесь клиент подписывает её своей меткой в интерфейсе.

    `show_extra=user["show_extra_stats"]` — та же тонкость, что у бота: рекорд
    e1RM молчит при выключенных доп. цифрах, а рекорд повторов виден всегда
    (см. докстринг format_block_record). Приложение обязано вести себя так же,
    иначе человек с выключенными доп. цифрами увидел бы в iOS то, что бот ему
    принципиально не показывает.

    Считается только для законченной тренировки: `previous_before` берёт
    прошлую сессию упражнения строго ДО этой (handlers.workout._finished_summary
    делает так же), а для ещё идущей тренировки сравнивать не с чем.

    `e1rm_text` — готовая строка «↳ e1RM …» (formatting.format_block_e1rm), тем
    же способом и по тем же правилам, что record_text: собирается сервером
    целиком (формула e1RM и порог зависят от настроек аккаунта), молчит без
    доп. цифр и, в отличие от record_text, только у завершённой тренировки —
    как в карточке бота, где эта строка есть лишь в итоговом тексте.
    """
    finished = workout["status"] == "finished"
    with i18n.use_lang(user["lang"]):
        blocks = await view_builder.build_block_views(
            workout["id"],
            user["e1rm_formula"],
            previous_before=workout["started_at"],
            mark_records=True,
        )
        show_extra = bool(user["show_extra_stats"])
        extras: dict[int, dict[str, Any]] = {}
        for block in blocks:
            previous_sets_text = None
            if block.prev_sets:
                formatted = [
                    formatting.format_set(w, r, block.prev_rpe_for(i))
                    for i, (w, r) in enumerate(block.prev_sets)
                ]
                previous_sets_text = ", ".join(formatted)
            extras[block.exercise_id] = {
                "record_text": formatting.format_block_record(block, user["unit"], show_extra),
                "previous_sets_text": previous_sets_text,
                "e1rm_text": (
                    formatting.format_block_e1rm(block, user["unit"], show_extra) if finished else None
                ),
            }
        return extras


async def _workout_detail_json(workout, user=None) -> dict[str, Any]:
    """`user` задан у GET /workouts/{id} и у ответа finish — тогда каждое
    упражнение получает `record_text`/`has_record`/`previous_sets_text`
    (см. _history_extras_by_exercise). У прочих ручек (активная/бэкофилл-
    тренировка, правка заметки) `user` не передаётся: тренировка ещё не
    завершена, и этим полям взяться неоткуда — прошлое сравнивается только
    с уже закрытой сессией.

    `group_tag` — готовый тег группы мышц у названия упражнения, тем же
    способом и в том же регистре, что бот печатает в карточке и истории
    (formatting.format_group_tag: локализованное имя капсом, скобки — на
    вызывающем). Нужен и активной/бэкофилл-тренировке тоже (экран
    предупреждения о зависшей тренировке показывает именно её), поэтому
    считается не из extras_by_exercise, а всегда — язык берётся у `user`,
    а если его не передали, подгружается тем же способом, что и для
    gold_formula ниже.
    """
    data = _workout_json(workout)
    extras_by_exercise = await _history_extras_by_exercise(workout, user) if user is not None else {}
    lang_user = user or await db.get_user(workout["user_id"])
    group_tag_cache: dict[int, str] = {}

    async def group_tag_for(exercise_id: int) -> Optional[str]:
        exercise = await db.get_exercise(exercise_id)
        group_id = exercise["primary_group_id"] if exercise else None
        if group_id is None:
            return None
        if group_id not in group_tag_cache:
            group = await db.get_muscle_group(group_id)
            if group is None:
                return None
            with i18n.use_lang(lang_user["lang"]):
                group_tag_cache[group_id] = formatting.format_group_tag(group["name"])
        return group_tag_cache[group_id]

    # 🥇 — тот же gold_index, что бот считает для живого трекера
    # (handlers.workout._refresh_live, mark_golds=True): единственный сет ЭТОЙ
    # тренировки, который бьёт до-этой-тренировки личный рекорд e1RM. Только
    # для ещё идущей тренировки — у бота это только live-экран, финальная
    # карточка отдаёт текстовый 🔥 (record_text) вместо него.
    gold_formula = None
    if workout["status"] != "finished":
        gold_formula = lang_user["e1rm_formula"]
    blocks_json = []
    for block in await db.list_blocks_for_workout(workout["id"]):
        exercises_json = []
        for be in await db.get_block_exercises(block["id"]):
            sets = await db.list_sets_for_block(block["id"])
            own_sets = [s for s in sets if s["exercise_id"] == be["exercise_id"]]
            extras = extras_by_exercise.get(be["exercise_id"], {})
            record_text = extras.get("record_text")
            # Заметка к упражнению в ЭТОЙ тренировке (live:note бота) — новое
            # поле, не ломает старых клиентов: они его просто не читают.
            exercise_note = await db.get_workout_exercise_note(workout["id"], be["exercise_id"])
            gold_index = None
            if gold_formula is not None and own_sets:
                previous_best = await db.max_e1rm_before_workout(
                    workout["user_id"], be["exercise_id"], workout["id"], gold_formula
                )
                gold_index = view_builder.best_gold_index(
                    [(db.load_of(s), s["reps"], s["rpe"]) for s in own_sets],
                    previous_best, gold_formula,
                )
            exercises_json.append(
                {
                    "exercise_id": be["exercise_id"],
                    "display_name": be["display_name"],
                    "sets": [
                        {**_set_json(s), "is_gold": i == gold_index}
                        for i, s in enumerate(own_sets)
                    ],
                    "record_text": record_text,
                    "has_record": record_text is not None,
                    "note": exercise_note,
                    "previous_sets_text": extras.get("previous_sets_text"),
                    "e1rm_text": extras.get("e1rm_text"),
                    "group_tag": await group_tag_for(be["exercise_id"]),
                }
            )
        blocks_json.append({"id": block["id"], "type": block["type"], "exercises": exercises_json})
    data["blocks"] = blocks_json
    return data


async def get_workout(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    workout = await _owned_workout(workout_id, user_id)
    user = await db.get_user(user_id)
    return JSONResponse(await _workout_detail_json(workout, user))


async def list_workouts(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    # common.query_int, а не голый int(): «?limit=abc» из кривой ссылки
    # роняло список тренировок пятисоткой вместо внятного 400 — остальные
    # списки (/workouts/search, /food/days) давно разбирают параметры им.
    limit = common.query_int(request, "limit", 20, minimum=1, maximum=100)
    offset = common.query_int(request, "offset", 0, minimum=0)
    workouts = await db.list_workouts(user_id, limit=limit, offset=offset, status="finished")
    contents = await db.list_workout_contents([w["id"] for w in workouts])
    items = []
    for w in workouts:
        exercise_names, set_count = contents.get(w["id"], ([], 0))
        item = _workout_json(w)
        item["exercise_names"] = exercise_names
        item["set_count"] = set_count
        items.append(item)
    return JSONResponse(items)


async def search_workouts(request: Request) -> JSONResponse:
    """Тренировки, где встречается упражнение из `exercise` — «в какой
    тренировке был жим», которое дата-only GET /workouts не отвечает.

    Отдельный маршрут, а не параметр у GET /workouts: у того ответ — голый
    массив (нет места для `total`), а постраничный поиск без общего числа
    совпадений не может ни показать «показано N из M», ни решить, есть ли
    следующая страница, — старые тренировки частого упражнения были бы
    физически недостижимы после первых `limit` штук.

    Расчёт — history_search_data.search, тот же, что и у бота
    (handlers.history._render_search_page поверх него же): второй запрос с
    той же парой db.search_workouts_by_exercise/count_workouts_by_exercise
    разъехался бы с первым при первой же правке.
    """
    user_id = await _authed_user_id(request)
    query = request.query_params.get("exercise", "").strip()
    if not query:
        raise ApiError(400, "bad_request", "exercise must not be empty", key="api.error.name_empty")
    limit = common.query_int(request, "limit", 20, minimum=1, maximum=100)
    offset = common.query_int(request, "offset", 0, minimum=0)
    page = await history_search_data.search(user_id, query, limit=limit, offset=offset)
    items = []
    for it in page.items:
        item = {"id": it.id, "started_at": it.started_at}
        item["exercise_names"] = it.exercise_names
        item["set_count"] = it.set_count
        items.append(item)
    return JSONResponse({"items": items, "total": page.total})


# ---------- вес тела ----------

async def list_bodyweight(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    # Без параметра — вся история (limit=None), с параметром — разобранное
    # число, а не int() над чем попало: «?limit=abc» отвечало 500.
    raw_limit = request.query_params.get("limit")
    limit = (
        common.query_int(request, "limit", 20, minimum=1, maximum=1000)
        if raw_limit
        else None
    )
    rows = await db.list_bodyweight_logs(user_id, limit=limit)
    return JSONResponse(
        [{"id": r["id"], "weight": r["weight"], "logged_at": r["logged_at"]} for r in rows]
    )


async def add_bodyweight(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    body = await _json_body(request)
    weight = float(_require(body, "weight", (int, float)))
    logged_at = body.get("logged_at") or db.now_iso()
    log_id = await db.add_bodyweight_log(user_id, weight, logged_at)
    return JSONResponse({"id": log_id, "weight": weight, "logged_at": logged_at}, status_code=201)


async def update_bodyweight(request: Request) -> JSONResponse:
    """Поправить опечатку в весе, не трогая дату/время взвешивания — тот же
    приём, что у «✏️ Записи» в боте."""
    user_id = await _authed_user_id(request)
    log_id = int(request.path_params["log_id"])
    body = await _json_body(request)
    weight = float(_require(body, "weight", (int, float)))
    updated = await db.update_bodyweight_log(log_id, user_id, weight)
    if not updated:
        raise ApiError(404, "not_found", "bodyweight entry not found")
    return JSONResponse({"id": log_id, "weight": weight})


async def delete_bodyweight(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    log_id = int(request.path_params["log_id"])
    deleted = await db.delete_bodyweight_log(log_id, user_id)
    if not deleted:
        raise ApiError(404, "not_found", "bodyweight entry not found")
    return JSONResponse({"deleted": True})


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


routes = [
    Route("/health", health, methods=["GET"]),
    Route("/auth/link", auth_link, methods=["POST"]),
    Route("/auth/apple", auth_apple, methods=["POST"]),
    Route("/account/telegram-link-code", request_telegram_link_code, methods=["POST"]),
    Route("/me", me, methods=["GET"]),
    Route("/push/register", register_push_token, methods=["POST"]),
    Route("/push/register", unregister_push_token, methods=["DELETE"]),
    Route("/muscle-groups", list_muscle_groups, methods=["GET"]),
    Route("/muscle-groups", create_muscle_group, methods=["POST"]),
    Route("/exercises", list_exercises, methods=["GET"]),
    Route("/exercises", create_exercise, methods=["POST"]),
    Route("/exercises/merge", merge_exercises, methods=["POST"]),
    Route("/exercises/next-suggestions", next_exercise_suggestions, methods=["GET"]),
    Route("/exercises/{exercise_id:int}", update_exercise, methods=["PATCH"]),
    Route("/exercises/{exercise_id:int}/archive", archive_exercise, methods=["POST"]),
    Route("/exercises/{exercise_id:int}/unarchive", unarchive_exercise, methods=["POST"]),
    Route("/exercises/{exercise_id:int}/progress", exercise_progress, methods=["GET"]),
    Route("/exercises/{exercise_id:int}/superset-partners", superset_partners, methods=["GET"]),
    Route("/workouts/active", active_workout, methods=["GET"]),
    Route("/workouts/active", start_workout, methods=["POST"]),
    Route("/workouts/active", discard_active_workout, methods=["DELETE"]),
    Route("/workouts/backfill", backfill_workout, methods=["GET"]),
    Route("/workouts/backfill", start_backfill_workout, methods=["POST"]),
    Route("/workouts/backfill", discard_backfill_workout, methods=["DELETE"]),
    Route("/workouts", list_workouts, methods=["GET"]),
    Route("/workouts/search", search_workouts, methods=["GET"]),
    Route("/workouts/{workout_id:int}", get_workout, methods=["GET"]),
    Route("/workouts/{workout_id:int}/sets", log_set, methods=["POST"]),
    Route("/workouts/{workout_id:int}/sets/parse", log_sets_from_text, methods=["POST"]),
    Route("/workouts/{workout_id:int}/sets/voice", log_set_from_voice, methods=["POST"]),
    Route(
        "/workouts/{workout_id:int}/exercises/{exercise_id:int}/last-set",
        delete_last_set, methods=["DELETE"],
    ),
    Route(
        "/workouts/{workout_id:int}/exercises/{exercise_id:int}/note",
        update_exercise_note, methods=["PATCH"],
    ),
    Route("/workouts/{workout_id:int}/finish", finish_workout, methods=["POST"]),
    Route("/workouts/{workout_id:int}/ai-comment", get_ai_comment, methods=["GET"]),
    Route("/workouts/{workout_id:int}/note", update_note, methods=["PATCH"]),
    Route("/bodyweight", list_bodyweight, methods=["GET"]),
    Route("/bodyweight", add_bodyweight, methods=["POST"]),
    Route("/bodyweight/{log_id:int}", update_bodyweight, methods=["PATCH"]),
    Route("/bodyweight/{log_id:int}", delete_bodyweight, methods=["DELETE"]),
]

# Домены, выросшие из дневника: программы, еда, достижения, AI-тренер. Каждый
# живёт своим модулем — в одном файле это были бы полторы тысячи строк, где
# правка в еде соседствует с правкой в программах.
routes += (
    api_v1_programs.routes
    + api_v1_food.routes
    + api_v1_achievements.routes
    + api_v1_ai.routes
    + api_v1_account.routes
    + api_v1_import.routes
    + api_v1_sharing.routes
    + api_v1_media.routes
    + api_v1_feedback.routes
    + api_v1_dashboard.routes
    + api_v1_progress.routes
    + api_v1_hall_of_fame.routes
    + api_v1_history.routes
    + api_v1_templates.routes
)


def build_app() -> Starlette:
    return Starlette(
        routes=routes,
        # Лог действий — одной middleware поверх всех маршрутов, а не записью в
        # каждом обработчике: их под сотню, и строку, которую надо не забыть
        # дописать в новый, забывают на первом же. Список маршрутов передаём
        # внутрь, чтобы middleware знала ШАБЛОН пути, а не только сам путь с
        # id (см. api_v1_activity).
        middleware=[
            # Снаружи всего — чтобы в замер вошли и сжатие, и лог действий.
            Middleware(server_timing.ServerTimingMiddleware, routes=routes),
            # JSON истории, прогресса, каталога упражнений — десятки килобайт
            # одинаковых ключей, и по мобильной сети сжатый ответ приходит
            # заметно быстрее. Меньше килобайта не жмём: заголовки дороже
            # выигрыша. Уже сжатое (фото, видео, аудио) и частичные ответы
            # (Range, 206) GZipMiddleware сам пропускает как есть.
            #
            # Внутри LogApiActions, а не снаружи: BaseHTTPMiddleware отдаёт
            # тело кусками с more_body=True, и GZip снаружи принимал бы любой
            # ответ, даже `{"status": "ok"}`, за поток и жал бы его целиком,
            # без Content-Length. Здесь он видит ответ обработчика как есть.
            Middleware(api_v1_activity.LogApiActions, routes=routes),
            Middleware(GZipMiddleware, minimum_size=1024, compresslevel=6),
        ],
        exception_handlers={
            ApiError: _api_error_handler,
            Exception: _unhandled_error_handler,
        },
    )
