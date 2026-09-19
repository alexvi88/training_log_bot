"""REST `/v1` для программ и дней тренировок (домен бота — handlers/routines.py).

Транспорт поверх db.py, как и api_v1.py: вся бизнес-логика (уникальность имён,
лимит дней, порядок дней/упражнений, каскадное удаление) уже в db, здесь только
разбор запроса, проверка владения по user_id и сборка JSON.

Владение проверяется на каждый id из URL, до любого действия с ним:
- программа — по `programs.user_id` напрямую;
- день (routine) — по `routines.user_id` напрямую (он не наследуется от
  программы: у standalone-дня программы вообще нет);
- строка routine_exercises — по цепочке к её routine.user_id, у самой строки
  своего user_id нет.

Без этого чужой объект читался/правился/удалялся бы по угаданному id — это и
есть главный риск такого API, и именно на него в tests/test_api_v1_programs.py
отдельные проверки на каждый маршрут.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import api_v1_common as common
import config
import db
import formatting

ApiError = common.ApiError
_authed_user_id = common.authed_user_id
_json_body = common.json_body
_require = common.require
_optional_str = common.optional_str


# ---------- владение ----------

async def _owned_program(program_id: int, user_id: int):
    program = await db.get_program(program_id)
    if program is None or program["user_id"] != user_id:
        raise ApiError(404, "not_found", "program not found")
    return program


async def _owned_routine(routine_id: int, user_id: int):
    routine = await db.get_routine(routine_id)
    if routine is None or routine["user_id"] != user_id:
        raise ApiError(404, "not_found", "routine not found")
    return routine


async def _owned_routine_exercise(item_id: int, user_id: int):
    """routine_exercises не хранит user_id — владелец только через её routine."""
    entry = await db.get_routine_exercise(item_id)
    if entry is None:
        raise ApiError(404, "not_found", "routine exercise not found")
    routine = await db.get_routine(entry["routine_id"])
    if routine is None or routine["user_id"] != user_id:
        raise ApiError(404, "not_found", "routine exercise not found")
    return entry


async def _owned_exercise(exercise_id: int, user_id: int):
    exercise = await db.get_exercise(exercise_id)
    if exercise is None or exercise["user_id"] != user_id:
        raise ApiError(404, "not_found", "exercise not found")
    return exercise


# ---------- имя дня/программы ----------

def _clean_name(raw: str) -> str:
    """Тот же потолок длины, которым бот режет имена программ и дней
    (config.MAX_PROGRAM_NAME_LENGTH — общий, отдельного под routine нет)."""
    name = raw.strip()
    if not name:
        raise ApiError(400, "bad_request", "name must not be empty")
    if len(name) > config.MAX_PROGRAM_NAME_LENGTH:
        raise ApiError(400, "name_too_long", f"name must be at most {config.MAX_PROGRAM_NAME_LENGTH} chars")
    return name


async def _check_routine_budget(user_id: int, adding: int) -> None:
    """403, а не тихий обход: тот же db.routine_budget, что у бота, и без
    исключения на этот транспорт — иначе через приложение можно завести
    больше дней, чем разрешает бот."""
    over_budget = await db.routine_budget(user_id, adding)
    if over_budget:
        raise ApiError(403, "routine_limit_reached", over_budget)


# ---------- сериализация ----------

def _program_list_json(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "source": row["source"],
        "day_count": row["day_count"],
        "last_trained_at": row["last_trained_at"],
    }


def _program_detail_json(program, days) -> dict[str, Any]:
    return {
        "id": program["id"],
        "name": program["name"],
        "description": program["description"],
        "created_at": program["created_at"],
        "source": program["source"],
        "source_ref": program["source_ref"],
        "days": [_routine_list_json(d) for d in days],
    }


def _routine_base_json(row) -> dict[str, Any]:
    """Общие поля дня — общие для db.get_routine (без счётчика упражнений) и
    "считанных" выборок (list_routines/list_standalone_routines/
    list_program_days_by_id), у которых столбец exercise_count есть."""
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "program_id": row["program_id"],
        "program_name": row["program_name"],
        "day_order": row["day_order"],
    }


def _routine_list_json(row) -> dict[str, Any]:
    """Только для строк из "считанных" выборок — у них есть exercise_count."""
    data = _routine_base_json(row)
    data["exercise_count"] = row["exercise_count"]
    return data


def _routine_exercise_json(row, display_name: str | None = None) -> dict[str, Any]:
    """`row` — либо строка из db.list_routine_exercises (с display_name уже в
    join), либо голая db.get_routine_exercise/get_routine_exercise-подобная
    строка без него: тогда имя передают отдельно (см. _routine_exercise_json_full)."""
    progression = row["progression"]
    try:
        name = row["display_name"]
    except IndexError:
        name = display_name
    return {
        "id": row["id"],
        "routine_id": row["routine_id"],
        "exercise_id": row["exercise_id"],
        "display_name": name,
        "order_index": row["order_index"],
        "target": row["target"],
        "progression": json.loads(progression) if progression else None,
    }


async def _routine_exercise_json_full(entry) -> dict[str, Any]:
    """Для db.get_routine_exercise, у которого нет join на exercises —
    имя дотягивается отдельным запросом."""
    exercise = await db.get_exercise(entry["exercise_id"])
    display_name = exercise["display_name"] if exercise is not None else None
    return _routine_exercise_json(entry, display_name)


async def _routine_detail_json(routine) -> dict[str, Any]:
    """`routine` приходит из db.get_routine — без счётчика exercise_count,
    его отдаёт длина уже загруженного списка упражнений, второй запрос не нужен."""
    exercises = await db.list_routine_exercises(routine["id"])
    data = _routine_base_json(routine)
    data["exercise_count"] = len(exercises)
    data["exercises"] = [_routine_exercise_json(e) for e in exercises]
    return data


# ---------- программы ----------

async def list_programs(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    programs = await db.list_programs(user_id)
    return JSONResponse([_program_list_json(p) for p in programs])


async def create_program(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    body = await _json_body(request)
    name = _clean_name(str(_require(body, "name", str)))
    description = _optional_str(body, "description")
    program_id = await db.create_program(user_id, name, description=description)
    if program_id is None:
        raise ApiError(409, "name_taken", "a program with this name already exists")
    program = await db.get_program(program_id)
    return JSONResponse(_program_detail_json(program, []), status_code=201)


async def get_program(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    program_id = int(request.path_params["program_id"])
    program = await _owned_program(program_id, user_id)
    days = await db.list_program_days_by_id(program_id)
    return JSONResponse(_program_detail_json(program, days))


async def update_program(request: Request) -> JSONResponse:
    """Имя и описание правятся независимо (см. db.set_program_description):
    коллизия имени не должна ронять уже принятое описание из того же тела."""
    user_id = await _authed_user_id(request)
    program_id = int(request.path_params["program_id"])
    await _owned_program(program_id, user_id)
    body = await _json_body(request)
    name = _optional_str(body, "name")
    if name is not None:
        renamed = await db.rename_program_by_id(program_id, _clean_name(name))
        if not renamed:
            raise ApiError(409, "name_taken", "a program with this name already exists")
    if "description" in body:
        await db.set_program_description(program_id, _optional_str(body, "description"))
    program = await _owned_program(program_id, user_id)
    days = await db.list_program_days_by_id(program_id)
    return JSONResponse(_program_detail_json(program, days))


async def delete_program(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    program_id = int(request.path_params["program_id"])
    await _owned_program(program_id, user_id)
    await db.delete_program_by_id(program_id)
    return JSONResponse({"deleted": True})


async def program_next_day(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    program_id = int(request.path_params["program_id"])
    await _owned_program(program_id, user_id)
    day = await db.next_program_day(program_id)
    return JSONResponse(_routine_list_json(day) if day is not None else None)


async def add_program_day(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    program_id = int(request.path_params["program_id"])
    await _owned_program(program_id, user_id)
    body = await _json_body(request)
    name = _clean_name(str(_require(body, "name", str)))
    await _check_routine_budget(user_id, 1)
    routine_id = await db.create_routine(user_id, name, program_id=program_id)
    routine = await db.get_routine(routine_id)
    data = _routine_base_json(routine)
    data["exercise_count"] = 0  # только что создан, упражнений ещё нет
    return JSONResponse(data, status_code=201)


# ---------- самостоятельные дни (routines) ----------

async def list_routines(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    routines = await db.list_standalone_routines(user_id)
    return JSONResponse([_routine_list_json(r) for r in routines])


async def create_routine(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    body = await _json_body(request)
    name = _clean_name(str(_require(body, "name", str)))
    await _check_routine_budget(user_id, 1)
    routine_id = await db.create_routine(user_id, name)
    routine = await db.get_routine(routine_id)
    data = _routine_base_json(routine)
    data["exercise_count"] = 0  # только что создан, упражнений ещё нет
    return JSONResponse(data, status_code=201)


async def get_routine(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    routine_id = int(request.path_params["routine_id"])
    routine = await _owned_routine(routine_id, user_id)
    return JSONResponse(await _routine_detail_json(routine))


async def update_routine(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    routine_id = int(request.path_params["routine_id"])
    await _owned_routine(routine_id, user_id)
    body = await _json_body(request)
    name = _optional_str(body, "name")
    if name is not None:
        await db.rename_routine(routine_id, _clean_name(name))
    routine = await _owned_routine(routine_id, user_id)
    return JSONResponse(await _routine_detail_json(routine))


async def delete_routine(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    routine_id = int(request.path_params["routine_id"])
    await _owned_routine(routine_id, user_id)
    await db.delete_routine(routine_id)
    return JSONResponse({"deleted": True})


# ---------- упражнения дня ----------

async def add_routine_exercise(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    routine_id = int(request.path_params["routine_id"])
    await _owned_routine(routine_id, user_id)
    body = await _json_body(request)
    exercise_id = _require(body, "exercise_id", int)
    await _owned_exercise(exercise_id, user_id)
    target = _optional_str(body, "target")
    if target is not None:
        target = formatting.normalize_routine_target(target)
    await db.append_routine_exercise(routine_id, exercise_id, target)
    exercises = await db.list_routine_exercises(routine_id)
    # Только что добавленное — последнее по order_index, ровно как отдаёт append.
    return JSONResponse(_routine_exercise_json(exercises[-1]), status_code=201)


async def update_routine_exercise(request: Request) -> JSONResponse:
    """target и progression правятся раздельными вызовами db (см.
    set_routine_exercise_target): ручная правка схемы стирает старое правило
    прогрессии, поэтому нельзя просто задать оба поля одним UPDATE."""
    user_id = await _authed_user_id(request)
    item_id = int(request.path_params["item_id"])
    await _owned_routine_exercise(item_id, user_id)
    body = await _json_body(request)
    if "target" in body:
        target = _optional_str(body, "target")
        if target is not None:
            target = formatting.normalize_routine_target(target)
        await db.set_routine_exercise_target(item_id, target)
    if "progression" in body:
        progression = body["progression"]
        if progression is not None and not isinstance(progression, dict):
            raise ApiError(400, "bad_request", "progression must be an object or null")
        await db.set_routine_exercise_progression(
            item_id, json.dumps(progression) if progression is not None else None
        )
    entry = await _owned_routine_exercise(item_id, user_id)
    return JSONResponse(await _routine_exercise_json_full(entry))


async def delete_routine_exercise(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    item_id = int(request.path_params["item_id"])
    await _owned_routine_exercise(item_id, user_id)
    await db.remove_routine_exercise(item_id)
    return JSONResponse({"deleted": True})


routes = [
    Route("/programs", list_programs, methods=["GET"]),
    Route("/programs", create_program, methods=["POST"]),
    Route("/programs/{program_id:int}", get_program, methods=["GET"]),
    Route("/programs/{program_id:int}", update_program, methods=["PATCH"]),
    Route("/programs/{program_id:int}", delete_program, methods=["DELETE"]),
    Route("/programs/{program_id:int}/next-day", program_next_day, methods=["GET"]),
    Route("/programs/{program_id:int}/days", add_program_day, methods=["POST"]),
    Route("/routines", list_routines, methods=["GET"]),
    Route("/routines", create_routine, methods=["POST"]),
    Route("/routines/{routine_id:int}", get_routine, methods=["GET"]),
    Route("/routines/{routine_id:int}", update_routine, methods=["PATCH"]),
    Route("/routines/{routine_id:int}", delete_routine, methods=["DELETE"]),
    Route("/routines/{routine_id:int}/exercises", add_routine_exercise, methods=["POST"]),
    Route("/routine-exercises/{item_id:int}", update_routine_exercise, methods=["PATCH"]),
    Route("/routine-exercises/{item_id:int}", delete_routine_exercise, methods=["DELETE"]),
]
