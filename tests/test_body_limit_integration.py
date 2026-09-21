"""`body_limit.MaxBodySizeMiddleware`, подключённый в mcp_server.build_app() —
сквозная проверка, что потолок реально стоит поверх всего приложения (и
`/v1`, и не только), а не только в юнит-тестах самого middleware
(tests/test_body_limit.py).
"""

import config
import mcp_server


async def _asgi_call(app, *, path: str, headers: dict[str, str], body_chunks: list[bytes]):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
    }
    sent: list[dict] = []
    chunks = iter(body_chunks)

    async def receive():
        try:
            chunk = next(chunks)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body


async def test_oversized_body_to_v1_rejected_before_reaching_the_route(monkeypatch):
    """Тело больше потолка, отправленное на реальный маршрут `/v1/factcheck`
    (в API-слое требует авторизации), должно быть отбито мидлварём — не
    дойдя до api_v1 (иначе получили бы 401 «missing bearer token», а не
    413) — и без чтения его целиком."""
    monkeypatch.setattr(config, "MAX_REQUEST_BODY_BYTES", 1000)
    app = mcp_server.build_app()

    huge_chunk = b"x" * 2000
    status, body = await _asgi_call(
        app,
        path="/v1/factcheck",
        headers={"content-type": "application/json"},
        body_chunks=[huge_chunk],
    )
    assert status == 413
    assert b"payload_too_large" in body


async def test_normal_body_to_v1_still_reaches_the_route(monkeypatch):
    """Тело в пределах потолка должно спокойно доехать до api_v1 — 401 от
    отсутствующего токена, а не что-то от мидлвари."""
    monkeypatch.setattr(config, "MAX_REQUEST_BODY_BYTES", 1000)
    app = mcp_server.build_app()

    status, body = await _asgi_call(
        app,
        path="/v1/factcheck",
        headers={"content-type": "application/json"},
        body_chunks=[b'{"text": "post"}'],
    )
    assert status == 401
    assert b"unauthorized" in body
