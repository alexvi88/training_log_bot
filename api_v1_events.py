"""REST `POST /v1/events` — продуктовые события из приложения пачкой.

То, чего сервер сам не видит: открытие приложения и уход в фон (с длиной
сессии), просмотр экрана, тап по пушу. Меняющие запросы сервер пишет сам
(`api_v1_activity` → событие `action`), GET'ы — нет, а тап по баннеру до
сервера не доходит вовсе. Подробно — `product_metrics.py`.

**Тело:** `{"events": [{"name": "screen_view", "props": {"screen": "progress"},
"at": "2026-09-29T11:24:55Z"}, ...]}`, не больше `MAX_EVENTS`. Версия
приложения — заголовком `X-App-Version` (его шлёт каждый запрос).

- Незнакомое имя события пропускается, а не 400: новая сборка с новым
  событием не должна терять всю пачку на старом сервере.
- Свойства — только из белого списка события (`product_metrics.CLIENT_EVENTS`),
  значения — короткие машинные токены; остальное выкидывается.
- `at` — когда событие случилось на телефоне (приложение копит пачку и шлёт
  её позже, в том числе после офлайна). Берётся, если не старше `MAX_AGE_DAYS`
  и не из будущего; иначе — время приёма.

Ответ: `{"stored": N}`. В ленту `/activity` сам приём пачки не пишется
(`api_v1_activity`), иначе каждая пачка была бы там строкой «отправил события».
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from starlette.requests import Request
from starlette.routing import Route

import api_v1_common as common
import db
import product_metrics
import timeutil
from api_v1_common import JSONResponse

ApiError = common.ApiError

MAX_EVENTS = 100
MAX_AGE_DAYS = 7
# Часы телефона спешат — не повод выкидывать время события.
_FUTURE_SKEW = dt.timedelta(minutes=5)


def event_time(value: Any, now: Optional[dt.datetime] = None) -> Optional[str]:
    """ISO-время события по часам сервера (как `db.now_iso` у created_at
    везде) или None — «записать временем приёма»."""
    if not isinstance(value, str):
        return None
    try:
        at = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        return None
    at = at.astimezone().replace(tzinfo=None)
    now = now or timeutil.utc_now()
    if at > now + _FUTURE_SKEW or at < now - dt.timedelta(days=MAX_AGE_DAYS):
        return None
    return min(at, now).isoformat(timespec="seconds")


async def submit_events(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    body = await common.json_body(request)
    events = body.get("events")
    if not isinstance(events, list):
        raise ApiError(400, "bad_request", "events must be a list")
    if len(events) > MAX_EVENTS:
        raise ApiError(400, "bad_request", f"at most {MAX_EVENTS} events per request")
    version = product_metrics.clean_version(request.headers.get("x-app-version"))
    rows = []
    for item in events:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        props = product_metrics.clean_props(name, item.get("props")) if isinstance(name, str) else None
        if props is None:
            continue
        rows.append(
            (
                user_id,
                name,
                product_metrics.props_json(props),
                product_metrics.PLATFORM_IOS,
                version,
                event_time(item.get("at")),
            )
        )
    await db.log_analytics_events(rows)
    return JSONResponse({"stored": len(rows)})


routes = [
    Route("/events", submit_events, methods=["POST"]),
]
