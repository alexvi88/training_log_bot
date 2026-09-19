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
import api_v1_import
import api_v1_media
import api_v1_programs
import api_v1_progress
import api_v1_sharing
import apple_signin
import dashboard_data
import db
import formatting
import i18n
import mcp_oauth
import parser
import timeutil
import view_builder

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

def _exercise_json(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "original_name": row["original_name"],
        "primary_group_id": row["primary_group_id"],
        "equipment": row["equipment"],
        "unilateral": bool(row["unilateral"]),
        "attachment": row["attachment"],
        "bodyweight_load": row["bodyweight_load"],
    }


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
    """
    body = await _json_body(request)
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
            new_user = await db.create_app_only_user()
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
    user_id = await _authed_user_id(request)
    user = await db.get_user(user_id)
    if user is None:
        raise ApiError(404, "not_found", "user not found")
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
    user_id = await _authed_user_id(request)
    groups = await db.list_muscle_groups(user_id, order_by_usage=True)
    return JSONResponse(
        [
            {"id": g["id"], "name": g["name"], "emoji": g["emoji"]}
            for g in groups
        ]
    )


async def create_muscle_group(request: Request) -> JSONResponse:
    """Своя группа мышц — не у всех каталог бота совпадает с тем, как
    называют группы в конкретном зале."""
    user_id = await _authed_user_id(request)
    body = await _json_body(request)
    name = str(_require(body, "name", str)).strip()
    if not name:
        raise ApiError(400, "bad_request", "name must not be empty")
    emoji = body.get("emoji")
    if emoji is not None and not isinstance(emoji, str):
        raise ApiError(400, "bad_request", "emoji must be a string")
    group_id = await db.create_muscle_group(user_id, name, emoji)
    group = await db.get_muscle_group(group_id)
    return JSONResponse({"id": group["id"], "name": group["name"], "emoji": group["emoji"]}, status_code=201)


async def list_exercises(request: Request) -> JSONResponse:
    """Три режима, как в боте: по группе мышц (обзор для тех, кто не помнит
    точное название), текстовым поиском, или весь каталог. group_id и query
    вместе не имеют смысла — group_id побеждает, раз пришёл."""
    user_id = await _authed_user_id(request)
    group_id_param = request.query_params.get("group_id")
    query = request.query_params.get("query")
    if group_id_param:
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
        raise ApiError(400, "bad_request", "name must not be empty")
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


async def _owned_exercise(exercise_id: int, user_id: int):
    exercise = await db.get_exercise(exercise_id)
    if exercise is None or exercise["user_id"] != user_id:
        raise ApiError(404, "not_found", "exercise not found")
    return exercise


async def _find_block_for_exercise(workout_id: int, exercise_id: int) -> Optional[int]:
    for block in await db.list_blocks_for_workout(workout_id):
        for be in await db.get_block_exercises(block["id"]):
            if be["exercise_id"] == exercise_id:
                return block["id"]
    return None


async def _block_for_exercise(workout_id: int, exercise_id: int) -> int:
    """Блок этого упражнения в тренировке — существующий (первый попавшийся,
    без суперсетов на этом этапе) или новый одиночный."""
    existing = await _find_block_for_exercise(workout_id, exercise_id)
    if existing is not None:
        return existing
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, exercise_id, 0)
    return block_id


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


def _parse_date(raw: Any) -> dt.date:
    if not isinstance(raw, str):
        raise ApiError(400, "bad_request", "date must be a string YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise ApiError(400, "bad_request", "date must be YYYY-MM-DD") from exc


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
    """
    user_id = await _authed_user_id(request)
    body = await _json_body(request)
    date = _parse_date(_require(body, "date", str))
    # Будущим днём тренировки не бывает: это занесение того, что уже сделано.
    # Сегодня — по часовому поясу пользователя, а не по UTC сервера.
    user = await db.get_user(user_id)
    if date > timeutil.user_today(user):
        raise ApiError(400, "bad_request", "date is in the future")

    existing = await db.get_backfill_workout(user_id)
    if existing is not None:
        return JSONResponse(await _workout_detail_json(existing), status_code=200)

    workout_id = await db.create_workout(
        user_id, started_at=f"{date.isoformat()}{BACKFILL_HOUR}", status="backfill"
    )
    workout = await db.get_workout(workout_id)
    return JSONResponse(await _workout_detail_json(workout), status_code=201)


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
    weight = float(_require(body, "weight", (int, float)))
    reps = _require(body, "reps", int)
    rpe = body.get("rpe")
    if rpe is not None:
        rpe = float(rpe)
    await _owned_exercise(exercise_id, user_id)
    block_id = await _block_for_exercise(workout_id, exercise_id)
    set_id = await db.append_set(block_id, exercise_id, 0, weight, reps, rpe)
    cur = await db.conn().execute("SELECT * FROM sets WHERE id = ?", (set_id,))
    row = await cur.fetchone()
    return JSONResponse(_set_json(row), status_code=201)


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
        raise ApiError(400, "bad_request", "text must not be empty")
    await _owned_exercise(exercise_id, user_id)

    user = await db.get_user(user_id)
    with i18n.use_lang(user["lang"] if user else "ru"):
        try:
            parsed = parser.parse_sets_line(text)
        except parser.ParseError as exc:
            # Машинный код всё равно есть — клиенту иногда надо отличить
            # «не разобрал» от «сервер лёг», — но показывать он должен message.
            raise ApiError(400, "unparsed_input", exc.message) from exc

    # Голые повторы («8») означают «тот же вес, что в прошлом подходе». Какой
    # это вес, знает сервер, а не клиент: иначе приложение считало бы
    # предыдущий подход само и расходилось бы с ботом на суперсетах.
    previous = await db.list_sets_for_workout_exercise(workout_id, exercise_id)
    prev_weight = previous[-1]["weight"] if previous else 0.0

    block_id = await _block_for_exercise(workout_id, exercise_id)
    created = []
    for item in parsed:
        weight = prev_weight if (item.weight_omitted and prev_weight) else item.weight
        set_id = await db.append_set(block_id, exercise_id, 0, weight, item.reps, item.rpe)
        prev_weight = weight
        cur = await db.conn().execute("SELECT * FROM sets WHERE id = ?", (set_id,))
        created.append(_set_json(await cur.fetchone()))
    return JSONResponse({"sets": created}, status_code=201)


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
                else {"name": _plain(promotion.name), "level": promotion.level}
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
    payload = await _workout_detail_json(workout)
    user = await db.get_user(user_id)
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


async def _workout_detail_json(workout) -> dict[str, Any]:
    data = _workout_json(workout)
    blocks_json = []
    for block in await db.list_blocks_for_workout(workout["id"]):
        exercises_json = []
        for be in await db.get_block_exercises(block["id"]):
            sets = await db.list_sets_for_block(block["id"])
            own_sets = [s for s in sets if s["exercise_id"] == be["exercise_id"]]
            exercises_json.append(
                {
                    "exercise_id": be["exercise_id"],
                    "display_name": be["display_name"],
                    "sets": [_set_json(s) for s in own_sets],
                }
            )
        blocks_json.append({"id": block["id"], "type": block["type"], "exercises": exercises_json})
    data["blocks"] = blocks_json
    return data


async def get_workout(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    workout = await _owned_workout(workout_id, user_id)
    return JSONResponse(await _workout_detail_json(workout))


async def list_workouts(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    limit = min(int(request.query_params.get("limit", 20)), 100)
    offset = max(int(request.query_params.get("offset", 0)), 0)
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


# ---------- вес тела ----------

async def list_bodyweight(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    limit = request.query_params.get("limit")
    rows = await db.list_bodyweight_logs(user_id, limit=int(limit) if limit else None)
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
    Route("/exercises/{exercise_id:int}/progress", exercise_progress, methods=["GET"]),
    Route("/workouts/active", active_workout, methods=["GET"]),
    Route("/workouts/active", start_workout, methods=["POST"]),
    Route("/workouts/active", discard_active_workout, methods=["DELETE"]),
    Route("/workouts/backfill", backfill_workout, methods=["GET"]),
    Route("/workouts/backfill", start_backfill_workout, methods=["POST"]),
    Route("/workouts/backfill", discard_backfill_workout, methods=["DELETE"]),
    Route("/workouts", list_workouts, methods=["GET"]),
    Route("/workouts/{workout_id:int}", get_workout, methods=["GET"]),
    Route("/workouts/{workout_id:int}/sets", log_set, methods=["POST"]),
    Route("/workouts/{workout_id:int}/sets/parse", log_sets_from_text, methods=["POST"]),
    Route(
        "/workouts/{workout_id:int}/exercises/{exercise_id:int}/last-set",
        delete_last_set, methods=["DELETE"],
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
)


def build_app() -> Starlette:
    return Starlette(
        routes=routes,
        # Лог действий — одной middleware поверх всех маршрутов, а не записью в
        # каждом обработчике: их под сотню, и строку, которую надо не забыть
        # дописать в новый, забывают на первом же. Список маршрутов передаём
        # внутрь, чтобы middleware знала ШАБЛОН пути, а не только сам путь с
        # id (см. api_v1_activity).
        middleware=[Middleware(api_v1_activity.LogApiActions, routes=routes)],
        exception_handlers={
            ApiError: _api_error_handler,
            Exception: _unhandled_error_handler,
        },
    )
