"""Cache-Control: no-store на JSON-ответах /v1.

Приложение ходит в API через URLSession с URLCache и политикой
`.useProtocolCachePolicy`. Без заголовков кэша iOS вправе держать ответ и
считать его свежим по эвристике — а «возраст» ответа она меряет часами
телефона. Перевели часы назад, и уже закрытая тренировка из старого ответа
`GET /workouts/active` снова встаёт на экран как идущая (сборка 51,
владелец, часы −10 ч). JSON API кэшировать нельзя вовсе: на каждом GET —
свежие данные. Медиа (картинки, видео, вложения) отдают свои заголовки
кэша — их не трогаем: правим только ответы без Cache-Control с JSON внутри.
"""

from __future__ import annotations


class NoStoreJsonMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {k.lower() for k, _ in headers}
                content_type = next(
                    (v for k, v in headers if k.lower() == b"content-type"), b""
                )
                if b"cache-control" not in names and content_type.startswith(b"application/json"):
                    headers.append((b"cache-control", b"no-store"))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)
