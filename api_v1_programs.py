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
import math
import sqlite3
from typing import Any

from starlette.requests import Request
from starlette.routing import Route

import ai_trainer
import analytics
import api_v1_common as common
import config
import db
import formatting
import i18n
import seed_data
from api_v1_common import JSONResponse

ApiError = common.ApiError
_PROGRESSION_RULES = ai_trainer.PROGRESSION_RULES
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
    name = formatting.strip_control_chars(raw).strip()
    if not name:
        raise ApiError(400, "bad_request", "name must not be empty", key="api.error.name_empty")
    if len(name) > config.MAX_PROGRAM_NAME_LENGTH:
        raise ApiError(
            400, "name_too_long", f"name must be at most {config.MAX_PROGRAM_NAME_LENGTH} chars",
            key="api.error.name_too_long", max=config.MAX_PROGRAM_NAME_LENGTH,
        )
    return name


async def _check_routine_budget(user_id: int, adding: int) -> None:
    """403, а не тихий обход: тот же db.routine_budget, что у бота, и без
    исключения на этот транспорт — иначе через приложение можно завести
    больше дней, чем разрешает бот."""
    over_budget = await db.routine_budget(user_id, adding)
    if over_budget:
        raise ApiError(403, "routine_limit_reached", "routine budget exceeded", human=over_budget)


async def _owned_workout(workout_id: int, user_id: int):
    """Тот же паттерн владения, что и у остальных id из URL (см. докстринг
    модуля) — своя копия, а не импорт api_v1_account._owned_workout, чтобы не
    заводить связь между доменными модулями ради одной проверки в четыре строки."""
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != user_id:
        raise ApiError(404, "not_found", "workout not found")
    return workout


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
        # Разгрузка каждые N недель (4–6) или null — у программы её нет.
        "deload_every_weeks": program["deload_every_weeks"],
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


# Правило прогрессии — плоский объект из пары скаляров (rule, step, reps_top…).
# Потолки: не больше 16 полей и 1 КБ в JSON, строки до 64 символов.
_PROGRESSION_MAX_KEYS = 16
_PROGRESSION_MAX_JSON_BYTES = 1024
_PROGRESSION_MAX_STR = 64


def _progression_is_sane(rule: Any) -> bool:
    """Плоский объект, где нет NaN/Infinity, вложенных структур и раздутых
    строк. Одно место и для записи (400), и для чтения уже записанного
    (молча игнорируем): NaN, проскочивший раньше, ронял JSONResponse 500-й."""
    if not isinstance(rule, dict) or len(rule) > _PROGRESSION_MAX_KEYS:
        return False
    for key, value in rule.items():
        if not isinstance(key, str) or len(key) > _PROGRESSION_MAX_STR:
            return False
        if isinstance(value, float):
            if not math.isfinite(value):
                return False
        elif isinstance(value, str):
            if len(value) > _PROGRESSION_MAX_STR:
                return False
        elif value is not None and not isinstance(value, (bool, int)):
            return False
    return True


def _stored_progression_json(raw: Any) -> dict[str, Any] | None:
    """routine_exercises.progression для ответа: битое, не-объект или с NaN —
    как «правила нет», а не 500."""
    if not raw:
        return None
    try:
        rule = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return rule if _progression_is_sane(rule) else None


def _validated_progression(progression: dict[str, Any], unit: str) -> dict[str, Any]:
    """Схема правила при записи: размер и скаляры (_progression_is_sane),
    `step` — конечное число > 0 и не больше потолка шага в единицах атлета
    (analytics.progression_max_step), `rule` — из ai_trainer-списка правил."""
    bad = ApiError(
        400, "bad_request", "progression is malformed", key="api.error.progression_invalid"
    )
    if not _progression_is_sane(progression):
        raise bad
    if len(json.dumps(progression, ensure_ascii=False).encode("utf-8")) > _PROGRESSION_MAX_JSON_BYTES:
        raise bad
    step = progression.get("step")  # null — «шага нет», как у iOS без поля
    if step is not None and (
        isinstance(step, bool) or not isinstance(step, (int, float))
        or step <= 0 or step > analytics.progression_max_step(unit)
    ):
        raise bad
    rule = progression.get("rule")
    if rule is not None and (not isinstance(rule, str) or rule not in _PROGRESSION_RULES):
        raise bad
    return progression


async def _restore_exact_step(item_id: int, progression: dict[str, Any], unit: str) -> dict[str, Any]:
    """Шаг мы отдаём округлённым до 2 знаков, а клиент шлёт его обратно (в том
    числе копией дня в новый пункт). Если у пункта уже есть шаг и пришло его
    округление — остаётся хранимый; если сравнить не с чем — см.
    common.restore_converted_weight."""
    step = progression.get("step")
    if step is None or isinstance(step, bool) or progression.get("step_unit") == "sec":
        return progression
    stored = _stored_progression_json((await db.get_routine_exercise(item_id))["progression"])
    stored_step = (stored or {}).get("step")
    if isinstance(stored_step, (int, float)) and not isinstance(stored_step, bool):
        exact = common.keep_exact(step, stored_step)
    else:
        exact = common.restore_converted_weight(step, unit)
    return {**progression, "step": exact}


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
        "progression": _stored_progression_json(progression),
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
    """Имя, описание и разгрузка правятся независимо (см. db.set_program_description):
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
    if "deload_every_weeks" in body:
        weeks = body["deload_every_weeks"]
        if weeks is not None and (isinstance(weeks, bool) or not isinstance(weeks, int)):
            raise ApiError(400, "bad_request", "deload_every_weeks must be an integer or null")
        # Зажим в 4–6 — db.clean_deload_every_weeks; 0 и null снимают разгрузку.
        await db.set_program_deload(program_id, weeks)
    program = await _owned_program(program_id, user_id)
    days = await db.list_program_days_by_id(program_id)
    return JSONResponse(_program_detail_json(program, days))


async def delete_program(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    program_id = int(request.path_params["program_id"])
    await _owned_program(program_id, user_id)
    await db.delete_program_by_id(program_id)
    return JSONResponse({"deleted": True})


async def merge_program(request: Request) -> JSONResponse:
    """«Слить с существующей программой» — тот же db.merge_programs, который
    в боте (handlers/routines.py rt_program_merge) вызывается только как
    ответ на коллизию имён при переименовании программы.

    В API у PATCH .../programs/{id} с именем коллизии нет — она уже 409
    name_taken (update_program), без предложения слить. Слияние здесь —
    отдельное явное действие, а не побочный эффект переименования: клиент,
    решивший смерджить программы, знает id обеих заранее (например, из
    /programs), а не угадывает его по тексту предложения, как бот.

    Программа из URL растворяется в `into_id` — её дни переезжают туда (с
    переименованием при совпадении имён, см. db.merge_programs) и сама она
    удаляется; в ответе — уже объединённая программа-получатель."""
    user_id = await _authed_user_id(request)
    program_id = int(request.path_params["program_id"])
    await _owned_program(program_id, user_id)
    body = await _json_body(request)
    into_id = _require(body, "into_id", int)
    if into_id == program_id:
        raise ApiError(400, "bad_request", "cannot merge a program with itself")
    await _owned_program(into_id, user_id)
    await db.merge_programs(user_id, program_id, into_id)
    target = await _owned_program(into_id, user_id)
    days = await db.list_program_days_by_id(into_id)
    return JSONResponse(_program_detail_json(target, days))


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
    """`program_id: null` — «📤 Вынести из программы» (rt:dayout в боте):
    день становится самостоятельной рутиной. Тот же db.move_routine_to_program,
    которым бот это делает; сюда, а не отдельной ручкой, потому что это
    правка одного поля того же routine, как и name.

    Ставить программу через это поле, а не только снимать, API не даёт —
    у бота такого действия нет (день попадает в программу только через
    add_program_day/create_routine_from_workout), и без него не нужны ни
    проверка бюджета дней, ни выбор day_order, которые тогда пришлось бы
    сюда тащить."""
    user_id = await _authed_user_id(request)
    routine_id = int(request.path_params["routine_id"])
    routine = await _owned_routine(routine_id, user_id)
    body = await _json_body(request)
    name = _optional_str(body, "name")
    if name is not None:
        await db.rename_routine(routine_id, _clean_name(name))
    former: dict[str, Any] = {}
    if "program_id" in body:
        if body["program_id"] is not None:
            raise ApiError(400, "bad_request", "program_id can only be set to null")
        if routine["program_id"] is None:
            raise ApiError(400, "already_standalone", "day is already standalone")
        former = {
            "former_program_id": routine["program_id"],
            "former_position": routine["day_order"],
        }
        await db.move_routine_to_program(routine_id, None)
    routine = await _owned_routine(routine_id, user_id)
    data = await _routine_detail_json(routine)
    # Откуда вынесли — для «Отменить» в приложении (POST /routines/{id}/attach).
    data.update(former)  # пусто, если это было просто переименование
    return JSONResponse(data)


async def attach_routine(request: Request) -> JSONResponse:
    """Обратное к «Вынести из программы»: вернуть самостоятельный день в
    программу на прежнее место. `position` — day_order, который вернул вынос
    (`former_position`); без него день встаёт последним. Ставит день только
    в свою программу и только самостоятельный — это не способ перекинуть день
    из одной программы в другую."""
    user_id = await _authed_user_id(request)
    routine_id = int(request.path_params["routine_id"])
    routine = await _owned_routine(routine_id, user_id)
    body = await _json_body(request)
    program_id = _require(body, "program_id", int)
    position = common.optional_int(body, "position")
    if position is not None and position < 0:
        raise ApiError(400, "bad_request", "position must be >= 0")
    await _owned_program(program_id, user_id)
    if routine["program_id"] is not None:
        raise ApiError(400, "already_in_program", "day is already part of a program")
    await db.attach_routine_to_program(routine_id, program_id, position)
    routine = await _owned_routine(routine_id, user_id)
    return JSONResponse(await _routine_detail_json(routine))


async def delete_routine(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    routine_id = int(request.path_params["routine_id"])
    await _owned_routine(routine_id, user_id)
    await db.delete_routine(routine_id)
    return JSONResponse({"deleted": True})


def _reorder_direction(body: dict[str, Any]) -> str:
    """"up"/"down" — то же значение, которым бот шлёт rt:daymv/rt:mvex
    (см. handlers/routines.py). Третьего значения нет ни там, ни здесь."""
    direction = _require(body, "direction", str)
    if direction not in ("up", "down"):
        raise ApiError(400, "bad_request", "direction must be 'up' or 'down'")
    return direction


async def reorder_program_day(request: Request) -> JSONResponse:
    """«🔀 Порядок дней» (rt:daymv) — переставить день программы относительно
    соседа. Сама перестановка и защита от гонки двух параллельных «вверх» —
    в db.reorder_program_day (общий _write_lock), здесь только владение и
    разбор direction."""
    user_id = await _authed_user_id(request)
    routine_id = int(request.path_params["routine_id"])
    routine = await _owned_routine(routine_id, user_id)
    if routine["program_id"] is None:
        # Одиночный день переставлять не относительно чего — как и в боте,
        # где rt:daymv доступен только внутри программы.
        raise ApiError(400, "not_in_program", "day is not part of a program")
    body = await _json_body(request)
    direction = _reorder_direction(body)
    await db.reorder_program_day(routine_id, direction)
    days = await db.list_program_days_by_id(routine["program_id"])
    return JSONResponse({"days": [_routine_list_json(d) for d in days]})


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
    # Упражнение уже в этом дне — отдаём существующий пункт (200), а не 500:
    # вставка упиралась в уникальный индекс idx_routine_exercises_unique и
    # падала internal_error. Не 409 — копирование дня в приложении
    # (AddProgramDayView) добавляет упражнения по одному в цикле, и ошибка на
    # повторе (ретрай после оборванного ответа, дубль в исходном дне) обрывала
    # бы копию на середине. Бот в том же случае тоже ничего не добавляет, а
    # показывает день как есть (handlers.routines._rtadd_finish).
    existing = await _routine_entry_for(routine_id, exercise_id)
    if existing is not None:
        return JSONResponse(_routine_exercise_json(existing), status_code=200)
    try:
        await db.append_routine_exercise(routine_id, exercise_id, target)
    except sqlite3.IntegrityError:
        # Параллельный запрос успел вставить то же самое между проверкой и
        # вставкой — итог тот же, что и у проверки выше.
        existing = await _routine_entry_for(routine_id, exercise_id)
        if existing is None:
            raise
        return JSONResponse(_routine_exercise_json(existing), status_code=200)
    exercises = await db.list_routine_exercises(routine_id)
    # Только что добавленное — последнее по order_index, ровно как отдаёт append.
    return JSONResponse(_routine_exercise_json(exercises[-1]), status_code=201)


async def _routine_entry_for(routine_id: int, exercise_id: int):
    for entry in await db.list_routine_exercises(routine_id):
        if entry["exercise_id"] == exercise_id:
            return entry
    return None


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
        if progression is not None:
            user = await db.get_user(user_id)
            progression = _validated_progression(progression, user["unit"])
            progression = await _restore_exact_step(item_id, progression, user["unit"])
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


async def reorder_routine_exercise(request: Request) -> JSONResponse:
    """rt:mvex — переставить упражнение дня относительно соседа. Как и у
    дней программы, сама перестановка и защита от гонки — в
    db.reorder_routine_exercise, здесь только владение и разбор тела."""
    user_id = await _authed_user_id(request)
    item_id = int(request.path_params["item_id"])
    entry = await _owned_routine_exercise(item_id, user_id)
    body = await _json_body(request)
    direction = _reorder_direction(body)
    await db.reorder_routine_exercise(item_id, direction)
    exercises = await db.list_routine_exercises(entry["routine_id"])
    return JSONResponse({"exercises": [_routine_exercise_json(e) for e in exercises]})


# ---------- готовые программы (каталог) ----------
#
# WORKOUT_PROGRAMS (seed_data.py) — тот же read-only каталог, что бот
# показывает под «✨ Готовые программы». Текст в каталоге живёт на русском и
# переводится на рендере (seed_data.localized_*), поэтому здесь, как и у
# api_v1_achievements, ответ собирается под i18n.use_lang(user["lang"]) — без
# этого англоязычный увидел бы русские названия программ.

def _catalog_program_json(program: dict, lang: str) -> dict[str, Any]:
    key = program["key"]
    return {
        "key": key,
        "name": seed_data.localized_program_name(key, lang),
        "meta": seed_data.localized_program_meta(key, lang),
        "description": seed_data.localized_program_description(key, lang),
        "days": [
            {
                "name": seed_data.localized_program_day_name(key, i, lang),
                "exercises": [
                    {
                        "name": seed_data.localized_exercise_name(ex, lang),
                        "target": seed_data.localized_target(target, lang),
                    }
                    for ex, target in exercises
                ],
            }
            for i, (_day_name, exercises) in enumerate(program["days"])
        ],
    }


async def list_program_catalog(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    user = await db.get_user(user_id)
    lang = user["lang"] if user else i18n.DEFAULT_LANG
    with i18n.use_lang(lang):
        payload = [_catalog_program_json(p, lang) for p in seed_data.WORKOUT_PROGRAMS]
    return JSONResponse(payload)


async def add_catalog_program(request: Request) -> JSONResponse:
    """«➕ Добавить себе» на каталожной программе (handlers/routines.py
    rt_program_add) — тот же seed_data.instantiate_program, чтобы не заводить
    вторую версию каталога. В отличие от бота (который на занятое имя
    показывает выбор «открыть/добавить копией»), API одним ответом отдаёт
    409 — как и обычный create_program: у клиента для этого уже есть экран
    конфликта имени, тот же самый, что при ручном создании программы."""
    user_id = await _authed_user_id(request)
    program = seed_data.PROGRAM_BY_KEY.get(request.path_params["key"])
    if program is None:
        raise ApiError(404, "not_found", "catalog program not found")
    user = await db.get_user(user_id)
    if user is None:
        raise ApiError(404, "not_found", "user not found")
    lang = user["lang"]
    body = await request.body()
    fields = await _json_body(request) if body else {}
    with i18n.use_lang(lang):
        name = _optional_str(fields, "name") or seed_data.localized_program_name(program["key"], lang)
        name = _clean_name(name)
        await _check_routine_budget(user_id, len(program["days"]))
        existing = await db.find_program_by_name(user_id, name)
        if existing is not None:
            raise ApiError(409, "name_taken", "a program with this name already exists")
        revived: list[int] = []
        program_id = await seed_data.instantiate_program(user_id, program["key"], name, revived)
    days = await db.list_program_days_by_id(program_id)
    result = await _owned_program(program_id, user_id)
    payload = _program_detail_json(result, days)
    # Что программа достала из архива, чтобы дни были целыми (см.
    # db.create_routine_from_program): приложение говорит об этом тостом с
    # «Убрать». Пустой список — ничего не доставали.
    payload["unarchived"] = []
    for ex_id in revived:
        ex = await db.get_exercise(ex_id)
        if ex is not None:
            payload["unarchived"].append({"exercise_id": ex_id, "name": ex["display_name"]})
    return JSONResponse(payload, status_code=201)


# ---------- программа/день из уже сделанной тренировки ----------
#
# handlers/routines.py «➕ Из тренировки» (rt_pickw_use → rt_name_entered):
# снимок состава прошлой тренировки (упражнения + фактически сделанные
# подходы как target) становится либо новым самостоятельным днём
# (program_id не передан — ровно как «Из тренировки» без открытой
# программы), либо днём уже существующей программы. Логика — тот же
# db.create_routine_from_workout, никакой второй реализации.

async def create_routine_from_workout(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    await _owned_workout(workout_id, user_id)
    body = await _json_body(request)
    name = _clean_name(str(_require(body, "name", str)))
    program_id = common.optional_int(body, "program_id")
    if program_id is not None:
        await _owned_program(program_id, user_id)
    await _check_routine_budget(user_id, 1)
    routine_id = await db.create_routine_from_workout(user_id, workout_id, name, program_id=program_id)
    routine = await db.get_routine(routine_id)
    return JSONResponse(await _routine_detail_json(routine), status_code=201)


routes = [
    Route("/programs", list_programs, methods=["GET"]),
    Route("/programs", create_program, methods=["POST"]),
    Route("/programs/catalog", list_program_catalog, methods=["GET"]),
    Route("/programs/catalog/{key}", add_catalog_program, methods=["POST"]),
    Route("/programs/{program_id:int}", get_program, methods=["GET"]),
    Route("/programs/{program_id:int}", update_program, methods=["PATCH"]),
    Route("/programs/{program_id:int}", delete_program, methods=["DELETE"]),
    Route("/programs/{program_id:int}/merge", merge_program, methods=["POST"]),
    Route("/programs/{program_id:int}/next-day", program_next_day, methods=["GET"]),
    Route("/programs/{program_id:int}/days", add_program_day, methods=["POST"]),
    Route("/routines", list_routines, methods=["GET"]),
    Route("/routines", create_routine, methods=["POST"]),
    Route("/routines/{routine_id:int}", get_routine, methods=["GET"]),
    Route("/routines/{routine_id:int}", update_routine, methods=["PATCH"]),
    Route("/routines/{routine_id:int}", delete_routine, methods=["DELETE"]),
    Route("/routines/{routine_id:int}/attach", attach_routine, methods=["POST"]),
    Route("/routines/{routine_id:int}/reorder", reorder_program_day, methods=["POST"]),
    Route("/routines/{routine_id:int}/exercises", add_routine_exercise, methods=["POST"]),
    Route("/routine-exercises/{item_id:int}", update_routine_exercise, methods=["PATCH"]),
    Route("/routine-exercises/{item_id:int}", delete_routine_exercise, methods=["DELETE"]),
    Route("/routine-exercises/{item_id:int}/reorder", reorder_routine_exercise, methods=["POST"]),
    Route("/workouts/{workout_id:int}/routines", create_routine_from_workout, methods=["POST"]),
]
