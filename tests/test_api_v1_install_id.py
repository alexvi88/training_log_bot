"""X-Install-Id: установка приложения склеивается с аккаунтом и подписывает
анонимные отказы в журнале (владелец: «анонимным не присваивается какой-то
идентификатор?»)."""

from __future__ import annotations

import logging
import uuid

import httpx
import pytest

import api_v1
import api_v1_common

pytestmark = pytest.mark.asyncio


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")


@pytest.fixture(autouse=True)
def _reset_seen():
    api_v1_common._INSTALL_SEEN.clear()
    yield
    api_v1_common._INSTALL_SEEN.clear()


async def _install_of(db, user_id):
    cur = await db.conn().execute("SELECT install_id FROM users WHERE telegram_id = ?", (user_id,))
    return (await cur.fetchone())["install_id"]


async def test_authed_request_saves_the_install_and_rewrites_only_on_change(fresh_db):
    await fresh_db.get_or_create_user(111, "a", "en")
    token = await fresh_db.issue_api_token(111)
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    async with _client() as client:
        assert (await client.get("/me", headers={"Authorization": f"Bearer {token}", "X-Install-Id": first})).status_code == 200
        assert await _install_of(fresh_db, 111) == first
        await client.get("/me", headers={"Authorization": f"Bearer {token}", "X-Install-Id": second})
        assert await _install_of(fresh_db, 111) == second
        # Мусор вместо UUID и запрос без заголовка — ничего не трогают.
        await client.get("/me", headers={"Authorization": f"Bearer {token}", "X-Install-Id": "not-a-uuid"})
        await client.get("/me", headers={"Authorization": f"Bearer {token}"})
        assert await _install_of(fresh_db, 111) == second


async def test_anonymous_failure_is_signed_with_the_install(fresh_db, caplog):
    install = str(uuid.uuid4())
    caplog.set_level(logging.WARNING, logger="api_v1_activity")
    async with _client() as client:
        resp = await client.post("/mcp", headers={"X-Install-Id": install})
    assert resp.status_code >= 400
    assert f"anon install={install[:8]}" in caplog.text
