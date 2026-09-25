"""REST `/v1/funnel` — анонимная воронка приложения ДО входа.

Зачем. Лента действий (`api_v1_activity`, `db.user_events`) пишет только
запросы вошедших: всё, что человек видел до «Войти с Apple» — слайды
онбординга, экран про уведомления, сам вход, — оставалось невидимым, и было
не понять, на каком слайде люди закрывают приложение. Здесь приложение
сообщает «эта установка дошла до шага N».

Без PII: `install_id` — случайный UUID, который приложение само завело в
UserDefaults (не IDFV, не рекламный идентификатор); после входа он нигде не
связывается с аккаунтом. Адрес клиента используется только для счётчика
частоты в памяти процесса и в базу не пишется. `lang` сводится к "ru"/"en".

Защита от спама — три слоя, по образцу остальных неавторизованных ручек
(`review_demo`, `mcp_oauth.RegisterRateLimitMiddleware` — счётчики в памяти
процесса, грубый предохранитель, рестарт переживать не обязаны):
  - тело не длиннее MAX_BODY_BYTES — у честного клиента оно в сотню байт;
  - не больше LIMIT_PER_IP запросов с адреса за WINDOW_SECONDS и не больше
    LIMIT_TOTAL со всех адресов за то же окно;
  - в базе строка на пару (установка, шаг) — `INSERT OR IGNORE` по UNIQUE,
    так что одна установка не наплодит больше строк, чем шагов в STEPS.
Шаг вне белого списка — 400: произвольные строки в базу не попадают.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import db
import i18n
import review_demo
from api_v1_common import ApiError

# Белый список шагов. Меняется вместе с приложением (FunnelTracker.swift в
# training_log_bot_ios) — новый шаг сначала сюда, иначе сервер его отвергнет.
STEPS = frozenset(
    {
        "onboarding_slide_1",
        "onboarding_slide_2",
        "onboarding_slide_3",
        "onboarding_slide_4",
        "onboarding_slide_5",
        "push_priming",
        "signin_shown",
        "signin_tapped",
        "signin_ok",
        "signin_failed",
    }
)

MAX_BODY_BYTES = 512
WINDOW_SECONDS = 600
# Онбординг — десяток шагов на установку; 60 за 10 минут с одного адреса
# хватает и нескольким телефонам за одним NAT оператора.
LIMIT_PER_IP = 60
LIMIT_TOTAL = 3000

_hits: dict[str, list[float]] = {}
_total: list[float] = []
_PRUNE_AT = 1024


def reset_limits() -> None:
    """Для тестов: счётчики живут в памяти процесса, а тесты — в одном процессе."""
    _hits.clear()
    _total.clear()


def _allow(ip: str, now: Optional[float] = None) -> bool:
    now = time.monotonic() if now is None else now
    start = now - WINDOW_SECONDS
    _total[:] = [t for t in _total if t > start]
    hits = [t for t in _hits.get(ip, []) if t > start]
    if len(hits) >= LIMIT_PER_IP or len(_total) >= LIMIT_TOTAL:
        _hits[ip] = hits
        return False
    hits.append(now)
    _hits[ip] = hits
    _total.append(now)
    if len(_hits) > _PRUNE_AT:
        for key in list(_hits):
            if not any(t > start for t in _hits[key]):
                del _hits[key]
    return True


def _install_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ApiError(400, "bad_request", "install_id must be a UUID string")
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError as exc:
        raise ApiError(400, "bad_request", "install_id must be a UUID string") from exc


async def log_funnel_step(request: Request) -> JSONResponse:
    """Тело: {"install_id": "<UUID>", "step": "<из STEPS>", "lang"?: "ru"|"en"|...}.
    Ответ: {"recorded": bool} — False, если эта установка уже доходила до шага."""
    request.state.lang = i18n.normalize(i18n.lang_from_accept_language(request.headers.get("accept-language")))
    if not _allow(review_demo.client_ip(request)):
        raise ApiError(429, "rate_limited", "too many funnel events, try again later")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(400, "bad_request", "funnel body too large")
    try:
        body = json.loads(raw or b"null")
    except ValueError as exc:
        raise ApiError(400, "bad_request", "invalid JSON body") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", "JSON object expected")
    raw_lang = body.get("lang")
    lang = i18n.normalize(raw_lang) if isinstance(raw_lang, str) and raw_lang.strip() else None
    if lang:
        request.state.lang = lang
    install_id = _install_id(body.get("install_id"))
    step = body.get("step")
    if not isinstance(step, str) or step not in STEPS:
        raise ApiError(400, "bad_request", "unknown funnel step")
    recorded = await db.log_funnel_event(install_id, step, lang)
    return JSONResponse({"recorded": recorded})


routes = [
    Route("/funnel", log_funnel_step, methods=["POST"]),
]
