"""HTTP-обвязка публичного сайта (seo_pages.py): один домен, сжатие и 304.

Снаружи всего приложения (mcp_server.build_app), но трогает ТОЛЬКО адреса
сайта — `SITE_PATHS` и статику сайта. /v1, OAuth и /mcp, /.well-known,
/coach/* (Universal Links приглашения тренера), /privacy, /terms, /support,
/connect, /embed, /go — мимо, как были.

1. **301 на свой домен.** Когда задан SITE_BASE_URL (свой домен сайта), адреса
   страниц на другом «нашем» хосте — `www.<домен>` и адрес сервера
   (MCP_PUBLIC_URL / PUBLIC_BASE_URL, сейчас training-log-bot.fly.dev) —
   отвечают 301 на тот же путь на своём домене: поисковик склеивает дубли.
   Только GET/HEAD и только пути из белого списка: `/`, `/ru`, `/en`,
   `/ru/*`, `/en/*`, sitemap.xml, robots.txt, llms.txt, llms-full.txt.
   Любой другой путь на любом хосте не перенаправляется никогда — это держит
   тест на длинном списке служебных адресов.
2. **gzip** для HTML, CSS, JS, SVG, JSON, XML и текста сайта (starlette
   GZipMiddleware; brotli на сервере не стоит). Картинки и шрифты уже сжаты.
3. **ETag и 304.** У файлов статики ETag ставит FileResponse; у страниц и
   картинок превью — слабый ETag по содержимому. Совпал If-None-Match — 304
   без тела. Страницы зависят от устройства (`Vary: User-Agent`), ETag
   считается по тому, что ушло этому клиенту.
"""

from __future__ import annotations

import hashlib
from typing import Optional
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.gzip import GZipMiddleware

import config

SITE_FILES = ("/sitemap.xml", "/robots.txt", "/llms.txt", "/llms-full.txt")
_STATIC_PREFIXES = ("/site/", "/og/")
_STATIC_FILES = ("/favicon.ico", "/apple-touch-icon.png", "/site.webmanifest")


def is_site_path(path: str) -> bool:
    """Страница сайта или его служебный файл — то, что можно перенести на свой домен."""
    if path in ("/", "/ru", "/en") or path in SITE_FILES:
        return True
    return path.startswith("/ru/") or path.startswith("/en/")


def is_site_asset(path: str) -> bool:
    return path in _STATIC_FILES or any(path.startswith(p) for p in _STATIC_PREFIXES)


def _host_of(url: Optional[str]) -> str:
    return (urlsplit(url or "").hostname or "").lower()


def redirect_target(host: str, path: str, query: str, method: str) -> Optional[str]:
    """Куда перенаправить запрос — или None, если не надо."""
    base = config.SITE_BASE_URL if config.SITE_BASE_URL.startswith("https://") else ""
    site_host = _host_of(base)
    if not site_host or method not in ("GET", "HEAD") or not is_site_path(path):
        return None
    host = host.lower().split(":")[0]
    others = {f"www.{site_host}", _host_of(config.MCP_PUBLIC_URL), _host_of(config.PUBLIC_BASE_URL)}
    others.discard("")
    others.discard(site_host)
    if host not in others:
        return None
    return base + path + (f"?{query}" if query else "")


class _ETagResponder:
    """Ставит слабый ETag по содержимому (если его нет) и отвечает 304 на совпадение."""

    def __init__(self, app, if_none_match: Optional[str]):
        self.app = app
        self.if_none_match = if_none_match

    async def __call__(self, scope, receive, send):
        start: Optional[dict] = None
        chunks: list[bytes] = []
        passthrough = False
        swallow = False

        async def wrapped(message):
            nonlocal start, passthrough, swallow
            if swallow:
                return
            if passthrough:
                await send(message)
                return
            if message["type"] == "http.response.start":
                start = message
                headers = Headers(raw=message["headers"])
                etag = headers.get("etag")
                if message["status"] != 200:
                    passthrough = True
                    await send(message)
                    return
                if etag:
                    if self._matches(etag):
                        await self._not_modified(send, headers)
                        swallow = True  # тело FileResponse клиенту уже не нужно
                        return
                    passthrough = True
                    await send(message)
                return
            if message["type"] == "http.response.body":
                if start is None:
                    return
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = b"".join(chunks)
                etag = 'W/"' + hashlib.sha1(body).hexdigest()[:20] + '"'
                headers = MutableHeaders(raw=start["headers"])
                if self._matches(etag):
                    headers["etag"] = etag
                    await self._not_modified(send, headers)
                    return
                headers["etag"] = etag
                await send(start)
                await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, wrapped)

    def _matches(self, etag: str) -> bool:
        if not self.if_none_match:
            return False
        wanted = {t.strip().removeprefix("W/") for t in self.if_none_match.split(",")}
        return etag.removeprefix("W/") in wanted or "*" in wanted

    async def _not_modified(self, send, headers) -> None:
        keep = [(k.encode(), v.encode()) for k, v in headers.items()
                if k.lower() in ("etag", "cache-control", "vary", "content-location", "expires", "last-modified")]
        await send({"type": "http.response.start", "status": 304, "headers": keep})
        await send({"type": "http.response.body", "body": b""})


class SiteMiddleware:
    """301 на свой домен, gzip и ETag/304 — только для адресов сайта."""

    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(self._etag_app, minimum_size=500, compresslevel=6)

    async def _etag_app(self, scope, receive, send):
        headers = Headers(scope=scope)
        await _ETagResponder(self.app, headers.get("if-none-match"))(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        headers = Headers(scope=scope)
        target = redirect_target(headers.get("host", ""), path, scope.get("query_string", b"").decode("latin-1"),
                                 scope.get("method", "GET"))
        if target:
            await send({"type": "http.response.start", "status": 301, "headers": [
                (b"location", target.encode()), (b"cache-control", b"public, max-age=86400"),
                (b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        if scope.get("method") in ("GET", "HEAD") and (is_site_path(path) or is_site_asset(path)):
            await self.gzip(scope, receive, send)
            return
        await self.app(scope, receive, send)
