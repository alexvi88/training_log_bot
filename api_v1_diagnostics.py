"""REST `/v1/diagnostics` — отчёты о сбоях iOS-приложения из MetricKit.

Зачем своё, а не только Xcode Organizer: Organizer показывает падения лишь тех,
кто согласился делиться аналитикой с разработчиками, и с задержкой в дни.
MetricKit отдаёт приложению те же диагностики (`MXDiagnosticPayload`) раз в
сутки на каждом устройстве, и приложение пересылает их сюда
(`TrainingLog/App/CrashReporter.swift` в iOS-репо).

**Тело** — одна диагностика на запрос:

    {"kind": "crash" | "hang" | "cpu_exception" | "disk_write_exception",
     "payload": {...},              # diagnostic.jsonRepresentation() как есть
     "app_version": "1.4", "build": "57", "os_version": "17.5.1",
     "device": "iPhone15,2"}

Одна, а не весь `MXDiagnosticPayload` разом: у каждой свой вид (колонка
`kind`, по ней сводка), и одно тяжёлое падение со стеком не тащит за собой в
413 остальные. Версия/сборка/iOS/модель берутся из `payload.diagnosticMetaData`
(это тот запуск, который упал), а поля тела — только запасом, если Apple их
не положил: MetricKit отдаёт отчёт через сутки, и приложение к тому времени
часто уже обновилось.

**Без входа тоже.** Сбой на экране входа или в онбординге — ровно тот, что
важнее всего увидеть, а токена там ещё нет. Поэтому авторизация
необязательна: валидный Bearer — строка с `user_id`, нет токена или он
отозван — строка анонимная, но не 401 (отчёт про падение не должен теряться
из-за того, что человек успел выйти). `request.state.user_id` при этом не
ставится: отчёт о сбое — не действие атлета, и в ленту `/activity` он не
пишется.

**Потолок тела** — `MAX_BODY_BYTES` (256 КБ), свой и много ниже общего
`config.MAX_REQUEST_BODY_BYTES` (body_limit.py, он под видео): отчёт — это
текст, и что-то тяжелее — не отчёт. Ответ на превышение — тот же 413 с
`payload_too_large`, что у общего потолка. Грабля с ingress (пустой 413 от
прокси до приложения, см. CLAUDE.md iOS-репо) здесь не бьёт: клиент сам
режет отчёт под тот же потолок (выкидывает `callStackTree`), прежде чем
отправить.

**Частота** — в памяти процесса, по адресу клиента, как у
`review_demo`/`mcp_oauth.RegisterRateLimitMiddleware`: грубый предохранитель
от флуда анонимной ручкой, рестарт переживать не обязан. `RATE_LIMIT_PER_IP`
на окно с запасом покрывает честную пачку (MetricKit — раз в сутки, в пачке
редко больше десятка), 429 клиент понимает как «дошлю в следующий запуск».

Посмотреть — админская `/crashes` (handlers/admin.py) или SQL в
db.py рядом с `log_diagnostic`.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import api_v1_common as common
import db
import review_demo

ApiError = common.ApiError

MAX_BODY_BYTES = 256 * 1024

KINDS = frozenset({"crash", "hang", "cpu_exception", "disk_write_exception"})

RATE_WINDOW_SECONDS = 3600
RATE_LIMIT_PER_IP = 60
_hits: dict[str, list[float]] = {}
_HITS_PRUNE_AT = 1024

# Короткие строки метаданных — подрезаем, чтобы мусор в поле не раздувал
# сводку; payload не трогаем, его размер уже ограничен потолком тела.
_MAX_META_LEN = 64


def reset_rate_limit() -> None:
    """Для тестов: счётчик живёт в памяти процесса."""
    _hits.clear()


def _allow(ip: str) -> bool:
    now = time.monotonic()
    start = now - RATE_WINDOW_SECONDS
    hits = [t for t in _hits.get(ip, []) if t > start]
    if len(hits) >= RATE_LIMIT_PER_IP:
        _hits[ip] = hits
        return False
    hits.append(now)
    _hits[ip] = hits
    if len(_hits) > _HITS_PRUNE_AT:
        for key in list(_hits):
            if not any(t > start for t in _hits[key]):
                del _hits[key]
    return True


async def _optional_user_id(request: Request) -> Optional[int]:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[len("bearer "):].strip()
    if not token:
        return None
    return await db.resolve_api_token(token)


def _meta(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:_MAX_META_LEN] or None


async def submit_diagnostic(request: Request) -> JSONResponse:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "diagnostic body too large")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(400, "bad_request", "invalid JSON body") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", "JSON object expected")
    kind = body.get("kind")
    if kind not in KINDS:
        raise ApiError(400, "bad_request", f"kind must be one of {sorted(KINDS)}")
    payload = body.get("payload")
    if not isinstance(payload, dict) or not payload:
        raise ApiError(400, "bad_request", "payload must be a non-empty JSON object")

    if not _allow(review_demo.client_ip(request)):
        raise ApiError(429, "rate_limited", "too many diagnostics, try again later")

    user_id = await _optional_user_id(request)
    meta = payload.get("diagnosticMetaData")
    meta = meta if isinstance(meta, dict) else {}
    await db.log_diagnostic(
        user_id=user_id,
        kind=kind,
        payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        app_version=_meta(meta.get("appVersion")) or _meta(body.get("app_version")),
        build=_meta(meta.get("appBuildVersion")) or _meta(body.get("build")),
        os_version=_meta(meta.get("osVersion")) or _meta(body.get("os_version")),
        device=_meta(meta.get("deviceType")) or _meta(body.get("device")),
    )
    return JSONResponse({"stored": True}, status_code=201)


routes = [
    Route("/diagnostics", submit_diagnostic, methods=["POST"]),
]
