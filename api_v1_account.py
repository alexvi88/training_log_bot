"""REST `/v1` для настроек аккаунта (в боте — handlers/settings.py) и для
правки/повтора уже записанной тренировки (handlers/edit_workout.py,
handlers/history.py, «Повторить тренировку» в handlers/workout.py).

Тот же приём, что у соседних доменных модулей: транспорт поверх db.py, не
вторая реализация логики. Где у бота есть побочный эффект вокруг простого
UPDATE (пересчёт весов при смене единиц, ресинк значков), эндпоинт вызывает
те же db-/achievement_sync-функции в том же порядке, а не только меняет
колонку — иначе история тренировок и значки в приложении и в боте разойдутся.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import achievement_sync
import api_v1_common as common
import config
import db
import i18n
from workout_edit_data import on_workout_edited

ApiError = common.ApiError
_authed_user_id = common.authed_user_id
_json_body = common.json_body

# Допустимые значения — те же множества, что проверяет бот на своих экранах
# (keyboards.UNIT_NAMES/FORMULA_NAMES, i18n.SUPPORTED). Захардкожены здесь же,
# а не переиспользованы из keyboards, чтобы не тащить в REST-слой aiogram-only
# модуль ради двух ключей словаря.
_UNITS = ("kg", "lb")
_FORMULAS = ("epley", "brzycki")
# Тот же диапазон, что у keyboards.timezone_picker_keyboard: UTC-11 … UTC+14 —
# весь обитаемый диапазон офсетов, не более широкий и не более узкий.
_TZ_MIN, _TZ_MAX = -11, 14

# Булевы тумблеры экрана настроек — имя в JSON совпадает с колонкой в users,
# так что и сериализация, и валидация PATCH идут одним циклом по этому списку.
_BOOL_FIELDS = (
    "pushes_enabled",
    "ai_comments_enabled",
    "progression_hint_enabled",
    "food_macros_enabled",
    "show_extra_stats",
)

# Двойной тап по «Переключить на lb» в приложении — тот же риск, что и
# settings._converting у бота: рескейл всей истории подходов занимает время
# дольше одного await, и без захвата второй запрос успел бы стартовать, пока
# первый ещё читает старую единицу, и пересчитать веса дважды.
_converting: set[int] = set()


def _try_claim_converting(user_id: int) -> bool:
    if user_id in _converting:
        return False
    _converting.add(user_id)
    return True


def _settings_json(user) -> dict[str, Any]:
    return {
        "unit": user["unit"],
        "lang": user["lang"],
        "tz_offset": user["tz_offset"],
        "e1rm_formula": user["e1rm_formula"],
        **{field: bool(user[field]) for field in _BOOL_FIELDS},
    }


async def get_settings(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    user = await db.get_user(user_id)
    if user is None:
        raise ApiError(404, "not_found", "user not found")
    return JSONResponse(_settings_json(user))


async def _apply_unit_change(user_id: int, new_unit: str) -> None:
    """Тот же порядок шагов, что handlers.settings.settings_unit: конвертнуть
    веса подходов, взвешивания и шаги прогрессии, только потом переключить
    саму колонку — и пересчитать значки, потому что пороги в кг, а веса только
    что переехали в другую единицу."""
    factor = config.LB_PER_KG if new_unit == "lb" else 1 / config.LB_PER_KG
    await db.scale_user_set_weights(user_id, factor)
    await db.scale_bodyweight_logs(user_id, factor)
    await db.scale_progression_steps(user_id, factor)
    await db.update_user(user_id, unit=new_unit)
    await achievement_sync.resync(user_id)


async def update_settings(request: Request) -> JSONResponse:
    """PATCH — частичное обновление: только присланные поля меняются,
    остальные остаются как были. Невалидное значение любого поля — 400 и ни
    одна колонка не трогается (проверка идёт до первого db.update_user)."""
    user_id = await _authed_user_id(request)
    user = await db.get_user(user_id)
    if user is None:
        raise ApiError(404, "not_found", "user not found")
    body = await _json_body(request)

    new_unit: Optional[str] = None
    if "unit" in body:
        new_unit = body["unit"]
        if new_unit not in _UNITS:
            raise ApiError(400, "bad_request", f"unit must be one of {_UNITS}")

    new_lang: Optional[str] = None
    if "lang" in body:
        new_lang = body["lang"]
        if new_lang not in i18n.SUPPORTED:
            raise ApiError(400, "bad_request", f"lang must be one of {i18n.SUPPORTED}")

    new_tz: Optional[int] = None
    if "tz_offset" in body:
        raw_tz = body["tz_offset"]
        if not isinstance(raw_tz, int) or isinstance(raw_tz, bool):
            raise ApiError(400, "bad_request", "tz_offset must be int")
        if not _TZ_MIN <= raw_tz <= _TZ_MAX:
            raise ApiError(400, "bad_request", f"tz_offset must be between {_TZ_MIN} and {_TZ_MAX}")
        new_tz = raw_tz

    new_formula: Optional[str] = None
    if "e1rm_formula" in body:
        new_formula = body["e1rm_formula"]
        if new_formula not in _FORMULAS:
            raise ApiError(400, "bad_request", f"e1rm_formula must be one of {_FORMULAS}")

    bool_updates: dict[str, int] = {}
    for field in _BOOL_FIELDS:
        if field in body:
            value = body[field]
            if not isinstance(value, bool):
                raise ApiError(400, "bad_request", f"{field} must be bool")
            bool_updates[field] = 1 if value else 0

    # Всё провалидировано — теперь можно писать. Единицы — отдельным путём
    # (рескейл + ресинк), остальное — одним update_user.
    if new_unit is not None and new_unit != user["unit"]:
        if not _try_claim_converting(user_id):
            raise ApiError(409, "unit_conversion_in_progress", "a unit switch is already running")
        try:
            await _apply_unit_change(user_id, new_unit)
        finally:
            _converting.discard(user_id)

    if new_lang is not None:
        await db.set_user_lang(user_id, new_lang)

    plain_updates: dict[str, Any] = dict(bool_updates)
    if new_tz is not None:
        plain_updates["tz_offset"] = new_tz
    if new_formula is not None:
        plain_updates["e1rm_formula"] = new_formula
    if plain_updates:
        await db.update_user(user_id, **plain_updates)
    if new_tz is not None and new_tz != user["tz_offset"]:
        # Дни считаются по местному времени (db.list_finished_workout_dates) —
        # сдвиг пояса может подвинуть тренировку в соседние сутки и поменять
        # набор стрик-значков, ровно как в handlers.settings.settings_timezone_set.
        await db.mark_tz_set_by_user(user_id)
        await achievement_sync.resync(user_id)

    user = await db.get_user(user_id)
    return JSONResponse(_settings_json(user))


# ---------- правка и повтор тренировки ----------


async def _owned_workout(workout_id: int, user_id: int):
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != user_id:
        raise ApiError(404, "not_found", "workout not found")
    return workout


def _set_json(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "exercise_id": row["exercise_id"],
        "weight": row["weight"],
        "reps": row["reps"],
        "rpe": row["rpe"],
        "load_weight": row["load_weight"] if row["load_weight"] is not None else row["weight"],
    }


def _workout_json(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "note": row["note"],
    }


async def _owned_set_in_workout(workout_id: int, set_id: int, user_id: int):
    """Владение проверяется в две стороны: подход должен быть и пользователя,
    и именно этой тренировки — иначе по id из другой своей же тренировки
    (не то что чужой) можно было бы поправить подход не там, где ожидает
    клиент."""
    set_row = await db.get_set(set_id)
    if set_row is None or await db.get_set_owner(set_id) != user_id:
        raise ApiError(404, "not_found", "set not found")
    block = await db.get_block(set_row["block_id"])
    if block is None or block["workout_id"] != workout_id:
        raise ApiError(404, "not_found", "set not found in this workout")
    return set_row


async def update_workout_set(request: Request) -> JSONResponse:
    """Поправить вес/повторы/RPE уже записанного подхода — «✏️ Изменить» на
    экране правки тренировки в боте (handlers.edit_workout.editw_editset_entered)."""
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    set_id = int(request.path_params["set_id"])
    await _owned_workout(workout_id, user_id)
    set_row = await _owned_set_in_workout(workout_id, set_id, user_id)
    body = await _json_body(request)
    if not body:
        raise ApiError(400, "bad_request", "at least one of weight, reps, rpe is required")

    weight = set_row["weight"]
    if "weight" in body:
        weight = body["weight"]
        if not isinstance(weight, (int, float)) or isinstance(weight, bool):
            raise ApiError(400, "bad_request", "weight must be a number")
        weight = float(weight)

    reps = set_row["reps"]
    if "reps" in body:
        reps = body["reps"]
        if not isinstance(reps, int) or isinstance(reps, bool) or reps <= 0:
            raise ApiError(400, "bad_request", "reps must be a positive int")

    rpe = set_row["rpe"]
    if "rpe" in body:
        raw_rpe = body["rpe"]
        if raw_rpe is None:
            rpe = None
        else:
            if not isinstance(raw_rpe, (int, float)) or isinstance(raw_rpe, bool):
                raise ApiError(400, "bad_request", "rpe must be a number or null")
            rpe = float(raw_rpe)

    await db.update_set(set_id, weight, reps, rpe)
    # Тот же хвост, что у handlers.edit_workout._on_workout_edited: закешированный
    # AI-комментарий описывает числа, которых уже нет, а значки (например,
    # весовой клуб) могли зависеть именно от этого подхода.
    await on_workout_edited(workout_id)
    updated = await db.get_set(set_id)
    return JSONResponse(_set_json(updated))


async def delete_workout_set(request: Request) -> JSONResponse:
    """Удалить один подход — «🗑 Удалить» там же."""
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    set_id = int(request.path_params["set_id"])
    await _owned_workout(workout_id, user_id)
    await _owned_set_in_workout(workout_id, set_id, user_id)
    await db.delete_set(set_id)
    await on_workout_edited(workout_id)
    return JSONResponse({"deleted": True})


async def _find_block_for_exercise(workout_id: int, exercise_id: int) -> Optional[int]:
    for block in await db.list_blocks_for_workout(workout_id):
        for be in await db.get_block_exercises(block["id"]):
            if be["exercise_id"] == exercise_id:
                return block["id"]
    return None


def _require_finished(workout) -> None:
    """Эти три ручки — ровно то, что в боте живёт под «✏️ Правка» уже
    завершённой тренировки (handlers.edit_workout). Для активной/заносимой
    задним числом тренировки есть свой путь записи — POST /workouts/{id}/sets
    (api_v1.log_set) — со своей семантикой (например, автосоздание блока без
    подтверждения). Держать их разделёнными, а не одной веткой на все статусы,
    — чтобы не плодить неочевидные условные ветки в двух разных местах ради
    одного и того же эндпоинта."""
    if workout["status"] != "finished":
        raise ApiError(409, "workout_active", "only a finished workout can be edited this way")


async def add_workout_set(request: Request) -> JSONResponse:
    """Добавить подход в уже завершённую тренировку — «➕ Добавить подход» /
    «➕ Новое упражнение» на экране правки в боте (handlers.edit_workout:
    editw_addset_prompt → editw_addset_entered, editw_new_exercise_start →
    ..._editwex_finish). Один эндпоинт закрывает оба случая бота: если у
    упражнения в этой тренировке ещё нет блока, он заводится тут же, тем же
    способом, каким его завёл бы первый подход нового упражнения.

    Основной сценарий, ради которого это вообще пишется: нажал «Завершить»,
    вспомнил про забытый подход (или целое упражнение) — раньше это можно было
    поправить только в боте, приложение такого не умело."""
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    exercise_id = int(request.path_params["exercise_id"])
    workout = await _owned_workout(workout_id, user_id)
    _require_finished(workout)
    exercise = await db.get_exercise(exercise_id)
    if exercise is None or exercise["user_id"] != user_id:
        raise ApiError(404, "not_found", "exercise not found")

    body = await _json_body(request)
    if "weight" not in body:
        raise ApiError(400, "bad_request", "missing field: weight")
    weight = body["weight"]
    if not isinstance(weight, (int, float)) or isinstance(weight, bool):
        raise ApiError(400, "bad_request", "weight must be a number")
    weight = float(weight)
    reps = common.require(body, "reps", int)
    if reps <= 0:
        raise ApiError(400, "bad_request", "reps must be a positive int")
    rpe = body.get("rpe")
    if rpe is not None:
        if not isinstance(rpe, (int, float)) or isinstance(rpe, bool):
            raise ApiError(400, "bad_request", "rpe must be a number or null")
        rpe = float(rpe)

    block_id = await _find_block_for_exercise(workout_id, exercise_id)
    if block_id is None:
        # Новое для этой тренировки упражнение — блок заводится только сейчас,
        # на первый настоящий подход, тем же порядком, что и
        # editw_addset_entered: отменённый ввод не оставляет за собой пустого
        # упражнения.
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, exercise_id, 0)
        await db.touch_exercise_last_used(exercise_id)
        order_in_round = 0
    else:
        block_exs = await db.get_block_exercises(block_id)
        order_in_round = next(
            (be["order_in_block"] for be in block_exs if be["exercise_id"] == exercise_id), 0
        )

    set_id = await db.append_set(block_id, exercise_id, order_in_round, weight, reps, rpe)
    await on_workout_edited(workout_id)
    created = await db.get_set(set_id)
    return JSONResponse(_set_json(created), status_code=201)


async def remove_workout_exercise(request: Request) -> JSONResponse:
    """Убрать упражнение из уже завершённой тренировки целиком, вместе со всеми
    его подходами — «🗑 Удалить упражнение» → подтверждение на экране правки в
    боте (handlers.edit_workout.editw_remove_exercise). Подтверждение — дело
    клиентского UI (это необратимо и может унести не один подход), сам эндпоинт
    его не переспрашивает."""
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    exercise_id = int(request.path_params["exercise_id"])
    workout = await _owned_workout(workout_id, user_id)
    _require_finished(workout)
    block_id = await _find_block_for_exercise(workout_id, exercise_id)
    if block_id is None:
        raise ApiError(404, "not_found", "exercise not found in this workout")
    await db.delete_block_and_sets(block_id)
    await on_workout_edited(workout_id)
    return JSONResponse({"deleted": True})


async def delete_workout(request: Request) -> JSONResponse:
    """Удалить законченную тренировку целиком — экран истории в боте не
    предлагает этого для активной: та снимается отдельной ручкой
    (DELETE /workouts/active, см. api_v1.discard_active_workout), у неё ещё
    нет истории, которую было бы жалко терять."""
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    workout = await _owned_workout(workout_id, user_id)
    if workout["status"] != "finished":
        raise ApiError(409, "workout_active", "only a finished workout can be deleted this way")
    await db.discard_workout(workout_id)
    # Значки (стрики, тоннаж) считаются по всей оставшейся истории —
    # пересчитываем уже после удаления; achievement_sync.resync принимает
    # user_id напрямую, а не саму тренировку.
    await achievement_sync.resync(user_id)
    return JSONResponse({"deleted": True})


async def update_workout_date(request: Request) -> JSONResponse:
    """Перенести уже записанную тренировку на другой день — «📅 Дата» на экране
    правки в боте (handlers.edit_workout).

    Нужно чаще, чем кажется: тренировку заносят вечером следующего дня и
    получают её в истории не тем числом. Без переноса единственный выход —
    снести и записать заново.

    После переноса значки пересчитываются целиком (`resync`, а не
    `evaluate_after_finish`): сдвиг даты работает в обе стороны — может и
    достроить серию, и разорвать уже засчитанную, — а начисляющий путь умеет
    только добавлять.
    """
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    workout = await _owned_workout(workout_id, user_id)
    body = await _json_body(request)
    raw = body.get("date")
    if not isinstance(raw, str):
        raise ApiError(400, "bad_request", "date must be a string YYYY-MM-DD")
    try:
        date = dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise ApiError(400, "bad_request", "date must be YYYY-MM-DD") from exc

    # Время суток и длительность сохраняются: переносится день, а не «когда
    # именно тренировался». Иначе правка даты тихо стирала бы утреннюю
    # тренировку в полдень и ломала значки за ранний подъём.
    started = dt.datetime.fromisoformat(workout["started_at"])
    new_started = started.replace(year=date.year, month=date.month, day=date.day)
    new_finished = None
    if workout["finished_at"]:
        finished = dt.datetime.fromisoformat(workout["finished_at"])
        new_finished = (finished + (new_started - started)).isoformat()

    await db.update_workout_date(workout_id, new_started.isoformat(), new_finished)
    await achievement_sync.resync(user_id)
    workout = await db.get_workout(workout_id)
    return JSONResponse(
        {
            "id": workout["id"],
            "status": workout["status"],
            "started_at": workout["started_at"],
            "finished_at": workout["finished_at"],
            "note": workout["note"],
        }
    )


async def repeat_workout(request: Request) -> JSONResponse:
    """Начать новую тренировку по составу этой — «🔁 Повторить тренировку» в
    боте (handlers.workout.pick_repeat_use), только без промежуточного показа
    подтверждения: клиент уже показал состав через GET /workouts/{id}.

    Переносится только состав (какие упражнения и в каком порядке), не сами
    подходы — так же, как _load_next_planned_block заполняет активную
    тренировку блоками из db.workout_plan, без единого числа веса/повторов.
    """
    user_id = await _authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    await _owned_workout(workout_id, user_id)

    plan = await db.workout_plan(workout_id)
    if not plan:
        raise ApiError(400, "no_exercises", "workout has no exercises to repeat")

    # У пользователя может быть только одна активная тренировка — проверяем
    # до создания, чтобы не заводить блоки в тренировку, которую тут же откатим.
    if await db.get_active_workout(user_id) is not None:
        raise ApiError(409, "active_workout_exists", "finish or discard the active workout first")

    new_workout_id, created = await db.get_or_create_active_workout(user_id)
    if not created:
        # Гонка: другой запрос успел завести активную тренировку между
        # проверкой выше и этим вызовом — не трогаем её и отвечаем тем же
        # кодом конфликта.
        raise ApiError(409, "active_workout_exists", "finish or discard the active workout first")

    for entry in plan:
        for exercise_id in entry["exercise_ids"]:
            block_id = await db.create_block(new_workout_id, "single")
            await db.add_block_exercise(block_id, exercise_id, 0)
            await db.touch_exercise_last_used(exercise_id)

    new_workout = await db.get_workout(new_workout_id)
    return JSONResponse(_workout_json(new_workout), status_code=201)


routes = [
    Route("/settings", get_settings, methods=["GET"]),
    Route("/settings", update_settings, methods=["PATCH"]),
    Route("/workouts/{workout_id:int}/sets/{set_id:int}", update_workout_set, methods=["PATCH"]),
    Route("/workouts/{workout_id:int}/sets/{set_id:int}", delete_workout_set, methods=["DELETE"]),
    Route(
        "/workouts/{workout_id:int}/exercises/{exercise_id:int}/sets",
        add_workout_set, methods=["POST"],
    ),
    Route(
        "/workouts/{workout_id:int}/exercises/{exercise_id:int}",
        remove_workout_exercise, methods=["DELETE"],
    ),
    Route("/workouts/{workout_id:int}", delete_workout, methods=["DELETE"]),
    Route("/workouts/{workout_id:int}/date", update_workout_date, methods=["PATCH"]),
    Route("/workouts/{workout_id:int}/repeat", repeat_workout, methods=["POST"]),
]
