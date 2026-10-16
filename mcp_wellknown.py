"""Файлы под `/.well-known/` для публичных каталогов MCP-коннекторов.

Каталоги (официальный MCP Registry, каталог приложений ChatGPT, Glama)
проверяют, что домен коннектора принадлежит тому, кто его публикует: просят
положить файл с их значением по известному адресу на этом домене. Значения
выдаёт каталог или ключ владельца, поэтому в коде их нет — только переменные
окружения (config.MCP_REGISTRY_AUTH, OPENAI_APPS_CHALLENGE,
GLAMA_MAINTAINER_EMAIL). Не задано — 404, как будто файла нет: выдумать
подтверждение нельзя, а пустая строка вместо него читается каталогом как
«не тот ключ».

Порядок для владельца — docs/MCP_LISTING.md.

Отдельно — две вещи, которые нужны клиентам, а не каталогам:

* `/.well-known/oauth-protected-resource` без пути. SDK вешает метаданные
  ресурса только по адресу RFC 9728 с путём (`…/oauth-protected-resource/mcp`),
  а часть клиентов, не нашедших указателя в 401, пробует сначала корень.
  Отдаём тот же документ, что и SDK, — из тех же настроек.
* `/.well-known/mcp/server-card.json` — карточка сервера: имя, версия,
  адрес, способ входа и список инструментов с аннотациями. Каталоги вроде
  Smithery читают её, когда без входа не могут сами спросить `tools/list`.
  Список инструментов берётся у живого сервера, а не переписывается руками, —
  разъехаться с тем, что сервер на самом деле отдаёт, ему нечем.

Текста для человека здесь нет: всё — машинные файлы для проверялок.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

import config
import mcp_oauth

REGISTRY_AUTH_PATH = "/.well-known/mcp-registry-auth"
OPENAI_CHALLENGE_PATH = "/.well-known/openai-apps-challenge"
GLAMA_PATH = "/.well-known/glama.json"
PROTECTED_RESOURCE_ROOT_PATH = "/.well-known/oauth-protected-resource"
SERVER_CARD_PATH = "/.well-known/mcp/server-card.json"

GLAMA_SCHEMA = "https://glama.ai/mcp/schemas/connector.json"

# Строка доказательства MCP Registry начинается так всегда (и для ed25519, и
# для ecdsap384) — по этому признаку отсекаем опечатку в переменной: отдать
# каталогу мусор хуже, чем 404, — он запомнит «ключ не совпал».
_REGISTRY_PREFIX = "v=MCPv1;"

_CACHE = {"Cache-Control": "public, max-age=300"}


def _not_found() -> Response:
    return PlainTextResponse("not found\n", status_code=404)


async def registry_auth_route(request: Request) -> Response:
    value = config.MCP_REGISTRY_AUTH
    if not value.startswith(_REGISTRY_PREFIX):
        return _not_found()
    return PlainTextResponse(value + "\n", headers=_CACHE)


async def openai_challenge_route(request: Request) -> Response:
    # Ровно токен, без перевода строки и JSON — так просит портал OpenAI.
    value = config.OPENAI_APPS_CHALLENGE
    if not value:
        return _not_found()
    return PlainTextResponse(value, headers=_CACHE)


async def glama_route(request: Request) -> Response:
    email = config.GLAMA_MAINTAINER_EMAIL
    if "@" not in email:
        return _not_found()
    return JSONResponse(
        {"$schema": GLAMA_SCHEMA, "maintainers": [{"email": email}]}, headers=_CACHE
    )


def protected_resource_metadata(resource_path: str) -> dict[str, Any]:
    """Тот же документ RFC 9728, что SDK отдаёт по адресу с путём."""
    settings = mcp_oauth.auth_settings(resource_path)
    return {
        "resource": str(settings.resource_server_url),
        "authorization_servers": [str(settings.issuer_url)],
        "bearer_methods_supported": ["header"],
    }


def server_card(server: Any, tools: list[Any], resource_path: str) -> dict[str, Any]:
    site = config.site_base_url()
    card: dict[str, Any] = {
        "serverInfo": {
            "name": server.name,
            "title": getattr(server, "title", None) or server.name,
            "version": getattr(server, "version", None) or "",
        },
        "description": getattr(server, "description", None) or "",
        "transport": {
            "type": "streamable-http",
            "url": f"{mcp_oauth.public_base_url()}{resource_path}",
        },
        "authentication": {
            "required": True,
            "schemes": ["oauth2"],
            "protectedResourceMetadata": (
                f"{mcp_oauth.public_base_url()}{PROTECTED_RESOURCE_ROOT_PATH}{resource_path}"
            ),
        },
        "capabilities": {"tools": {}},
        "tools": [
            tool.model_dump(by_alias=True, exclude_none=True, mode="json") for tool in tools
        ],
    }
    if site:
        card["websiteUrl"] = site
        card["documentationUrl"] = f"{site}/connect"
        card["privacyPolicyUrl"] = f"{site}/privacy"
        card["iconUrl"] = f"{site}/site/icons/icon-512.png"
    return card


def register_routes(server: Any, resource_path: str = "/mcp") -> None:
    server.custom_route(REGISTRY_AUTH_PATH, methods=["GET"])(registry_auth_route)
    server.custom_route(OPENAI_CHALLENGE_PATH, methods=["GET"])(openai_challenge_route)
    server.custom_route(GLAMA_PATH, methods=["GET"])(glama_route)

    async def protected_resource_root(request: Request) -> Response:
        return JSONResponse(protected_resource_metadata(resource_path), headers=_CACHE)

    async def server_card_route(request: Request) -> Response:
        tools = await server.list_tools()
        body = json.dumps(server_card(server, tools, resource_path), ensure_ascii=False)
        return Response(body, media_type="application/json", headers=_CACHE)

    server.custom_route(PROTECTED_RESOURCE_ROOT_PATH, methods=["GET"])(protected_resource_root)
    server.custom_route(SERVER_CARD_PATH, methods=["GET"])(server_card_route)
