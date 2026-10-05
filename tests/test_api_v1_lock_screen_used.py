"""Флаг «подход записан с экрана блокировки» в `/v1/settings`.

Односторонний: `true` ставит, `false` молча игнорируется, не-bool — 400.
Флаг живёт в строке users, так что «Удалить аккаунт» уносит его вместе с ней.
"""

import httpx
import pytest

import api_v1


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester", language_code="ru")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def test_default_is_false(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    body = (await client.get("/settings")).json()
    assert body["lock_screen_used"] is False


async def test_patch_true_sets_and_survives_false(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.patch("/settings", json={"lock_screen_used": True})
    assert resp.status_code == 200
    assert resp.json()["lock_screen_used"] is True

    resp = await client.patch("/settings", json={"lock_screen_used": False})
    assert resp.status_code == 200
    assert resp.json()["lock_screen_used"] is True
    assert (await client.get("/settings")).json()["lock_screen_used"] is True
    assert (await fresh_db.get_user(111))["lock_screen_used"] == 1


async def test_other_patch_does_not_touch_flag(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    await client.patch("/settings", json={"lock_screen_used": True})
    resp = await client.patch("/settings", json={"pushes_enabled": False})
    assert resp.json()["lock_screen_used"] is True


async def test_non_bool_is_400(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    for bad in ("yes", 1, None):
        resp = await client.patch("/settings", json={"lock_screen_used": bad})
        assert resp.status_code == 400, bad
    assert (await client.get("/settings")).json()["lock_screen_used"] is False


async def test_account_deletion_takes_the_flag(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    await client.patch("/settings", json={"lock_screen_used": True})
    await fresh_db.wipe_user_account(111)
    assert await fresh_db.get_user(111) is None
