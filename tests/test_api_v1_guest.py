"""«Начать без аккаунта»: POST /v1/auth/guest и вход гостя с Apple
(api_v1.auth_guest, api_v1._sign_in_guest_with_apple)."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

import acquisition
import ai_limits
import api_v1
import db

pytestmark = pytest.mark.asyncio


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")


def _apple(monkeypatch, apple_user_id: str = "apple-guest"):
    import apple_signin

    monkeypatch.setattr(
        apple_signin, "verify_identity_token",
        lambda token: apple_signin.AppleIdentity(apple_user_id=apple_user_id, email=None),
    )


async def _guest(client: httpx.AsyncClient, **extra) -> dict:
    resp = await client.post("/auth/guest", json={"install_id": str(uuid.uuid4()), **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _signup_props(user_id: int):
    cur = await db.conn().execute(
        "SELECT props, platform FROM analytics_events WHERE user_id = ? AND event = 'signup'", (user_id,)
    )
    row = await cur.fetchone()
    return (json.loads(row[0]) if row[0] else None), row[1]


# --- заведение гостя ------------------------------------------------------------


async def test_guest_gets_the_apple_response_shape_and_a_fresh_account_each_time(fresh_db):
    client = _client()
    install = str(uuid.uuid4())
    first = await client.post("/auth/guest", json={"install_id": install, "lang": "en-US"})
    second = await client.post("/auth/guest", json={"install_id": install, "lang": "en-US"})
    assert first.status_code == second.status_code == 200
    body = first.json()
    assert set(body) == {"token", "user_id", "unit", "lang", "is_guest"}
    assert body["is_guest"] is True and body["lang"] == "en"
    # install_id не секрет: по нему никого не находим, каждый вызов — новый гость.
    assert second.json()["user_id"] != body["user_id"]
    user = await fresh_db.get_user(body["user_id"])
    assert user["is_guest"] == 1 and user["telegram_linked"] == 0 and body["user_id"] < 0
    # install_id нигде не хранится.
    cur = await fresh_db.conn().execute("SELECT COUNT(*) FROM funnel_events")
    assert (await cur.fetchone())[0] == 0

    me = await client.get("/me", headers=_bearer(body["token"]))
    assert me.status_code == 200 and me.json()["is_guest"] is True


async def test_me_is_guest_is_false_for_ordinary_accounts(fresh_db, monkeypatch):
    _apple(monkeypatch)
    client = _client()
    apple = await client.post("/auth/apple", json={"identity_token": "t"})
    assert apple.json()["is_guest"] is False
    me = await client.get("/me", headers=_bearer(apple.json()["token"]))
    assert me.json()["is_guest"] is False


async def test_guest_signup_event_is_marked_and_carries_the_source(fresh_db):
    body = await _guest(_client(), source="src_tiktok")
    props, platform = await _signup_props(body["user_id"])
    assert platform == "ios" and props == {"source": "src_tiktok", "guest": "yes"}
    assert (await fresh_db.get_user(body["user_id"]))["source"] == "src_tiktok"

    plain = await _guest(_client())
    props, _ = await _signup_props(plain["user_id"])
    assert props == {"guest": "yes"}
    # Без метки источник пуст, а не organic — как у /auth/apple.
    assert (await fresh_db.get_user(plain["user_id"]))["source"] is None


async def test_guest_keeps_normal_quotas(fresh_db):
    body = await _guest(_client())
    assert not await ai_limits.quota_exempt(body["user_id"])
    assert body["user_id"] not in await fresh_db.own_account_ids()


@pytest.mark.parametrize("payload", [{}, {"install_id": "nope"}, {"install_id": 5}, [1]])
async def test_guest_rejects_bad_bodies_with_a_human_message(fresh_db, payload):
    resp = await _client().post("/auth/guest", json=payload, headers={"Accept-Language": "en"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request" and resp.json()["message"]


async def test_guest_body_size_is_capped(fresh_db):
    resp = await _client().post(
        "/auth/guest", json={"install_id": str(uuid.uuid4()), "pad": "x" * api_v1.GUEST_MAX_BODY_BYTES},
    )
    assert resp.status_code == 400
    cur = await fresh_db.conn().execute("SELECT COUNT(*) FROM users")
    assert (await cur.fetchone())[0] == 0


async def test_guest_rate_limit_per_address(fresh_db):
    client = _client()
    ip = {"Fly-Client-IP": "203.0.113.7", "Accept-Language": "ru"}
    for _ in range(api_v1.GUEST_LIMIT_PER_IP):
        resp = await client.post("/auth/guest", json={"install_id": str(uuid.uuid4())}, headers=ip)
        assert resp.status_code == 200
    blocked = await client.post("/auth/guest", json={"install_id": str(uuid.uuid4())}, headers=ip)
    assert blocked.status_code == 429
    assert blocked.json()["error"] == "guest_rate_limited"
    assert "Apple" in blocked.json()["message"] and "завтра" in blocked.json()["message"]
    # Другой адрес не задет.
    other = await client.post(
        "/auth/guest", json={"install_id": str(uuid.uuid4())}, headers={"Fly-Client-IP": "203.0.113.8"},
    )
    assert other.status_code == 200
    # Кривые запросы потолок не тратят.
    api_v1.reset_guest_limits()
    for _ in range(api_v1.GUEST_LIMIT_PER_IP + 2):
        await client.post("/auth/guest", json={"install_id": "nope"}, headers=ip)
    assert (await client.post("/auth/guest", json={"install_id": str(uuid.uuid4())}, headers=ip)).status_code == 200


async def test_guest_global_hourly_cap(fresh_db, monkeypatch):
    monkeypatch.setattr(api_v1, "GUEST_LIMIT_TOTAL", 2)
    client = _client()
    codes = []
    for i in range(3):
        resp = await client.post(
            "/auth/guest", json={"install_id": str(uuid.uuid4()), "lang": "en"},
            headers={"Fly-Client-IP": f"198.51.100.{i}"},
        )
        codes.append(resp.status_code)
    assert codes == [200, 200, 503]
    assert resp.json()["error"] == "guest_unavailable" and "Apple" in resp.json()["message"]
    assert resp.headers["retry-after"] == str(api_v1.GUEST_TOTAL_WINDOW_SECONDS)


def test_limit_windows():
    api_v1.reset_guest_limits()
    for _ in range(api_v1.GUEST_LIMIT_PER_IP):
        assert api_v1._guest_limit_hit("a", now=0.0) is None
    assert api_v1._guest_limit_hit("a", now=10.0) == "ip"
    # Сутки спустя адрес снова может.
    assert api_v1._guest_limit_hit("a", now=api_v1.GUEST_IP_WINDOW_SECONDS + 1.0) is None
    api_v1.reset_guest_limits()


# --- гость входит с Apple ---------------------------------------------------------


async def test_guest_links_a_new_apple_id_and_keeps_everything(fresh_db, monkeypatch):
    _apple(monkeypatch, "apple-fresh")
    client = _client()
    guest = await _guest(client)
    workout_id = await fresh_db.create_workout(guest["user_id"])

    resp = await client.post("/auth/apple", json={"identity_token": "t"}, headers=_bearer(guest["token"]))

    assert resp.status_code == 200, resp.text
    assert resp.json()["user_id"] == guest["user_id"] and resp.json()["is_guest"] is False
    assert await fresh_db.resolve_auth_identity("apple", "apple-fresh") == guest["user_id"]
    assert (await fresh_db.get_user(guest["user_id"]))["is_guest"] == 0
    assert await fresh_db.get_workout(workout_id) is not None
    # Прежний токен гостя жив — это тот же аккаунт.
    me = await client.get("/me", headers=_bearer(guest["token"]))
    assert me.status_code == 200 and me.json()["is_guest"] is False
    # Второй аккаунт не заводился.
    cur = await fresh_db.conn().execute("SELECT COUNT(*) FROM users")
    assert (await cur.fetchone())[0] == 1


async def _existing_apple_account(client, monkeypatch, apple_user_id: str = "apple-taken") -> int:
    _apple(monkeypatch, apple_user_id)
    resp = await client.post("/auth/apple", json={"identity_token": "t"})
    assert resp.status_code == 200
    return resp.json()["user_id"]


async def test_empty_guest_signs_in_to_the_existing_account_and_is_deleted(fresh_db, monkeypatch):
    client = _client()
    owner = await _existing_apple_account(client, monkeypatch)
    guest = await _guest(client)

    resp = await client.post("/auth/apple", json={"identity_token": "t"}, headers=_bearer(guest["token"]))

    assert resp.status_code == 200, resp.text
    assert resp.json()["user_id"] == owner and resp.json()["is_guest"] is False
    assert await fresh_db.get_user(guest["user_id"]) is None
    assert (await client.get("/me", headers=_bearer(guest["token"]))).status_code == 401


async def test_guest_with_data_gets_409_then_discard_guest_signs_in(fresh_db, monkeypatch):
    client = _client()
    owner = await _existing_apple_account(client, monkeypatch)
    guest = await _guest(client, lang="en")
    await fresh_db.create_workout(guest["user_id"])

    conflict = await client.post(
        "/auth/apple", json={"identity_token": "t", "lang": "en"}, headers=_bearer(guest["token"]),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "apple_account_exists"
    assert "delete" in conflict.json()["message"]
    # Ничего не тронуто: гость и его тренировка на месте, токен жив.
    assert await fresh_db.user_has_content_data(guest["user_id"])
    assert (await client.get("/me", headers=_bearer(guest["token"]))).status_code == 200

    resp = await client.post(
        "/auth/apple", json={"identity_token": "t", "discard_guest": True}, headers=_bearer(guest["token"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_id"] == owner
    assert await fresh_db.get_user(guest["user_id"]) is None
    assert await fresh_db.user_data_left(guest["user_id"]) == {}


async def test_non_guest_or_stale_bearer_keeps_the_old_apple_flow(fresh_db, monkeypatch):
    client = _client()
    owner = await _existing_apple_account(client, monkeypatch)
    _apple(monkeypatch, "apple-other")
    other = (await client.post("/auth/apple", json={"identity_token": "t"})).json()
    # Токен обычного аккаунта — не гость: обычный вход в свой Apple ID, никого не сносим.
    _apple(monkeypatch, "apple-taken")
    resp = await client.post("/auth/apple", json={"identity_token": "t"}, headers=_bearer(other["token"]))
    assert resp.status_code == 200 and resp.json()["user_id"] == owner
    assert await fresh_db.get_user(other["user_id"]) is not None
    # Протухший токен — тоже обычный вход, а не 401.
    resp = await client.post("/auth/apple", json={"identity_token": "t"}, headers=_bearer("stale"))
    assert resp.status_code == 200 and resp.json()["user_id"] == owner


async def test_attach_apple_to_guest_never_steals_a_taken_apple_id(fresh_db):
    owner = await fresh_db.create_app_only_user("ru")
    await fresh_db.link_auth_identity(owner["telegram_id"], "apple", "apple-race")
    guest = await fresh_db.create_app_only_user("ru", guest=True)
    assert not await fresh_db.attach_apple_to_guest(guest["telegram_id"], "apple-race")
    assert await fresh_db.resolve_auth_identity("apple", "apple-race") == owner["telegram_id"]
    assert (await fresh_db.get_user(guest["telegram_id"]))["is_guest"] == 1
    # Не гость — не привязываем.
    assert not await fresh_db.attach_apple_to_guest(owner["telegram_id"], "apple-new")


async def test_guest_can_delete_the_account(fresh_db):
    client = _client()
    guest = await _guest(client)
    await fresh_db.create_workout(guest["user_id"])
    resp = await client.delete("/account?confirm=delete", headers=_bearer(guest["token"]))
    assert resp.status_code == 200 and resp.json() == {"deleted": True}
    assert await fresh_db.get_user(guest["user_id"]) is None


async def test_guest_linking_telegram_stops_being_a_guest(fresh_db):
    guest = await fresh_db.create_app_only_user("ru", guest=True)
    assert await fresh_db.link_telegram_to_app_account(guest["telegram_id"], 5551, "tg") == "ok"
    assert (await fresh_db.get_user(5551))["is_guest"] == 0


# --- /growth: пустой источник ------------------------------------------------------


async def test_growth_splits_empty_sources_by_where_the_account_was_made(fresh_db):
    await fresh_db.create_app_only_user("ru")  # приложение, без метки
    await fresh_db.create_app_only_user("ru", guest=True)  # гость, без метки
    await fresh_db.get_or_create_user(7001, "bot_no_start")  # бот, /start не было
    await fresh_db.get_or_create_user(7002, "bad_label")
    await fresh_db.set_user_source(7002, acquisition.SOURCE_UNKNOWN)
    await fresh_db.get_or_create_user(7003, "legacy")
    await fresh_db.set_user_source(7003, acquisition.SOURCE_LEGACY)

    rows = {r["source"]: r["users"] for r in await fresh_db.acquisition_funnel(days=30)}
    assert rows == {
        acquisition.SOURCE_APP_NO_LABEL: 2,
        acquisition.SOURCE_BOT_NO_START: 1,
        acquisition.SOURCE_UNKNOWN: 1,
    }
    onboarding = {r["source"]: r["users"] for r in await fresh_db.onboarding_funnel(days=30)}
    assert onboarding == rows

    text = acquisition.format_funnel(await fresh_db.acquisition_funnel(days=30), 30)
    assert "📱 Приложение, без метки — <b>2</b>" in text
    assert "🤖 Бот, без /start — <b>1</b>" in text
    assert "❓ Метка не разобралась — <b>1</b>" in text
