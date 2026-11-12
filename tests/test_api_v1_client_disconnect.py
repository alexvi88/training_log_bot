"""Обрыв запроса телефоном (не дослал тело) — не сбой сервера: /v1 отвечает
499 без трейсбэка, а сводка отказов его не считает (прод 12.11: ERROR-трейсбэк
на POST /funnel)."""

import logging

import httpx
import pytest

import api_v1
import ops_alerts


@pytest.fixture
def disconnected_app():
    app = api_v1.build_app()

    async def wrapped(scope, receive, send):
        async def receive_disconnect():
            return {"type": "http.disconnect"}

        await app(scope, receive_disconnect if scope["type"] == "http" else receive, send)

    return wrapped


@pytest.mark.asyncio
async def test_funnel_body_cut_is_499_without_traceback(fresh_db, disconnected_app, caplog):
    transport = httpx.ASGITransport(app=disconnected_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with caplog.at_level(logging.INFO):
            resp = await client.post("/funnel", content=b'{"install_id": "x"}',
                                     headers={"content-type": "application/json"})
    assert resp.status_code == 499
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("client disconnected" in r.getMessage() for r in caplog.records)


def test_499_is_an_expected_failure():
    assert ops_alerts._expected_api_failure("POST", "/funnel", 499)
