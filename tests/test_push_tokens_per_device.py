"""push_tokens — строка на устройство, а не на (user_id, platform).

Токены /v1 выдаются на устройство (вход на iPad не разлогинивает iPhone), а
push_tokens держал одну строку на человека: регистрация на iPad перетирала
iPhone, и DELETE /push/register на одном устройстве глушил пуши на другом.
"""

import asyncio
import sqlite3

import httpx
import pytest

import api_v1
import apns
import config
import db
import engagement
import push_texts

pytestmark = pytest.mark.asyncio


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _tokens(user_id: int) -> set[str]:
    return set(await db.get_push_tokens(user_id, "ios"))


async def test_second_device_does_not_overwrite_the_first(fresh_db, user_id):
    await db.register_push_token(user_id, "ios", "iphone")
    await db.register_push_token(user_id, "ios", "ipad")
    # Повторная регистрация того же устройства не плодит строку.
    await db.register_push_token(user_id, "ios", "iphone")

    assert await _tokens(user_id) == {"iphone", "ipad"}
    assert await db.count_push_tokens() == 2


async def test_device_moves_to_the_account_that_registered_it_last(fresh_db):
    await db.register_push_token(1, "ios", "phone")
    await db.register_push_token(1, "ios", "ipad")
    await db.register_push_token(2, "ios", "phone")

    assert await _tokens(1) == {"ipad"}
    assert await _tokens(2) == {"phone"}


async def test_per_user_device_cap_drops_the_stalest(fresh_db, user_id):
    for i in range(db.MAX_PUSH_TOKENS_PER_USER + 2):
        await db.register_push_token(user_id, "ios", f"dev-{i}")

    tokens = await _tokens(user_id)
    assert len(tokens) == db.MAX_PUSH_TOKENS_PER_USER
    assert "dev-0" not in tokens and "dev-1" not in tokens
    assert f"dev-{db.MAX_PUSH_TOKENS_PER_USER + 1}" in tokens


async def test_both_devices_get_the_push(fresh_db, user_id, monkeypatch):
    await db.register_push_token(user_id, "ios", "iphone")
    await db.register_push_token(user_id, "ios", "ipad")
    sent: list[tuple[str, str, str]] = []

    async def fake_send_alert(uid, device_token, title, body, *, category=None, route=None):
        sent.append((device_token, title, body))
        return True

    monkeypatch.setattr(apns, "is_configured", lambda: True)
    monkeypatch.setattr(apns, "send_alert", fake_send_alert)
    decision = engagement.PushDecision(
        push_texts.WEEKLY_DIGEST, "текст", with_cta=False,
        ios_params={"tonnage": "1.2т", "week_count": "3 тренировки"},
    )

    await engagement._send_apns_push(user_id, decision)

    assert {token for token, _, _ in sent} == {"iphone", "ipad"}
    # Один и тот же текст на оба устройства, а не два разных из ротации.
    assert len({(title, body) for _, title, body in sent}) == 1


async def test_dead_token_cleanup_removes_only_that_device(fresh_db, user_id, monkeypatch):
    await db.register_push_token(user_id, "ios", "dead")
    await db.register_push_token(user_id, "ios", "alive")

    class Response:
        status_code = 410

        def json(self):
            return {"reason": "Unregistered"}

    class Client:
        async def post(self, url, json=None, headers=None):
            return Response()

    async def get_client():
        return Client()

    monkeypatch.setattr(config, "APNS_KEY_P8", "fake-key")
    monkeypatch.setattr(config, "APNS_KEY_ID", "KEYID123")
    monkeypatch.setattr(config, "APNS_TEAM_ID", "TEAMID456")
    monkeypatch.setattr(config, "APNS_BUNDLE_ID", "com.trainingdiary.ios")
    monkeypatch.setattr(apns, "_get_client", get_client)
    monkeypatch.setattr(apns, "_provider_token_jwt", lambda: "signed.jwt")

    assert await apns.send_alert(user_id, "dead", "T", "B") is False
    assert await _tokens(user_id) == {"alive"}


async def test_unregister_with_device_token_keeps_the_other_device(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    assert (await client.post("/push/register", json={"device_token": "iphone"})).status_code == 201
    assert (await client.post("/push/register", json={"device_token": "ipad"})).status_code == 201

    resp = await client.request("DELETE", "/push/register", json={"device_token": "ipad"})
    assert resp.status_code == 200, resp.text
    assert await _tokens(111) == {"iphone"}

    resp = await client.delete("/push/register", params={"device_token": "iphone"})
    assert resp.status_code == 200, resp.text
    assert await _tokens(111) == set()


async def test_unregister_cannot_remove_someone_elses_device(fresh_db, client_factory):
    await db.register_push_token(222, "ios", "their-phone")
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.delete("/push/register", params={"device_token": "their-phone"})

    assert resp.status_code == 200
    assert await _tokens(222) == {"their-phone"}


async def test_unregister_without_device_token_keeps_old_behaviour(fresh_db, client_factory):
    # Старые сборки шлют DELETE без тела — снимаются все устройства человека,
    # как раньше снималась единственная строка.
    client = await _linked_client(fresh_db, client_factory)
    await db.register_push_token(111, "ios", "iphone")
    await db.register_push_token(111, "ios", "ipad")

    resp = await client.delete("/push/register")

    assert resp.status_code == 200, resp.text
    assert await _tokens(111) == set()


async def test_unregister_rejects_a_non_string_device_token(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.request("DELETE", "/push/register", json={"device_token": 5})
    assert resp.status_code == 400


async def test_migration_rebuilds_old_table_and_keeps_rows(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE push_tokens (user_id INTEGER NOT NULL, platform TEXT NOT NULL, "
        "device_token TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "PRIMARY KEY (user_id, platform))"
    )
    old.execute("INSERT INTO push_tokens VALUES (5, 'ios', 'phone-5', '2026-01-01', '2026-01-02')")
    old.execute("INSERT INTO push_tokens VALUES (6, 'ios', 'phone-6', '2026-01-01', '2026-01-03')")
    old.commit()
    old.close()

    expected_5 = {"phone-5"}
    for _ in range(2):  # второй запуск — миграция идемпотентна
        db._write_lock = asyncio.Lock()
        await db.init_db(str(path))
        try:
            assert await _tokens(5) == expected_5
            assert await _tokens(6) == {"phone-6"}
            cur = await db.conn().execute("PRAGMA table_info(push_tokens)")
            pk = sorted((r["pk"], r["name"]) for r in await cur.fetchall() if r["pk"])
            assert [name for _, name in pk] == ["platform", "device_token"]
            cur = await db.conn().execute("PRAGMA index_list(push_tokens)")
            assert "idx_push_tokens_user" in {r["name"] for r in await cur.fetchall()}
            # После миграции второе устройство уже не перетирает первое.
            await db.register_push_token(5, "ios", "ipad-5")
            expected_5 = {"phone-5", "ipad-5"}
            assert await _tokens(5) == expected_5
        finally:
            await db.close_db()


async def test_merge_keeps_devices_of_both_accounts(fresh_db):
    await db.get_or_create_user(555, username="tg")
    await db.register_push_token(555, "ios", "tg-ipad")
    app = await db.create_app_only_user(language_code="ru")
    app_id = app["telegram_id"]
    await db.register_push_token(app_id, "ios", "app-iphone")

    assert await db.link_telegram_to_app_account(app_id, 555) == "ok"

    assert await _tokens(555) == {"tg-ipad", "app-iphone"}
