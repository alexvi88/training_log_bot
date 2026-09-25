"""Токены `/v1` — по одному на устройство (db.api_tokens).

Раньше выдача нового токена удаляла все прежние токены человека: вход на
втором устройстве (iPad, новый телефон, переустановка) молча выкидывал из
аккаунта первое. Теперь токенов несколько, `POST /auth/logout` гасит только
свой, удаление аккаунта — все, а сверх db.MAX_API_TOKENS_PER_USER гасятся те,
что дольше всех не ходили.
"""

import httpx
import pytest

import api_v1
import db


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _login(fresh_db, client_factory, telegram_id=111):
    """Вход кодом из бота, как у /auth/link, — отдельный клиент на устройство."""
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client, token


async def _token_count(user_id: int) -> int:
    cur = await db.conn().execute("SELECT COUNT(*) FROM api_tokens WHERE user_id = ?", (user_id,))
    return (await cur.fetchone())[0]


async def test_second_login_keeps_the_first_device_signed_in(fresh_db, client_factory):
    phone, phone_token = await _login(fresh_db, client_factory)
    tablet, tablet_token = await _login(fresh_db, client_factory)
    assert phone_token != tablet_token

    assert (await phone.get("/me")).status_code == 200
    assert (await tablet.get("/me")).status_code == 200
    assert await _token_count(111) == 2


async def test_logout_revokes_only_the_calling_token(fresh_db, client_factory):
    phone, _ = await _login(fresh_db, client_factory)
    tablet, _ = await _login(fresh_db, client_factory)
    other, _ = await _login(fresh_db, client_factory, telegram_id=222)

    resp = await phone.post("/auth/logout")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"logged_out": True}

    assert (await phone.get("/me")).status_code == 401
    assert (await tablet.get("/me")).status_code == 200
    assert (await other.get("/me")).status_code == 200
    # Повторный выход тем же (уже погашенным) токеном — обычный 401.
    assert (await phone.post("/auth/logout")).status_code == 401


async def test_logout_requires_a_token(client_factory):
    resp = await client_factory().post("/auth/logout")
    assert resp.status_code == 401


async def test_account_deletion_revokes_every_token(fresh_db, client_factory):
    phone, _ = await _login(fresh_db, client_factory)
    tablet, _ = await _login(fresh_db, client_factory)
    assert (await tablet.get("/me")).status_code == 200

    resp = await phone.delete("/account?confirm=delete")
    assert resp.status_code < 400, resp.text

    assert (await phone.get("/me")).status_code == 401
    assert (await tablet.get("/me")).status_code == 401
    assert await _token_count(111) == 0


async def test_tokens_are_capped_and_the_stalest_go_first(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "MAX_API_TOKENS_PER_USER", 3)
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    await fresh_db.get_or_create_user(telegram_id=222, username="other")
    other = await db.issue_api_token(222)

    first = await db.issue_api_token(111)
    second = await db.issue_api_token(111)
    third = await db.issue_api_token(111)
    # Первый выдан раньше всех, но им ходят каждый день — вылететь должен
    # второй, к которому никто не возвращался.
    await db.conn().execute(
        "UPDATE api_tokens SET created_at = '2026-01-01T00:00:00', last_used_at = NULL WHERE token = ?",
        (second,),
    )
    await db.conn().execute(
        "UPDATE api_tokens SET created_at = '2025-12-01T00:00:00', last_used_at = '2099-01-01T00:00:00' "
        "WHERE token = ?",
        (first,),
    )
    await db.conn().commit()

    fourth = await db.issue_api_token(111)

    assert await _token_count(111) == 3
    assert await db.resolve_api_token(second) is None
    for token in (first, third, fourth):
        assert await db.resolve_api_token(token) == 111
    # Чужие токены подрезка не трогает.
    assert await db.resolve_api_token(other) == 222


async def test_many_logins_never_exceed_the_cap(fresh_db):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    tokens = [await db.issue_api_token(111) for _ in range(db.MAX_API_TOKENS_PER_USER + 5)]
    assert await _token_count(111) == db.MAX_API_TOKENS_PER_USER
    # Самый свежий вход всегда жив.
    assert await db.resolve_api_token(tokens[-1]) == 111


async def test_merge_keeps_the_telegram_accounts_own_devices(fresh_db):
    """Слияние app-only аккаунта с telegram-аккаунтом раньше гасило токены
    приёмника (UNIQUE не дал бы перенести второй). Теперь UNIQUE нет — оба
    устройства остаются в аккаунте."""
    await fresh_db.get_or_create_user(telegram_id=555, username="tg")
    tg_token = await db.issue_api_token(555)
    app_id = (await db.create_app_only_user())["telegram_id"]
    app_token = await db.issue_api_token(app_id)

    assert await db.link_telegram_to_app_account(app_id, 555) == "ok"

    assert await db.resolve_api_token(tg_token) == 555
    assert await db.resolve_api_token(app_token) == 555
