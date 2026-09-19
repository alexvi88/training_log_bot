"""REST `/v1` для дневника еды (в боте — handlers/food_diary.py).

Транспорт поверх db.py, не вторая реализация логики: суммы по дню считаются
здесь так же, как их считает formatting.build_food_day_screen для бота, а
распознавание фото/текста уходит в ai_trainer.analyze_food — ту же функцию,
что дёргает бот.

`/food/parse` не сохраняет запись — только отдаёт догадку модели, как
карточка «Всё верно?» в боте до подтверждения. Сохранение — отдельный
POST /food с уже готовыми полями (человек мог их поправить в приложении).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import ai_limits
import ai_trainer
import api_v1_common as common
import config
import db
import timeutil

ApiError = common.ApiError


def _parse_date(raw: Optional[str], user) -> dt.date:
    """Дата параметром, а без неё — «сегодня» по таймзоне пользователя
    (timeutil.user_today), не по UTC сервера: иначе поздний ужин после
    полуночи UTC, но до полуночи по местному времени, попадал бы во вчера."""
    if not raw:
        return timeutil.user_today(user)
    try:
        return dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise ApiError(400, "bad_request", "date must be YYYY-MM-DD") from exc


def _entry_json(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "eaten_on": row["eaten_on"],
        "description": row["description"],
        "calories": row["calories"],
        "protein": row["protein"],
        "fat": row["fat"],
        "carbs": row["carbs"],
        "has_photo": bool(row["photo_file_id"]),
        "source": row["source"],
        "created_at": row["created_at"],
    }


def _sum(rows, key: str) -> Optional[float]:
    """SUM по возможно-NULL столбцу: пусто — None (нечего складывать), как и
    в db.list_food_days, а не 0 — 0 ккал за день без единой записи с калориями
    выглядел бы как «поел на ноль», а не «не считали»."""
    values = [r[key] for r in rows if r[key] is not None]
    return sum(values) if values else None


async def get_day(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    user = await db.get_user(user_id)
    date = _parse_date(request.query_params.get("date"), user)
    rows = await db.list_food_entries(user_id, date.isoformat())
    return JSONResponse(
        {
            "date": date.isoformat(),
            "entries": [_entry_json(r) for r in rows],
            "totals": {
                "calories": _sum(rows, "calories"),
                "protein": _sum(rows, "protein"),
                "fat": _sum(rows, "fat"),
                "carbs": _sum(rows, "carbs"),
            },
            "kcal_goal": user["kcal_goal"] if user else None,
        }
    )


async def add_entry(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    user = await db.get_user(user_id)
    body = await common.json_body(request)
    description = str(common.require(body, "name", str)).strip()
    if not description:
        raise ApiError(400, "bad_request", "name must not be empty")
    calories = body.get("kcal")
    if calories is not None and not isinstance(calories, (int, float)):
        raise ApiError(400, "bad_request", "kcal must be a number")
    protein = body.get("protein")
    fat = body.get("fat")
    carbs = body.get("carbs")
    for field_name, value in (("protein", protein), ("fat", fat), ("carbs", carbs)):
        if value is not None and not isinstance(value, (int, float)):
            raise ApiError(400, "bad_request", f"{field_name} must be a number")
    eaten_on_raw = common.optional_str(body, "eaten_on")
    eaten_on = _parse_date(eaten_on_raw, user)
    # Съесть что-то завтра нельзя — та же проверка, что у занесения
    # тренировки задним числом (POST /workouts/backfill).
    await common.reject_future_date(eaten_on, user_id, field="eaten_on")
    entry_id = await db.add_food_entry(
        user_id,
        eaten_on.isoformat(),
        description,
        calories=calories,
        protein=protein,
        fat=fat,
        carbs=carbs,
    )
    row = await db.get_food_entry(entry_id)
    return JSONResponse(_entry_json(row), status_code=201)


async def _owned_entry(entry_id: int, user_id: int):
    """get_food_entry отдаёт запись по id без проверки хозяина (id из URL мог
    быть чужим и просто угадан), так что владение проверяется здесь, на
    каждом пути к конкретной записи."""
    row = await db.get_food_entry(entry_id)
    if row is None or row["telegram_id"] != user_id:
        raise ApiError(404, "not_found", "food entry not found")
    return row


async def get_entry(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    entry_id = int(request.path_params["entry_id"])
    row = await _owned_entry(entry_id, user_id)
    return JSONResponse(_entry_json(row))


async def delete_entry(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    entry_id = int(request.path_params["entry_id"])
    await _owned_entry(entry_id, user_id)
    await db.delete_food_entry(entry_id)
    return JSONResponse({"deleted": True})


async def list_days(request: Request) -> JSONResponse:
    """Список дней с суммами — экран истории дневника еды."""
    user_id = await common.authed_user_id(request)
    limit = common.query_int(request, "limit", 8, minimum=1, maximum=100)
    offset = common.query_int(request, "offset", 0, minimum=0)
    rows = await db.list_food_days(user_id, limit=limit, offset=offset)
    total = await db.count_food_days(user_id)
    return JSONResponse(
        {
            "days": [
                {
                    "date": r["eaten_on"],
                    "entries": r["entries"],
                    "calories": r["calories"],
                    "protein": r["protein"],
                    "fat": r["fat"],
                    "carbs": r["carbs"],
                    "descriptions": r["descriptions"].split("\n") if r["descriptions"] else [],
                }
                for r in rows
            ],
            "total": total,
        }
    )


async def parse_food(request: Request) -> JSONResponse:
    """Распознать текст/фото едой моделью — без сохранения (см. докстринг
    модуля). Тот же ai_trainer.analyze_food, что зовёт бот: он и так принимает
    голые text/image_data_url, без объекта телеграм-сообщения, так что
    рефакторинг handlers/food_diary.py тут не понадобился.

    Дневная квота — общая с ботом (db.ai_food_usage), поэтому AI-разбор в
    приложении отнимает такую же попытку, как разбор в Telegram."""
    user_id = await common.authed_user_id(request)
    if not ai_trainer.is_configured():
        raise ApiError(503, "not_configured", "food analysis is not configured")
    body = await common.json_body(request)
    text = common.optional_str(body, "text") or ""
    image_data_url = common.optional_str(body, "image_data_url")
    correction = common.optional_str(body, "correction") or ""
    previous = body.get("previous")
    if previous is not None and not isinstance(previous, dict):
        raise ApiError(400, "bad_request", "previous must be an object")
    if not text and not image_data_url:
        raise ApiError(400, "bad_request", "text or image_data_url required")

    # preview-режим (свои аккаунты без "Понятно" за сегодня) в боте пропускает
    # шаг вместе с предупреждением — у API нет экрана, куда это предупреждение
    # показать, так что здесь любой Block, включая preview, значит «квота
    # исчерпана» и отдаётся как 429, без «показать и всё равно выполнить».
    block = await ai_limits.check(user_id, ai_limits.KIND_FOOD)
    if block is not None:
        raise ApiError(429, "food_limit_exceeded", "daily food analysis limit reached")

    user = await db.get_user(user_id)
    with_macros = bool(user["food_macros_enabled"]) if user else True
    try:
        estimate = await ai_trainer.analyze_food(
            user_id,
            text=text,
            image_data_url=image_data_url,
            previous=previous,
            correction=correction,
            with_macros=with_macros,
            source="ios",
        )
    except Exception as exc:
        raise ApiError(502, "analysis_failed", "food analysis failed") from exc

    # Квота тратится за состоявшийся разбор, как и в боте — сбой уже
    # произошёл бы раньше (см. except выше) и до сюда не дошёл.
    await db.increment_ai_food_count(user_id)
    return JSONResponse(estimate)


async def set_goal(request: Request) -> JSONResponse:
    """«🎯 Цель ккал» (handlers/food_diary.py fd_goal_entered) — тот же
    db.set_kcal_goal и те же границы (config.KCAL_GOAL_MIN/MAX), иначе
    в приложении можно было бы поставить цель, которую бот считает опечаткой.

    `goal: null` снимает цель — как и пустая история кнопки в боте нет, но
    GET /food её уже отдаёт как kcal_goal: null, симметрично."""
    user_id = await common.authed_user_id(request)
    body = await common.json_body(request)
    if "goal" not in body:
        raise ApiError(400, "bad_request", "missing field: goal")
    goal = body["goal"]
    if goal is not None:
        if not isinstance(goal, int) or isinstance(goal, bool):
            raise ApiError(400, "bad_request", "goal must be an int or null")
        if not (config.KCAL_GOAL_MIN <= goal <= config.KCAL_GOAL_MAX):
            raise ApiError(
                400,
                "out_of_range",
                f"goal must be between {config.KCAL_GOAL_MIN} and {config.KCAL_GOAL_MAX}",
            )
    await db.set_kcal_goal(user_id, goal)
    return JSONResponse({"kcal_goal": goal})


routes = [
    Route("/food", get_day, methods=["GET"]),
    Route("/food", add_entry, methods=["POST"]),
    Route("/food/days", list_days, methods=["GET"]),
    Route("/food/parse", parse_food, methods=["POST"]),
    Route("/food/goal", set_goal, methods=["POST"]),
    Route("/food/{entry_id:int}", get_entry, methods=["GET"]),
    Route("/food/{entry_id:int}", delete_entry, methods=["DELETE"]),
]
