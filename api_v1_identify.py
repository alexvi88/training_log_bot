"""REST `/v1` для «Что это за тренажёр» — ядро в exercise_identify.py, общее с
ботом (handlers/workout.py, фото в подборе упражнения).

- `POST /exercises/identify` `{"image_data_url": "data:image/jpeg;base64,…"}` —
  фото → `{result, title, tip, exercise, alternatives, limits}`. Те же проверки
  фото, что у `/ai/ask` (400 `photo_too_big`, 415 `unsupported_media_type`),
  то же согласие на AI, та же бронь «один платный шаг за раз» (`_busy`
  api_v1_ai) и та же квота вопроса: `ai_limits.check` до вызова (любой Block —
  429, как у `/ai/ask`), списание после ответа. `limits` — тот же объект, что
  в ответах `/ai/*`.
- `POST /exercises/identify/accept` `{"catalog_name": …}` или
  `{"name", "muscle_group", "equipment"}` → `{"exercise": <как в GET
  /exercises>}`: находит своё или заводит (копия каталога на языке атлета или
  своё), без модели и без квоты.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from starlette.requests import Request
from starlette.routing import Route

import ai_limits
import ai_trainer
import api_v1_ai
import api_v1_common as common
import config
import exercise_identify
import i18n
from api_v1_common import JSONResponse

logger = logging.getLogger(__name__)

ApiError = common.ApiError


async def identify_exercise(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    await api_v1_ai._require_ai_consent(request, user_id)
    if not ai_trainer.is_configured():
        raise ApiError(503, "not_configured", "ai trainer is not configured")
    body = await common.json_body(request)
    data_url = body.get("image_data_url")
    if not isinstance(data_url, str) or not data_url:
        raise ApiError(400, "bad_request", "image_data_url is required", key="exercise_identify.photo_required")
    lang = common.request_lang(request)
    api_v1_ai._validate_image_data_url(
        data_url,
        too_big_message=i18n.t("ai.screen.photo_too_big", mb=api_v1_ai.MAX_IMAGE_BYTES // (1024 * 1024)),
    )
    block = await ai_limits.check(user_id, ai_limits.KIND_QUESTION)
    if block is not None:
        raise ApiError(429, "question_limit_exceeded", "daily question limit reached", human=block.user_text)
    api_v1_ai._claim_turn_or_429(user_id, lang)
    try:
        identification = await asyncio.wait_for(
            exercise_identify.identify(user_id, data_url), timeout=config.AI_TOTAL_ANSWER_SECONDS,
        )
    except ai_trainer.LimitBlocked as exc:
        raise ApiError(
            429, "question_limit_exceeded", "daily question limit reached", human=exc.block.user_text,
        ) from exc
    except asyncio.TimeoutError as exc:
        raise ApiError(504, "timeout", "ai trainer did not answer in time") from exc
    except Exception as exc:
        logger.exception("exercise identify failed for user %s", user_id)
        raise ApiError(502, "answer_failed", "ai trainer failed to answer") from exc
    finally:
        api_v1_ai._busy.discard(user_id)
    payload: dict[str, Any] = identification.as_json()
    payload["limits"] = await api_v1_ai._limits_json(user_id)
    return JSONResponse(payload)


async def accept_identified(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    body = await common.json_body(request)
    try:
        row, _result = await exercise_identify.accept(
            user_id,
            catalog_name=body.get("catalog_name"),
            name=body.get("name"),
            muscle_group=body.get("muscle_group"),
            equipment=body.get("equipment"),
        )
    except exercise_identify.AcceptError as exc:
        raise ApiError(exc.status, exc.code, f"cannot accept: {exc.code}", key=exc.key, **exc.params) from exc
    return JSONResponse({"exercise": common.exercise_json(row)})


routes = [
    Route("/exercises/identify", api_v1_ai._drain_aware(identify_exercise), methods=["POST"]),
    Route("/exercises/identify/accept", accept_identified, methods=["POST"]),
]
