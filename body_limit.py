"""Общий потолок на размер тела HTTP-запроса — до того, как оно целиком
попало в память какого-либо обработчика.

Зачем отдельно от MAX_IMAGE_BYTES/MAX_VIDEO_BYTES/MAX_VOICE_BYTES/
MAX_CSV_BYTES (config.py): те лимиты — про содержимое конкретного вложения
внутри уже разобранного JSON-тела, и раньше проверялись ПОСЛЕ того, как всё
тело (и весь base64 внутри него) целиком легло в память процесса —
`Request.json()`/`Request.body()` у Starlette читают тело целиком без
всякого потолка сами. Ни uvicorn, ни Starlette не ставят потолок на тело по
умолчанию, а `amvera.yaml` тоже не про это — и на этом порту, в одном
процессе, живёт и бот (Telegram polling), и весь `/v1` (см. докстринг
amvera.yaml и mcp_server.py): одно достаточно большое тело способно уронить
всё сразу, не одну ручку.

Middleware — сырой ASGI, а не `starlette.middleware.base.BaseHTTPMiddleware`:
тому тоже приходится читать `receive()` в цикле, но именно ЭТО и должно
случиться под нашим контролем, до того, как тело дойдёт до обработчика —
никакого специального перехвата через BaseHTTPMiddleware для этого не нужно,
а раз он оборачивает `receive`, свой ASGI-код — тот же объём кода без лишней
прослойки.

Content-Length заголовку доверять нельзя (он клиентский и необязателен) —
это только быстрая отсечка для явно завравшихся значений. Настоящая защита —
считать байты `receive()` по факту, по мере их прихода, и остановиться, как
только накопленный размер превысит потолок, до того как тело целиком
попадёт в приложение (см. докстринг `MaxBodySizeMiddleware.__call__`).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import db
import i18n

logger = logging.getLogger(__name__)

_TOO_LARGE_KEY = "api.error.payload_too_large"
_TOO_LARGE_DETAIL = "request body too large"


def _too_large_body(lang: str) -> bytes:
    """Та же форма, что у остальных ошибок /v1 (api_v1_common.api_error_handler):
    `message` — человеческий текст на языке атлета (его приложение показывает
    как есть), машинный английский — в `detail`. Раньше в `message` лежал
    голый английский «request body too large», и русскоязычный видел его
    прямо на экране."""
    return json.dumps(
        {
            "error": "payload_too_large",
            "message": i18n.t_in(lang, _TOO_LARGE_KEY),
            "detail": _TOO_LARGE_DETAIL,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _header(scope: dict[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


async def _request_lang(scope: dict[str, Any]) -> str:
    """Язык ответа до всякого обработчика: из users.lang владельца токена, как
    у api_v1_common.authed_user_id, иначе — из Accept-Language (вход, /mcp).

    Любой сбой разбора токена — не повод не ответить 413: тогда просто
    заголовок языка."""
    auth = _header(scope, b"authorization") or ""
    if auth.lower().startswith("bearer "):
        try:
            user_id = await db.resolve_api_token(auth[len("bearer "):].strip())
            user = await db.get_user(user_id) if user_id is not None else None
            if user is not None and user["lang"] in i18n.SUPPORTED:
                return user["lang"]
        except Exception:
            logger.debug("body limit: could not resolve token language", exc_info=True)
    return i18n.lang_from_accept_language(_header(scope, b"accept-language"))


class MaxBodySizeMiddleware:
    """Отвергает запрос, чьё тело превышает `max_bytes`, до того как оно
    целиком дойдёт до приложения — 413 с честным JSON, а не обрыв соединения
    или зависание процесса на декодировании гигантского base64."""

    def __init__(self, app, max_bytes: int):
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive, send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        # Быстрая отсечка по заголовку — экономит одно чтение первого чанка
        # у запроса, который сам, честно, объявляет себя слишком большим.
        # Заголовку верить нельзя (см. докстринг модуля), поэтому отсутствие
        # или маленькое значение здесь НИЧЕГО не доказывает и не пропускает
        # проверку ниже — она идёт в любом случае.
        content_length = _content_length(scope)
        if content_length is not None and content_length > self._max_bytes:
            await _reject(scope, send)
            return

        buffered: list[dict[str, Any]] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # Отключение клиента или что-то ещё из lifecycle — прокидываем
                # как есть дальше, разбирать это не наша забота.
                buffered.append(message)
                break
            total += len(message.get("body") or b"")
            buffered.append(message)
            if total > self._max_bytes:
                await _reject(scope, send)
                return
            if not message.get("more_body", False):
                break

        # Тело уже целиком у нас в памяти (иначе не смогли бы досчитать
        # честную сумму байт) и не больше max_bytes — проигрываем те же
        # сообщения приложению, как будто оно само их получило от receive().
        index = 0

        async def replay_receive() -> dict[str, Any]:
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return await receive()

        await self._app(scope, replay_receive, send)


def _content_length(scope: dict[str, Any]) -> int | None:
    for name, value in scope.get("headers") or ():
        if name.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _reject(scope: dict[str, Any], send) -> None:
    body = _too_large_body(await _request_lang(scope))
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
