"""`body_limit.MaxBodySizeMiddleware` — потолок на тело HTTP-запроса до того,
как оно целиком доходит до приложения (см. докстринг модуля и разбор аудита
безопасности: раньше потолка не было вовсе ни на одном уровне)."""

import pytest

import body_limit


def _scope(*, content_length: int | None = None) -> dict:
    headers = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    return {"type": "http", "method": "POST", "path": "/v1/factcheck", "headers": headers}


def _make_receive_from_messages(messages: list[dict]):
    it = iter(messages)

    async def receive():
        return next(it)

    return receive


class _RecordingApp:
    def __init__(self):
        self.called = False
        self.received_body = b""

    async def __call__(self, scope, receive, send):
        self.called = True
        while True:
            message = await receive()
            self.received_body += message.get("body") or b""
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _collect_response(app, scope, receive):
    sent = []

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body


@pytest.mark.asyncio
async def test_rejects_by_content_length_header_without_touching_app():
    inner = _RecordingApp()
    middleware = body_limit.MaxBodySizeMiddleware(inner, max_bytes=10)
    scope = _scope(content_length=10_000_000)

    called = False

    async def receive():
        nonlocal called
        called = True
        return {"type": "http.request", "body": b"", "more_body": False}

    status, body = await _collect_response(middleware, scope, receive)
    assert status == 413
    assert b"payload_too_large" in body
    assert inner.called is False
    # Заголовок сам по себе достаточен для быстрой отсечки — читать тело,
    # чтобы отказать, не нужно.
    assert called is False


@pytest.mark.asyncio
async def test_rejects_when_actual_bytes_exceed_limit_even_without_header():
    """Content-Length отсутствует (или врёт в меньшую сторону) — потолок
    всё равно должен сработать по факту прочитанных байт, до того как они
    дойдут до приложения."""
    inner = _RecordingApp()
    middleware = body_limit.MaxBodySizeMiddleware(inner, max_bytes=10)
    scope = _scope(content_length=None)

    messages = [
        {"type": "http.request", "body": b"x" * 6, "more_body": True},
        {"type": "http.request", "body": b"x" * 6, "more_body": False},
    ]
    receive = _make_receive_from_messages(messages)

    status, body = await _collect_response(middleware, scope, receive)
    assert status == 413
    assert b"payload_too_large" in body
    assert inner.called is False


@pytest.mark.asyncio
async def test_allows_body_within_limit_and_replays_it_to_app():
    inner = _RecordingApp()
    middleware = body_limit.MaxBodySizeMiddleware(inner, max_bytes=100)
    scope = _scope(content_length=5)

    messages = [{"type": "http.request", "body": b"hello", "more_body": False}]
    receive = _make_receive_from_messages(messages)

    status, body = await _collect_response(middleware, scope, receive)
    assert status == 200
    assert body == b"ok"
    assert inner.called is True
    assert inner.received_body == b"hello"


@pytest.mark.asyncio
async def test_allows_chunked_body_within_limit():
    inner = _RecordingApp()
    middleware = body_limit.MaxBodySizeMiddleware(inner, max_bytes=100)
    scope = _scope(content_length=None)

    messages = [
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"cd", "more_body": False},
    ]
    receive = _make_receive_from_messages(messages)

    status, body = await _collect_response(middleware, scope, receive)
    assert status == 200
    assert inner.received_body == b"abcd"


@pytest.mark.asyncio
async def test_non_http_scope_passes_through_untouched():
    """lifespan-события не буферизуются — мидлварь не должна вмешиваться в
    старт/остановку приложения."""
    calls = []

    async def inner_app(scope, receive, send):
        calls.append(scope["type"])

    middleware = body_limit.MaxBodySizeMiddleware(inner_app, max_bytes=1)

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        pass

    await middleware({"type": "lifespan"}, receive, send)
    assert calls == ["lifespan"]
