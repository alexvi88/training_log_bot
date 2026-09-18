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

import logging
from typing import Any, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import db
import mcp_oauth

logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message


async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status_code)


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("api_v1: unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"error": "internal_error", "message": "internal error"}, status_code=500)


async def _authed_user_id(request: Request) -> int:
    """Bearer-токен → telegram_id, либо ApiError(401)."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise ApiError(401, "unauthorized", "missing bearer token")
    token = auth[len("bearer "):].strip()
    user_id = await db.resolve_api_token(token)
    if user_id is None:
        raise ApiError(401, "unauthorized", "invalid or revoked token")
    return user_id


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise ApiError(400, "bad_request", "invalid JSON body") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", "JSON object expected")
    return body


def _require(body: dict[str, Any], key: str, expected_type: type) -> Any:
    if key not in body:
        raise ApiError(400, "bad_request", f"missing field: {key}")
    value = body[key]
    if not isinstance(value, expected_type) or isinstance(value, bool) and expected_type is not bool:
        raise ApiError(400, "bad_request", f"field {key} must be {expected_type.__name__}")
    return value


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
        }
    )


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


async def active_workout(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    workout = await db.get_active_workout(user_id)
    if workout is None:
        return JSONResponse(None)
    return JSONResponse(await _workout_detail_json(workout))


async def start_workout(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    workout_id, created = await db.get_or_create_active_workout(user_id)
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


async def log_set(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    workout = await _owned_workout(workout_id, user_id)
    if workout["status"] != "active":
        raise ApiError(409, "workout_finished", "workout is already finished")
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


async def delete_last_set(request: Request) -> JSONResponse:
    """Убрать последний подход этого упражнения — правка опечатки веса/повторов
    сразу после записи, тем же приёмом, что «↩️ Отменить» в боте."""
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    exercise_id = int(request.path_params["exercise_id"])
    workout = await _owned_workout(workout_id, user_id)
    if workout["status"] != "active":
        raise ApiError(409, "workout_finished", "workout is already finished")
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


async def finish_workout(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    workout = await _owned_workout(workout_id, user_id)
    if workout["status"] != "active":
        raise ApiError(409, "workout_finished", "workout is already finished")
    body = await _json_body(request) if await request.body() else {}
    # "note" отсутствует в теле — не значит "очисти её": iOS зовёт finish без
    # note, когда её уже поставили раньше через PATCH /note, и молчаливая
    # перезапись на NULL стёрла бы то, что пользователь только что написал.
    note = body["note"] if "note" in body else workout["note"]
    await db.finish_workout(workout_id, note=note)
    workout = await db.get_workout(workout_id)
    return JSONResponse(await _workout_detail_json(workout))


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
    Route("/me", me, methods=["GET"]),
    Route("/muscle-groups", list_muscle_groups, methods=["GET"]),
    Route("/muscle-groups", create_muscle_group, methods=["POST"]),
    Route("/exercises", list_exercises, methods=["GET"]),
    Route("/exercises", create_exercise, methods=["POST"]),
    Route("/exercises/{exercise_id:int}/progress", exercise_progress, methods=["GET"]),
    Route("/workouts/active", active_workout, methods=["GET"]),
    Route("/workouts/active", start_workout, methods=["POST"]),
    Route("/workouts/active", discard_active_workout, methods=["DELETE"]),
    Route("/workouts", list_workouts, methods=["GET"]),
    Route("/workouts/{workout_id:int}", get_workout, methods=["GET"]),
    Route("/workouts/{workout_id:int}/sets", log_set, methods=["POST"]),
    Route(
        "/workouts/{workout_id:int}/exercises/{exercise_id:int}/last-set",
        delete_last_set, methods=["DELETE"],
    ),
    Route("/workouts/{workout_id:int}/finish", finish_workout, methods=["POST"]),
    Route("/workouts/{workout_id:int}/note", update_note, methods=["PATCH"]),
    Route("/bodyweight", list_bodyweight, methods=["GET"]),
    Route("/bodyweight", add_bodyweight, methods=["POST"]),
    Route("/bodyweight/{log_id:int}", update_bodyweight, methods=["PATCH"]),
    Route("/bodyweight/{log_id:int}", delete_bodyweight, methods=["DELETE"]),
]


def build_app() -> Starlette:
    return Starlette(
        routes=routes,
        exception_handlers={
            ApiError: _api_error_handler,
            Exception: _unhandled_error_handler,
        },
    )
