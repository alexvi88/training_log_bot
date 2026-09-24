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

import json
import logging
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import account_deletion
import achievement_sync
import api_v1_common as common
import config
import db
import i18n
from workout_edit_data import move_workout_to_date, on_workout_edited

logger = logging.getLogger(__name__)

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
_TZ_MIN, _TZ_MAX = common.TZ_OFFSET_MIN, common.TZ_OFFSET_MAX

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
#
# Захват общий с ботом (db.try_claim_unit_conversion), а не свой набор в этом
# модуле: тот же аккаунт может переключить единицы и здесь, и в боте почти
# одновременно, и два разных набора в памяти процесса не видят друг друга —
# каждый гасит только повтор на своей же поверхности. См. комментарий в db.py
# над scale_bodyweight_logs.
_try_claim_converting = db.try_claim_unit_conversion
_converting = db._unit_converting


def _settings_json(user) -> dict[str, Any]:
    return {
        "unit": user["unit"],
        "lang": user["lang"],
        "tz_offset": user["tz_offset"],
        # Выбрал ли человек пояс сам (пикер здесь или в боте). Пока нет —
        # приложение подтягивает пояс телефона (device_tz_offset_minutes в
        # PATCH), выбранный руками не трогает никогда.
        "tz_set_by_user": bool(user["tz_set_by_user"]),
        "e1rm_formula": user["e1rm_formula"],
        **{field: bool(user[field]) for field in _BOOL_FIELDS},
    }


async def get_settings(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    user = await db.get_user(user_id)
    if user is None:
        raise ApiError(404, "not_found", "user not found")
    return JSONResponse(_settings_json(user))


def _equipment_list(user) -> Optional[list[str]]:
    """users.equipment хранится строкой JSON (ai_trainer._save_athlete_profile
    пишет её через json.dumps) — тот же разбор, что и у handlers.settings.
    profile_rows, только результат отдаётся списком, а не готовой строкой
    через запятую: клиент сам решает, как рисовать список инвентаря."""
    raw = user["equipment"]
    if not raw:
        return None
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        # Могла приехать и старая запись не-списком — показать как одну
        # строку лучше, чем уронить эндпоинт.
        return [str(raw)]
    return [str(x) for x in items] if isinstance(items, list) else [str(items)]


def _profile_json(user) -> dict[str, Any]:
    """Профиль атлета, который AI-тренер копит через save_athlete_profile
    («🤖 Что тренер про тебя знает» в боте) — те же четыре поля и те же
    имена колонок users.*, что читает handlers.settings.profile_rows.
    `days_per_week` сюда не входит: колонка в базе осталась, но её больше
    никто не пишет и не читает (см. docstring profile_rows)."""
    return {
        "experience": user["experience"],
        "goal": user["goal"],
        "equipment": _equipment_list(user),
        "limitations": user["limitations"],
    }


async def get_athlete_profile(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    user = await db.get_user(user_id)
    if user is None:
        raise ApiError(404, "not_found", "user not found")
    return JSONResponse(_profile_json(user))


async def clear_athlete_profile(request: Request) -> JSONResponse:
    """«🗑 Очистить» на экране профиля — та же запись, что и у бота
    (handlers.settings.settings_profile_clear): все поля памяти разом,
    включая уже неиспользуемый days_per_week, чтобы не оставлять в базе
    осколок старого значения."""
    user_id = await _authed_user_id(request)
    await db.update_user(
        user_id,
        days_per_week=None, experience=None, goal=None, equipment=None, limitations=None,
    )
    user = await db.get_user(user_id)
    return JSONResponse(_profile_json(user))


async def delete_account(request: Request) -> JSONResponse:
    """Снести аккаунт целиком — требование Apple 5.1.1(v): путь «удалить
    аккаунт» обязан быть внутри приложения, а не в письме в поддержку.

    Сносится ВЕСЬ аккаунт, а не данные приложения: аккаунт у нас один на две
    поверхности, значит вместе с приложением исчезает и история в боте. Так и
    должно быть по требованию, но клиент обязан сказать это на экране
    подтверждения дословно — человек, который думает, что отвязывает
    приложение, иначе снесёт год тренировок.

    Подтверждение спрашивается ещё раз здесь, параметром `?confirm=delete`, и
    это не дубль экрана клиента: голый DELETE слишком легко получить случайно —
    повтор запроса из офлайн-очереди, чужой скрипт с утёкшим токеном, опечатка
    в пути. Операция необратимая и единственная такая во всём `/v1`, так что
    цена лишнего параметра — ноль, а цена его отсутствия — история, которую не
    вернуть. Параметром, а не телом: тело у DELETE по дороге теряют и прокси, и
    половина HTTP-клиентов, и «подтверждение пропало» превратилось бы в 400 на
    ровном месте.

    Ответ 200 отдаётся только когда в базе действительно ничего не осталось:
    `account_deletion.delete_account` возвращает уцелевшее, и непустой остаток
    — это 500, а не успех. Токен, которым пришёл запрос, к этому моменту уже
    снесён вместе с остальными строками аккаунта.
    """
    user_id = await _authed_user_id(request)
    if request.query_params.get("confirm") != "delete":
        raise ApiError(400, "bad_request", "expected ?confirm=delete")

    left = await account_deletion.delete_account(user_id)
    if left:
        # Снос идёт одной транзакцией, так что сюда можно попасть только если
        # часть данных лежит вне неё — знать об этом надо по логу, а не по
        # жалобе «удалил аккаунт, а история осталась».
        logger.error("DELETE /v1/account: %s not fully deleted, left: %s", user_id, left)
        raise ApiError(500, "internal_error", "account was not fully deleted")
    return JSONResponse({"deleted": True})


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

    # Пояс телефона, который приложение шлёт само, без участия человека, —
    # в отличие от tz_offset (пикер): применяется только пока пояс не выбран
    # руками (users.tz_set_by_user) и отметку «выбран» не ставит. Явный
    # tz_offset в том же теле важнее. Иначе аккаунт, заведённый с
    # config.DEFAULT_TZ_OFFSET (+3), так и жил бы по Москве, где бы ни был
    # телефон.
    device_tz: Optional[int] = None
    if "device_tz_offset_minutes" in body:
        device_tz = common.device_tz_offset_hours(body["device_tz_offset_minutes"])
        if device_tz is None:
            raise ApiError(
                400, "bad_request", "device_tz_offset_minutes must be int between -720 and 840"
            )
        if new_tz is not None or user["tz_set_by_user"]:
            device_tz = None

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
    elif device_tz is not None:
        plain_updates["tz_offset"] = device_tz
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
    elif device_tz is not None and device_tz != user["tz_offset"]:
        # Тот же пересчёт значков, что и у ручной смены пояса выше, но без
        # отметки «выбрал сам».
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

    # Границы — общие с живой записью и с разбором строки (см. api_v1_common).
    weight = set_row["weight"]
    if "weight" in body:
        weight = common.set_weight(body["weight"])

    reps = set_row["reps"]
    if "reps" in body:
        reps = common.set_reps(body["reps"])

    rpe = set_row["rpe"]
    if "rpe" in body:
        rpe = common.set_rpe(body["rpe"])

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
    weight = common.set_weight(body["weight"])
    reps = common.set_reps(common.require(body, "reps", int))
    rpe = common.set_rpe(body.get("rpe"))
    idempotency_key = common.optional_str(body, "idempotency_key")

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

    set_id = await db.append_set(
        block_id, exercise_id, order_in_round, weight, reps, rpe,
        user_id=user_id, idempotency_key=idempotency_key,
    )
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
    await _owned_workout(workout_id, user_id)
    body = await _json_body(request)
    raw = body.get("date")
    if not isinstance(raw, str):
        raise ApiError(400, "bad_request", "date must be a string YYYY-MM-DD")
    date = common.parse_date(raw)
    # Будущим днём тренировки не бывает — ровно та же проверка, что у
    # POST /workouts/backfill: перенести уже сделанное в завтра нельзя.
    await common.reject_future_date(date, user_id)

    # Время суток и длительность сохраняются: переносится день, а не «когда
    # именно тренировался». Иначе правка даты тихо стирала бы утреннюю
    # тренировку в полдень и ломала значки за ранний подъём. Сам перенос —
    # общая с ботом workout_edit_data.move_workout_to_date: кроме UPDATE он
    # двигает следом метки подходов (без этого карточка теряет длительность),
    # сбрасывает AI-комментарий и пересчитывает значки.
    await move_workout_to_date(workout_id, date)
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
    # Живёт рядом с настройками намеренно: в боте «Удалить аккаунт» — кнопка
    # блока «Данные» на экране настроек, и в приложении ей место там же.
    Route("/account", delete_account, methods=["DELETE"]),
    # Профиль, который AI-тренер копит сам (save_athlete_profile) — экран
    # «🤖 Что тренер про тебя знает» в боте. Пишет его только модель через
    # диалог, поэтому тут только чтение и очистка целиком, без PATCH полей.
    Route("/profile", get_athlete_profile, methods=["GET"]),
    Route("/profile", clear_athlete_profile, methods=["DELETE"]),
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
