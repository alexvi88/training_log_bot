"""REST `/v1/mcp/*` — подключение дневника к Claude и ChatGPT из приложения.

То же, что экран /mcp в боте (handlers/mcp_access.py), для тех, у кого бота
нет вовсе: аккаунт, заведённый через Apple, иначе не получил бы ни кода для
страницы согласия, ни способа отключить приложение, которому однажды дал
доступ (а политика конфиденциальности обещает, что отозвать можно).

* `POST /mcp/link-code` — код для страницы согласия (`mcp_oauth.link_code`,
  тот же слот кода, срок и лимиты, что у бота) и канонический адрес
  коннектора: приложение показывает его как есть, а не собирает из своего
  адреса сервера — коннектор живёт на своём домене (config.MCP_PUBLIC_URL),
  а приложение ходит на fly.dev.
* `GET /mcp/apps` — подключённые приложения (`db.list_oauth_connections`).
* `DELETE /mcp/apps/{client_id}` — отключить одно: гасит все его токены у
  этого атлета (`db.revoke_oauth_client_tokens`). Чужой client_id сюда не
  подставить — сверяемся со списком самого атлета. Повтор — не ошибка.

Текста здесь нет: имя приложения пишет тот, кто регистрировался, а запасное
имя (`oauth.client_fallback_name`) берётся из каталога на языке атлета —
язык запроса выставляет `authed_user_id`.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.routing import Route

import api_v1_common as common
import config
import db
import mcp_oauth
from api_v1_common import JSONResponse

ApiError = common.ApiError

logger = logging.getLogger(__name__)


def _require_mcp() -> None:
    # Без публичного адреса MCP-сервер не поднят, и код вёл бы в никуда.
    if not config.mcp_available():
        raise ApiError(503, "not_configured", "mcp is not enabled on this server")


async def request_link_code(request: Request) -> JSONResponse:
    """Код всегда новый (`force_new`): человек нажал кнопку прямо сейчас и
    сразу понесёт код в браузер. Новый гасит прежний — у атлета живёт один."""
    user_id = await common.authed_user_id(request)
    _require_mcp()
    code = await mcp_oauth.link_code(user_id, force_new=True)
    return JSONResponse(
        {
            "code": code,
            "ttl_minutes": mcp_oauth.LINK_CODE_TTL_MINUTES,
            "server_url": mcp_oauth.connector_url(),
        }
    )


def _app_json(row: Any) -> dict[str, Any]:
    return {
        "client_id": row["client_id"],
        "name": mcp_oauth.client_display_name(row["metadata"], row["client_id"]),
        "connected_at": row["connected_at"],
        "last_used_at": row["last_used_at"],
    }


async def _apps_payload(user_id: int) -> dict[str, Any]:
    rows = await db.list_oauth_connections(user_id)
    return {"server_url": mcp_oauth.connector_url(), "apps": [_app_json(r) for r in rows]}


async def list_apps(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    _require_mcp()
    return JSONResponse(await _apps_payload(user_id))


async def disconnect_app(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    _require_mcp()
    client_id = request.path_params["client_id"]
    own = {r["client_id"] for r in await db.list_oauth_connections(user_id)}
    revoked = 0
    if client_id in own:
        revoked = await db.revoke_oauth_client_tokens(user_id, client_id)
        logger.info("MCP OAuth: user %s disconnected client %s from the app (%s tokens)", user_id, client_id, revoked)
    payload = await _apps_payload(user_id)
    payload["disconnected"] = revoked > 0
    return JSONResponse(payload)


routes = [
    Route("/mcp/link-code", request_link_code, methods=["POST"]),
    Route("/mcp/apps", list_apps, methods=["GET"]),
    Route("/mcp/apps/{client_id}", disconnect_app, methods=["DELETE"]),
]
