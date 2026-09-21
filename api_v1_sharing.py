"""REST `/v1` для шаринга программ и упражнений (в боте — handlers/sharing.py).

Механика та же, что у бота: шарится не живая ссылка, а снапшот (см.
db.shared_items и докстринг handlers/sharing.py) — владелец потом может
переименовать или удалить оригинал, визитка продолжит открываться, а
получатель ничего не импортирует, пока явно не попросит.

Транспорт поверх той же логики, что и у бота: снапшот собирают
sharing.build_program_payload / build_routine_payload / build_exercise_payload,
резолв упражнений при импорте и сам импорт — sharing.import_program /
import_routine / import_exercise. Здесь только HTTP: проверка владения,
разбор запроса и сборка JSON — вторая копия правил (лимиты, three-step резолв
имён) была бы нарушением единственного источника истины, поэтому её тут нет.

У REST-объекта нет aiogram-бота, чтобы спросить его username (bot.get_me() —
рантайм-вызов Telegram, у HTTP-запроса такого контекста нет), а выдумывать
username конфигом нельзя — конфиг его не хранит. Поэтому вместо готовой
ссылки отдаём token и start_param, а t.me/<username>?start=<start_param>
собирает клиент, зная свой собственный username бота.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import api_v1_common as common
import db
from handlers import sharing

ApiError = common.ApiError
_authed_user_id = common.authed_user_id


# ---------- владение (см. одноимённые помощники в api_v1_programs.py —
# не переиспользуются оттуда намеренно: у каждого REST-домена свой набор
# проверок, как и в api_v1_food.py с его _owned_entry) ----------

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


async def _owned_exercise(exercise_id: int, user_id: int):
    exercise = await db.get_exercise(exercise_id)
    if exercise is None or exercise["user_id"] != user_id:
        raise ApiError(404, "not_found", "exercise not found")
    return exercise


def _owner_json(owner) -> Optional[dict[str, Any]]:
    if owner is None:
        return None
    return {"user_id": owner["telegram_id"], "username": owner["username"]}


def _routine_payload_json(payload: dict[str, Any]) -> dict[str, Any]:
    """Структура одного дня/шаблона — то, что в боте рисуется текстом в
    _routine_preview_lines, здесь отдаётся как есть: разметку строит клиент."""
    return {
        "name": payload["name"],
        "exercises": [
            {"name": ex["name"], "target": ex.get("target")} for ex in payload["exercises"]
        ],
    }


def _program_payload_json(payload: dict[str, Any]) -> dict[str, Any]:
    """Структура многодневки. total_days/days_in_card — то же число, из
    которого бот в _omitted_days_note решает, уехало ли всё; клиент сам решает,
    как об этом сказать (или не показывать вовсе)."""
    in_card, total_days = sharing.program_days_totals(payload)
    return {
        "name": payload["name"],
        "description": payload.get("description"),
        "days": [
            {
                "name": day["name"],
                "exercises": [
                    {"name": ex["name"], "target": ex.get("target")} for ex in day["exercises"]
                ],
            }
            for day in payload["days"]
        ],
        "days_in_card": in_card,
        "total_days": total_days,
    }


def _exercise_payload_json(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": payload["name"],
        "group": payload.get("group"),
        "description": payload.get("description"),
        "has_photo": bool(payload.get("photo_file_id")),
    }


def _payload_json(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if kind == "program":
        return _program_payload_json(payload)
    if kind == "routine":
        return _routine_payload_json(payload)
    return _exercise_payload_json(payload)


def _share_created_json(token: str, kind: str) -> JSONResponse:
    return JSONResponse(
        {"token": token, "kind": kind, "start_param": f"{sharing.START_PREFIX}{token}"},
        status_code=201,
    )


# ---------- владелец: создание визитки ----------

async def share_program(request: Request) -> JSONResponse:
    """Снапшот многодневки (программы). Один снапшот на всю программу, как и
    у кнопки «Поделиться» на экране «✏️ Изменить программу» — не по дню."""
    user_id = await _authed_user_id(request)
    program_id = int(request.path_params["program_id"])
    program = await _owned_program(program_id, user_id)
    payload = await sharing.build_program_payload(program_id, program["name"])
    if payload is None:
        raise ApiError(400, "empty_program", "program has no exercises to share")
    token = await db.create_shared_item(user_id, "program", json.dumps(payload, ensure_ascii=False))
    return _share_created_json(token, "program")


async def share_routine(request: Request) -> JSONResponse:
    """Снапшот одного дня (share:rt: в handlers/sharing.py) — «📤 Поделиться»
    на экране одного дня программы, в отличие от share_program выше (вся
    многодневка). Приём такой визитки уже есть в import_share ниже: снапшот
    несёт kind="routine", и sharing.import_routine его давно умеет —
    отдельного эндпоинта на приём заводить не нужно."""
    user_id = await _authed_user_id(request)
    routine_id = int(request.path_params["routine_id"])
    routine = await _owned_routine(routine_id, user_id)
    exercises = await db.list_routine_exercises(routine_id)
    if not exercises:
        raise ApiError(400, "empty_routine", "day has no exercises to share")
    payload = sharing.build_routine_payload(routine["name"], exercises)
    token = await db.create_shared_item(user_id, "routine", json.dumps(payload, ensure_ascii=False))
    return _share_created_json(token, "routine")


async def share_exercise(request: Request) -> JSONResponse:
    user_id = await _authed_user_id(request)
    exercise_id = int(request.path_params["exercise_id"])
    ex = await _owned_exercise(exercise_id, user_id)
    group = await db.get_muscle_group(ex["primary_group_id"]) if ex["primary_group_id"] else None
    payload = sharing.build_exercise_payload(ex, group)
    token = await db.create_shared_item(user_id, "exercise", json.dumps(payload, ensure_ascii=False))
    return _share_created_json(token, "exercise")


# ---------- получатель: превью и импорт ----------

async def get_share_preview(request: Request) -> JSONResponse:
    """Превью снапшота без импорта — «Тебе прислали программу» из бота, но
    структурой, а не HTML-текстом (клиент сам рисует экран).

    Доступно любому авторизованному пользователю, включая владельца: чужой
    токен здесь — не нарушение владения, а весь смысл шаринга (получатель по
    определению не хозяин), поэтому 404 отвечает только на битый/отозванный
    токен, а не на "не твоё"."""
    user_id = await _authed_user_id(request)
    token = request.path_params["token"]
    row = await db.get_shared_item(token)
    if row is None:
        raise ApiError(404, "not_found", "share link is broken or revoked")
    payload = json.loads(row["payload"])
    payload.setdefault("v", 0)
    owner = await db.get_user(row["owner_id"])
    return JSONResponse(
        {
            "token": token,
            "kind": row["kind"],
            "owner": _owner_json(owner),
            "is_own": row["owner_id"] == user_id,
            "created_at": row["created_at"],
            row["kind"]: _payload_json(row["kind"], payload),
        }
    )


async def import_share(request: Request) -> JSONResponse:
    """«➕ Добавить себе» — единственная ручка, которая правда что-то создаёт;
    get_share_preview выше нарочно этого не делает ни при каких условиях."""
    user_id = await _authed_user_id(request)
    # Тот же приём, что у бота (handlers.sharing.share_add): проверка бюджета
    # и сама запись программы не атомарны, и два запроса подряд (двойной тап
    # в приложении, повтор после таймаута) видят один и тот же бюджет до того,
    # как первый успел закоммитить — вместе заводят больше программ, чем
    # разрешает db.routine_budget. Общий с ботом _importing — тот же процесс,
    # тот же пользователь не может писать в двух местах сразу.
    if not sharing._try_claim_importing(user_id):
        raise ApiError(409, "import_in_progress", "an import for this account is already running")
    try:
        return await _do_import_share(request, user_id)
    finally:
        sharing._importing.discard(user_id)


async def _do_import_share(request: Request, user_id: int) -> JSONResponse:
    token = request.path_params["token"]
    row = await db.get_shared_item(token)
    if row is None:
        raise ApiError(404, "not_found", "share link is broken or revoked")
    if row["owner_id"] == user_id:
        raise ApiError(400, "own_card", "can't import your own shared item")
    payload = json.loads(row["payload"])

    # Тот же общий бюджет, что и у бота (см. handlers.sharing.share_add) и у
    # остальных трёх дверей создания программ (db.routine_budget) — раньше
    # присланной программой на 40 дней его можно было перешагнуть в обход бота.
    incoming = sharing.incoming_days_count(row["kind"], payload)
    over_budget = await db.routine_budget(user_id, incoming)
    if over_budget:
        raise ApiError(403, "routine_limit_reached", over_budget)

    if row["kind"] == "program":
        owner = await db.get_user(row["owner_id"])
        owner_name = owner["username"] if owner else None
        program_id, program_name = await sharing.import_program(user_id, payload, owner_name)
        await db.mark_shared_item_taken(token)
        return JSONResponse(
            {"kind": "program", "program_id": program_id, "name": program_name, "days": len(payload["days"])},
            status_code=201,
        )

    if row["kind"] == "routine":
        routine_id = await sharing.import_routine(user_id, payload)
        await db.mark_shared_item_taken(token)
        return JSONResponse({"kind": "routine", "routine_id": routine_id, "name": payload["name"]}, status_code=201)

    # kind == "exercise"
    ex_id, already_existed = await sharing.import_exercise(user_id, payload)
    if already_existed:
        raise ApiError(409, "exercise_exists", "an exercise with this name already exists")
    await db.mark_shared_item_taken(token)
    return JSONResponse({"kind": "exercise", "exercise_id": ex_id, "name": payload["name"]}, status_code=201)


# ---------- владелец: отзыв визитки ----------

async def revoke_share(request: Request) -> JSONResponse:
    """«🚫 Отозвать ссылку» из владельческой визитки бота (handlers.sharing.
    share_revoke) — до сих пор из приложения ссылка не отзывалась никак, то
    есть отданный снапшот тренировок жил вечно.

    Отзыв — это удаление строки, а не флаг: ровно как в боте (db.
    delete_shared_item). Поэтому все уже разосланные копии ссылки умирают
    разом, а get_share_preview/import_share выше начинают отвечать своим
    404 "broken or revoked" — отдельного «отозвано» получателю не показываем,
    у бота он тоже видит просто «ссылка устарела».

    Чужой токен — те же 404 not_found, что и битый: в боте это два разных
    текста («не твоя визитка» против «отзывать нечего»), но там отвечают
    владельцу на его же кнопку, а здесь ответ разделил бы для постороннего
    существующие токены и несуществующие, чего угадывать по HTTP не нужно.

    taken_count отдаём потому, что бот о нём говорит в момент отзыва
    (share.link_revoked_with_taken): «отозвал, но N уже забрали» — это
    единственный шанс человека узнать, что копии всё же разошлись.
    """
    user_id = await _authed_user_id(request)
    token = request.path_params["token"]
    row = await db.get_shared_item(token)
    if row is None or not await db.delete_shared_item(token, user_id):
        raise ApiError(404, "not_found", "share link is broken or revoked")
    return JSONResponse({"revoked": True, "kind": row["kind"], "taken_count": row["taken_count"]})


routes = [
    Route("/share/programs/{program_id:int}", share_program, methods=["POST"]),
    Route("/share/routines/{routine_id:int}", share_routine, methods=["POST"]),
    Route("/share/exercises/{exercise_id:int}", share_exercise, methods=["POST"]),
    Route("/share/{token}", get_share_preview, methods=["GET"]),
    Route("/share/{token}", revoke_share, methods=["DELETE"]),
    Route("/share/{token}/import", import_share, methods=["POST"]),
]
