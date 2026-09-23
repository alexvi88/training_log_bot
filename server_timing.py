"""Сколько сервер думал над запросом `/v1` — заголовком ответа и строкой в лог.

Зачем. «Приложение тормозит» раньше нечем было разложить на части: сеть,
сервер или сам телефон. Заголовок `Server-Timing: app;dur=<мс>` виден и в
URLSession-метриках на телефоне, и в любом HTTP-клиенте — разница между ним и
полным временем запроса и есть сеть. А медленный запрос (дольше
SLOW_REQUEST_MS) пишется в журнал сервиса сам, без телефона в руках: метод,
ШАБЛОН маршрута (`/workouts/{workout_id}` — не путь с id), код и время.

Без персональных данных: ни токена, ни тела запроса или ответа, ни query-строки
— только то, что перечислено выше.

Сырой ASGI, а не BaseHTTPMiddleware: заголовок надо дописать в
`http.response.start`, до того как он ушёл клиенту, а BaseHTTPMiddleware
вдобавок гоняет приложение отдельной задачей — лишняя прослойка на каждый
запрос ради одного заголовка.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Порог «медленного» запроса. Экран приложения собирается из нескольких
# запросов, и 300 мс на один — уже заметная на глаз задержка.
SLOW_REQUEST_MS = 300.0

_CONVERTER = re.compile(r"{(\w+):[^}]+}")


class ServerTimingMiddleware:
    def __init__(self, app, routes: Iterable[Any] = ()):
        self.app = app
        # Шаблон маршрута по обработчику — тот же приём, что у
        # api_v1_activity.LogApiActions: роутер кладёт обработчик в
        # scope["endpoint"], и после ответа по нему находится шаблон пути.
        self._templates = {
            route.endpoint: _CONVERTER.sub(r"{\1}", route.path)
            for route in routes
            if getattr(route, "endpoint", None) is not None
        }

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = {"code": 500}

        async def send_with_timing(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                dur_ms = (time.perf_counter() - started) * 1000
                headers = list(message.get("headers", []))
                headers.append((b"server-timing", f"app;dur={dur_ms:.1f}".encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_timing)
        finally:
            total_ms = (time.perf_counter() - started) * 1000
            if total_ms >= SLOW_REQUEST_MS:
                logger.warning(
                    "[api-slow] %s %s | %s | %.0f ms",
                    scope.get("method", "?"),
                    self._template(scope),
                    status["code"],
                    total_ms,
                )

    def _template(self, scope) -> str:
        template = self._templates.get(scope.get("endpoint"))
        if template is not None:
            return template
        # Маршрут не нашёлся (404) — путь как есть, без query-строки: в ней
        # могла бы оказаться что угодно, а в пути /v1 только id.
        return scope.get("path", "?")
