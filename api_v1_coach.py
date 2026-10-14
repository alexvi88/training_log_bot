"""REST `/v1` сервиса для тренеров (правила — coach_service.py).

Две стороны одной связки:

- тренер: `/coach` (стать тренером, имя, код приглашения), `/coach/clients…`
  (список с сигналами, дневник атлета только на чтение, прогресс — недели,
  движения и рекорды, «Все упражнения» и прогресс любого из них тем же
  ответом, что у атлета, назначить программу,
  комментарий к тренировке, убрать клиента), `/coach/ai-summary` (POST —
  собрать сводку, GET — последняя сохранённая);
- атлет: `/coach/invites/{code}` (кто зовёт и что увидит → «Разрешить»),
  `/coach-links…` (мои тренеры: что открыто, отозвать) и комментарии тренера
  под своей тренировкой (`/workouts/{id}/coach-comments`).

Каждый id атлета из URL проверяется связкой (`_linked_client`) до любого чтения:
нет связки — 404 `not_linked`, и чужой дневник не отличить от
несуществующего. Вес тела — 403 `coach_hidden`, пока атлет его не открыл.
Переписки с AI-тренером здесь нет ни в одном ответе.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from starlette.convertors import Convertor, register_url_convertor
from starlette.requests import Request
from starlette.routing import Route

import ai_trainer
import api_v1_ai
import api_v1_common as common
import api_v1_progress
import coach_notify
import coach_service
import config
import db
import i18n
import keyboards
from api_v1_common import JSONResponse

logger = logging.getLogger(__name__)

ApiError = common.ApiError


class _UserIdConvertor(Convertor[int]):
    """id атлета или тренера в пути. У аккаунта только из приложения он
    синтетический и отрицательный (db.create_app_only_user), а встроенный
    `:int` Starlette знает только `[0-9]+` — и `/coach/clients/-1000000000`
    отвечал 404 без захода в обработчик: тренер не открывал клиента из
    приложения, атлет не мог закрыть доступ тренеру без Telegram."""

    regex = "-?[0-9]+"

    def convert(self, value: str) -> int:
        return int(value)

    def to_string(self, value: int) -> str:
        return str(int(value))


register_url_convertor("user_id", _UserIdConvertor())

# ---------- общие проверки ----------


async def _coach_profile(user_id: int):
    profile = await db.get_coach_profile(user_id)
    if profile is None:
        raise ApiError(403, "not_a_coach", "coach profile is required")
    return profile


async def _linked_client(coach_id: int, client_id: int):
    """Связка и строка атлета — или 404 `not_linked` (и для чужого, и для
    несуществующего id: снаружи их не различить)."""
    link = await db.get_coach_link(coach_id, client_id)
    client = await db.get_user(client_id) if link is not None else None
    if link is None or client is None:
        raise ApiError(404, "not_linked", "no such client for this coach")
    return link, client


async def _client_workout(client_id: int, workout_id: int):
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != client_id:
        raise ApiError(404, "not_found", "workout not found")
    return workout


def _client_id(request: Request) -> int:
    return int(request.path_params["client_id"])


def _bool_field(body: dict[str, Any], key: str) -> Optional[bool]:
    if key not in body or body[key] is None:
        return None
    value = body[key]
    if not isinstance(value, bool):
        raise ApiError(400, "bad_request", f"{key} must be a boolean")
    return value


def _display_name(raw: Any) -> str:
    name = coach_service.clean_display_name(raw)
    if name is None:
        raise ApiError(400, "bad_request", "name must not be empty", key="api.error.name_empty")
    if len(name) > coach_service.MAX_DISPLAY_NAME_LENGTH:
        raise ApiError(
            400, "name_too_long", "name is too long", key="api.error.name_too_long",
            max=coach_service.MAX_DISPLAY_NAME_LENGTH,
        )
    return name


def _comment_json(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workout_id": row["workout_id"],
        "text": row["text"],
        "coach_name": row["coach_name"],
        "created_at": row["created_at"],
        "is_read": row["read_at"] is not None,
    }


async def _client_json(coach_id: int, link, client) -> dict[str, Any]:
    return {
        "client_id": link["client_id"],
        "name": link["client_name"],
        "linked_at": link["created_at"],
        "unit": client["unit"],
        "visibility": coach_service.visibility_json(link),
        "signals": await coach_service.client_signals(client, link, coach_id),
    }


async def _coach_state_json(user_id: int) -> dict[str, Any]:
    profile = await db.get_coach_profile(user_id)
    invite = await db.get_coach_invite_for(user_id) if profile is not None else None
    clients = await db.list_coach_clients(user_id) if profile is not None else []
    return {
        "is_coach": profile is not None,
        "display_name": profile["display_name"] if profile is not None else None,
        "invite": coach_service.invite_json(invite["code"]) if invite is not None else None,
        "client_count": len(clients),
    }


# ---------- тренер: профиль и приглашение ----------


async def get_coach(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    return JSONResponse(await _coach_state_json(user_id))


async def set_coach(request: Request) -> JSONResponse:
    """«Я тренер»: имя, под которым тебя видят атлеты. Повтор — переименование.
    Первый раз заодно выдаёт код приглашения."""
    user_id = await common.authed_user_id(request)
    body = await common.json_body(request)
    name = _display_name(body.get("display_name"))
    await db.upsert_coach_profile(user_id, name)
    if await db.get_coach_invite_for(user_id) is None:
        await db.replace_coach_invite(user_id, coach_service.new_invite_code())
    return JSONResponse(await _coach_state_json(user_id))


async def renew_invite(request: Request) -> JSONResponse:
    """Новый код вместо прежнего: старый перестаёт открываться. Уже
    связанные атлеты остаются — их доступ держит связка, а не код."""
    user_id = await common.authed_user_id(request)
    await _coach_profile(user_id)
    code = coach_service.new_invite_code()
    await db.replace_coach_invite(user_id, code)
    return JSONResponse(coach_service.invite_json(code), status_code=201)


# ---------- тренер: клиенты ----------


async def list_clients(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    await _coach_profile(user_id)
    items = []
    for link in await db.list_coach_clients(user_id):
        client = await db.get_user(link["client_id"])
        if client is not None:
            items.append(await _client_json(user_id, link, client))
    return JSONResponse({"clients": items})


async def get_client(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    link, client = await _linked_client(user_id, _client_id(request))
    return JSONResponse({
        "client": await _client_json(user_id, link, client),
        "records": await coach_service.recent_records(client),
        "programs": await coach_service.assigned_programs(user_id, link["client_id"]),
        # Шапка карточки: тренировки по неделям и в среднем — без движений и
        # рекордов «Прогресса», которые дороже и нужны только там.
        "activity": await coach_service.client_activity(client),
    })


async def client_progress(request: Request) -> JSONResponse:
    """«Прогресс клиента»: частота и тоннаж по неделям, основные движения
    по неделям, рекорды за всё время и свежие (coach_service.client_progress).
    Та же проверка связки, что у списка тренировок: чужой или бывший
    клиент — 404 `not_linked`. Имена упражнений — на языке тренера."""
    user_id = await common.authed_user_id(request)
    _, client = await _linked_client(user_id, _client_id(request))
    weeks = common.query_int(
        request, "weeks", coach_service.PROGRESS_WEEKS,
        minimum=coach_service.PROGRESS_MIN_WEEKS, maximum=coach_service.PROGRESS_MAX_WEEKS,
    )
    return JSONResponse(await coach_service.client_progress(client, weeks))


async def client_exercises(request: Request) -> JSONResponse:
    """«Все упражнения» клиента для прогресса: всё, в чём у атлета есть
    законченные подходы, частые и свежие выше (coach_service.client_exercises).
    Чужой или бывший клиент, сам атлет со своим id — 404 `not_linked`."""
    user_id = await common.authed_user_id(request)
    _, client = await _linked_client(user_id, _client_id(request))
    return JSONResponse({
        "unit": client["unit"],
        "exercises": await coach_service.client_exercises(client),
    })


async def client_exercise_progress(request: Request) -> JSONResponse:
    """Прогресс одного упражнения клиента — ровно та же форма, что у атлета
    на `GET /exercises/{id}/progress/sessions` (одна сборка,
    api_v1_progress.series_payload): формула, единица и пояс — атлета,
    подписи — на языке тренера. Сначала связка (404 `not_linked`), потом
    упражнение этого атлета (404 `not_found` — и для чужого, и для
    несуществующего). `limit` — те же 10 / 20 / «все»."""
    user_id = await common.authed_user_id(request)
    _, client = await _linked_client(user_id, _client_id(request))
    exercise_id = int(request.path_params["exercise_id"])
    if await coach_service.client_exercise(client, exercise_id) is None:
        raise ApiError(404, "not_found", "exercise not found")
    limit = common.query_int(
        request, "limit", keyboards.DEFAULT_PROGRESS_LIMIT, minimum=1, maximum=api_v1_progress.MAX_LIMIT
    )
    return JSONResponse(await api_v1_progress.series_payload(exercise_id, client, limit))


async def remove_client(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    link, _ = await _linked_client(user_id, _client_id(request))
    await db.delete_coach_link(user_id, link["client_id"])
    return JSONResponse({"removed": True})


async def client_workouts(request: Request) -> JSONResponse:
    import api_v1

    user_id = await common.authed_user_id(request)
    link, _ = await _linked_client(user_id, _client_id(request))
    limit = common.query_int(request, "limit", 20, minimum=1, maximum=100)
    offset = common.query_int(request, "offset", 0, minimum=0)
    workouts = await db.list_workouts(link["client_id"], limit=limit, offset=offset, status="finished")
    ids = [w["id"] for w in workouts]
    contents = await db.list_workout_contents(ids)
    comments = await db.count_coach_comments_by_workout(ids)
    items = []
    for w in workouts:
        names, set_count = contents.get(w["id"], ([], 0))
        item = api_v1._workout_json(w)
        item["exercise_names"] = coach_service.shown_exercise_names(names)
        item["set_count"] = set_count
        item["coach_comment_count"] = comments.get(w["id"], 0)
        items.append(item)
    return JSONResponse({"workouts": items})


async def client_workout(request: Request) -> JSONResponse:
    """Тренировка атлета на чтение — та же сборка, что у GET /workouts/{id}
    (api_v1._workout_detail_json), только тексты (рекорды, теги групп) — на
    языке тренера, а единица веса приходит рядом: тренер в кг может смотреть
    атлета в lb."""
    import api_v1

    user_id = await common.authed_user_id(request)
    link, client = await _linked_client(user_id, _client_id(request))
    workout = await _client_workout(link["client_id"], int(request.path_params["workout_id"]))
    if workout["status"] != "finished":
        raise ApiError(404, "not_found", "workout not found")
    viewer = dict(client)
    viewer["lang"] = i18n.get_lang()
    detail = await api_v1._workout_detail_json(workout, viewer)
    # Каталожные упражнения — на языке тренера, как и остальные тексты.
    for block in detail["blocks"]:
        for exercise in block["exercises"]:
            exercise["display_name"] = coach_service.shown_exercise_names([exercise["display_name"]])[0]
    comments = await db.list_coach_comments(workout["id"], coach_id=user_id)
    return JSONResponse({
        "workout": detail,
        "unit": client["unit"],
        "comments": [_comment_json(c) for c in comments],
    })


async def comment_workout(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    profile = await _coach_profile(user_id)
    link, client = await _linked_client(user_id, _client_id(request))
    workout = await _client_workout(link["client_id"], int(request.path_params["workout_id"]))
    if workout["status"] != "finished":
        raise ApiError(409, "coach_workout_active", "workout is not finished")
    body = await common.json_body(request)
    text = common.optional_str(body, "text") or ""
    text = text.strip()
    if not text:
        raise ApiError(400, "bad_request", "text must not be empty", key="api.error.text_empty")
    if len(text) > coach_service.MAX_COMMENT_LENGTH:
        raise ApiError(
            400, "bad_request", "text is too long", key="api.error.text_too_long",
            max=coach_service.MAX_COMMENT_LENGTH,
        )
    row = await db.add_coach_comment(user_id, link["client_id"], workout["id"], text)
    # Банер на iPhone и сообщение в Telegram — на языке атлета (coach_notify).
    pushed, _ = await coach_notify.deliver_comment(client, profile["display_name"], text, workout["id"])
    data = _comment_json({**dict(row), "coach_name": profile["display_name"]})
    data["pushed"] = pushed
    return JSONResponse(data, status_code=201)


async def assign_program(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    profile = await _coach_profile(user_id)
    link, client = await _linked_client(user_id, _client_id(request))
    body = await common.json_body(request)
    program_id = common.require(body, "program_id", int)
    try:
        new_id, name = await coach_service.assign_program(user_id, link["client_id"], program_id)
    except coach_service.AssignError as exc:
        if exc.code == "not_found":
            raise ApiError(404, "not_found", "program not found") from exc
        if exc.code == "name_taken":
            raise ApiError(409, "name_taken", "program name collision") from exc
        status = 400 if exc.code == "coach_program_empty" else 403
        raise ApiError(status, exc.code, f"cannot assign: {exc.code}") from exc
    # Банер на iPhone и сообщение в Telegram — на языке атлета (coach_notify).
    await coach_notify.deliver_program(client, profile["display_name"], name, new_id)
    return JSONResponse({"program_id": new_id, "name": name}, status_code=201)


async def client_bodyweight(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    link, client = await _linked_client(user_id, _client_id(request))
    if not link["show_bodyweight"]:
        raise ApiError(403, "coach_hidden", "bodyweight is not shared with the coach")
    limit = common.query_int(request, "limit", 30, minimum=1, maximum=365)
    rows = await db.list_bodyweight_logs(link["client_id"], limit=limit)
    return JSONResponse({
        "unit": client["unit"],
        "entries": [{"id": r["id"], "weight": r["weight"], "logged_at": r["logged_at"]} for r in rows],
    })


async def ai_summary(request: Request) -> JSONResponse:
    """AI-сводка тренеру: по всем клиентам или по одному (`client_id`).
    Тот же лимит вопроса тренеру, что у чата (ai_limits через paid_call), то
    же согласие на AI у самого тренера и та же бронь «один ход за раз»."""
    user_id = await common.authed_user_id(request)
    await _coach_profile(user_id)
    body = await common.json_body(request)
    client_id = common.optional_int(body, "client_id")
    if client_id is not None:
        await _linked_client(user_id, client_id)
    await api_v1_ai._require_ai_consent(request, user_id)
    if not ai_trainer.is_configured():
        raise ApiError(503, "not_configured", "ai trainer is not configured")
    lang = common.request_lang(request)
    api_v1_ai._claim_turn_or_429(user_id, lang)
    try:
        with i18n.use_lang(lang):
            result = await asyncio.wait_for(
                coach_service.ai_summary(user_id, client_id), timeout=config.AI_TOTAL_ANSWER_SECONDS
            )
    except ai_trainer.LimitBlocked as exc:
        raise ApiError(
            429, "question_limit_exceeded", "daily question limit reached", human=exc.block.user_text
        ) from exc
    except asyncio.TimeoutError as exc:
        raise ApiError(504, "timeout", "coach summary did not finish in time") from exc
    except ApiError:
        raise
    except Exception as exc:
        logger.exception("coach: ai summary failed for %s", user_id)
        raise ApiError(502, "answer_failed", "coach summary failed") from exc
    finally:
        api_v1_ai._busy.discard(user_id)
    return JSONResponse(result)


async def saved_ai_summary(request: Request) -> JSONResponse:
    """Последняя сохранённая AI-сводка (`?client_id=` — по одному клиенту, без
    него — по всем). Бесплатно: модель не зовётся. `summary` — null, если
    сводки нет, она старше срока хранения, связку разрывали или сводка
    написана на другом языке, чем сейчас у тренера (coach_service.saved_summary).
    Форма `summary` — та же, что у ответа POST. `summary.stale` — данные
    клиентов с тех пор поменялись (новый клиент, тренировка, рекорд,
    видимость): как текущую её не показывать. Приложение тогда само зовёт
    POST за свежей, а упёрлось в лимит — показывает эту с пометкой «устарела».
    GET модель не зовёт никогда — он бесплатный."""
    user_id = await common.authed_user_id(request)
    await _coach_profile(user_id)
    raw = request.query_params.get("client_id")
    client_id: Optional[int] = None
    if raw not in (None, ""):
        try:
            client_id = int(raw)
        except ValueError as exc:
            raise ApiError(400, "bad_request", "client_id must be int") from exc
        await _linked_client(user_id, client_id)
    return JSONResponse({"summary": await coach_service.saved_summary(user_id, client_id)})


# ---------- атлет: приглашение и мои тренеры ----------


async def _invite(code_raw: str):
    code = coach_service.normalize_invite_code(code_raw)
    invite = await db.get_coach_invite(code) if code else None
    profile = await db.get_coach_profile(invite["owner_id"]) if invite is not None else None
    if invite is None or profile is None:
        raise ApiError(404, "invite_not_found", "invite code is unknown or renewed")
    return invite, profile


async def invite_preview(request: Request) -> JSONResponse:
    """Кто зовёт и что увидит — до «Разрешить» ничего не открыто."""
    user_id = await common.authed_user_id(request)
    invite, profile = await _invite(request.path_params["code"])
    link = await db.get_coach_link(invite["owner_id"], user_id)
    return JSONResponse({
        "code": invite["code"],
        "coach_name": profile["display_name"],
        "is_own": invite["owner_id"] == user_id,
        "already_linked": link is not None,
        "visibility": coach_service.visibility_json(link),
    })


async def accept_invite(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    invite, profile = await _invite(request.path_params["code"])
    coach_id = invite["owner_id"]
    if coach_id == user_id:
        raise ApiError(400, "own_invite", "can't accept your own invite")
    body = await common.json_body(request)
    name = _display_name(body.get("name"))
    show_bodyweight = bool(_bool_field(body, "show_bodyweight"))
    created = await db.create_coach_link(coach_id, user_id, name, show_bodyweight=show_bodyweight)
    if not created:
        raise ApiError(409, "coach_already_linked", "this coach is already linked")
    link = await db.get_coach_link(coach_id, user_id)
    # Тренеру — «новый клиент» банером и в Telegram, на его языке.
    await coach_notify.deliver_new_client(coach_id, link["client_name"])
    return JSONResponse(_my_coach_json(link, profile["display_name"]), status_code=201)


def _my_coach_json(link, coach_name: Optional[str]) -> dict[str, Any]:
    return {
        "coach_id": link["coach_id"],
        "coach_name": coach_name,
        "my_name": link["client_name"],
        "linked_at": link["created_at"],
        "visibility": coach_service.visibility_json(link),
    }


async def list_my_coaches(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    rows = await db.list_client_coaches(user_id)
    return JSONResponse({"coaches": [_my_coach_json(r, r["coach_name"]) for r in rows]})


async def _my_link(user_id: int, coach_id: int):
    link = await db.get_coach_link(coach_id, user_id)
    if link is None:
        raise ApiError(404, "not_found", "no such coach link")
    return link


async def update_my_coach(request: Request) -> JSONResponse:
    """Что открыто тренеру (вес тела) и имя для тренера. Незнакомые поля тела
    молча пропускаются."""
    user_id = await common.authed_user_id(request)
    coach_id = int(request.path_params["coach_id"])
    await _my_link(user_id, coach_id)
    body = await common.json_body(request)
    name = _display_name(body["name"]) if body.get("name") is not None else None
    await db.update_coach_link_visibility(
        coach_id, user_id,
        show_bodyweight=_bool_field(body, "show_bodyweight"),
        client_name=name,
    )
    link = await db.get_coach_link(coach_id, user_id)
    profile = await db.get_coach_profile(coach_id)
    return JSONResponse(_my_coach_json(link, profile["display_name"] if profile is not None else None))


async def revoke_my_coach(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    coach_id = int(request.path_params["coach_id"])
    await _my_link(user_id, coach_id)
    await db.delete_coach_link(coach_id, user_id)
    return JSONResponse({"revoked": True})


async def my_workout_comments(request: Request) -> JSONResponse:
    """Комментарии тренеров под своей тренировкой; открыл — прочитал."""
    user_id = await common.authed_user_id(request)
    workout_id = int(request.path_params["workout_id"])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != user_id:
        raise ApiError(404, "not_found", "workout not found")
    comments = await db.list_coach_comments(workout_id)
    await db.mark_coach_comments_read(workout_id, user_id)
    return JSONResponse({"comments": [_comment_json(c) for c in comments]})


routes = [
    Route("/coach", get_coach, methods=["GET"]),
    Route("/coach", set_coach, methods=["POST"]),
    Route("/coach/invite", renew_invite, methods=["POST"]),
    Route("/coach/clients", list_clients, methods=["GET"]),
    Route("/coach/clients/{client_id:user_id}", get_client, methods=["GET"]),
    Route("/coach/clients/{client_id:user_id}", remove_client, methods=["DELETE"]),
    Route("/coach/clients/{client_id:user_id}/workouts", client_workouts, methods=["GET"]),
    Route("/coach/clients/{client_id:user_id}/progress", client_progress, methods=["GET"]),
    Route("/coach/clients/{client_id:user_id}/workouts/{workout_id:int}", client_workout, methods=["GET"]),
    Route("/coach/clients/{client_id:user_id}/exercises", client_exercises, methods=["GET"]),
    Route(
        "/coach/clients/{client_id:user_id}/exercises/{exercise_id:int}/progress",
        client_exercise_progress, methods=["GET"],
    ),
    Route(
        "/coach/clients/{client_id:user_id}/workouts/{workout_id:int}/comments",
        comment_workout, methods=["POST"],
    ),
    Route("/coach/clients/{client_id:user_id}/programs", assign_program, methods=["POST"]),
    Route("/coach/clients/{client_id:user_id}/bodyweight", client_bodyweight, methods=["GET"]),
    Route("/coach/ai-summary", saved_ai_summary, methods=["GET"]),
    Route("/coach/ai-summary", ai_summary, methods=["POST"]),
    Route("/coach/invites/{code}", invite_preview, methods=["GET"]),
    Route("/coach/invites/{code}/accept", accept_invite, methods=["POST"]),
    Route("/coach-links", list_my_coaches, methods=["GET"]),
    Route("/coach-links/{coach_id:user_id}", update_my_coach, methods=["PATCH"]),
    Route("/coach-links/{coach_id:user_id}", revoke_my_coach, methods=["DELETE"]),
    Route("/workouts/{workout_id:int}/coach-comments", my_workout_comments, methods=["GET"]),
]
