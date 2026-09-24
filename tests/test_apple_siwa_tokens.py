"""Отзыв токенов Sign in with Apple при удалении аккаунта (TN3194).

Держим четыре вещи:
1. на входе код авторизации меняется на refresh_token и ложится в
   auth_identities, а client_secret подписан так, как ждёт Apple;
2. при удалении аккаунта этот токен отзывается — до сноса строк;
3. любой сбой Apple (сеть, 4xx/5xx) не срывает ни вход, ни удаление;
4. без APPLE_SIWA_KEY_ID к Apple не ходим вообще.

HTTP к Apple подменён httpx.MockTransport через apple_signin._http_client —
ни один тест не ходит в сеть. Ключ подписи — одноразовый, сгенерированный
прямо в тесте, не настоящий.
"""

from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import account_deletion
import api_v1
import apple_signin
import config

pytestmark = pytest.mark.asyncio

APPLE_SUB = "001234.apple-sub.0001"


def _ephemeral_p8() -> tuple[str, object]:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


def _id_token(sub: str) -> str:
    # Подпись id_token сервер не перепроверяет (см. exchange_authorization_code),
    # поэтому в заглушке — любой ключ.
    return jwt.encode({"sub": sub}, "stub-key-" + "0" * 32, algorithm="HS256")


class AppleStub:
    """Записывает запросы к Apple и отвечает заданным образом."""

    def __init__(self):
        self.requests: list[tuple[str, dict]] = []
        self.token_response: httpx.Response | Exception = httpx.Response(
            200,
            json={
                "access_token": "stub-access",
                "refresh_token": "stub-refresh",
                "id_token": _id_token(APPLE_SUB),
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )
        self.revoke_response: httpx.Response | Exception = httpx.Response(200)

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append((request.url.path, form))
        answer = self.token_response if request.url.path == "/auth/token" else self.revoke_response
        if isinstance(answer, Exception):
            raise answer
        return answer

    def paths(self) -> list[str]:
        return [path for path, _ in self.requests]


@pytest.fixture
def siwa_key(monkeypatch):
    pem, public_key = _ephemeral_p8()
    monkeypatch.setattr(config, "APPLE_SIWA_KEY_ID", "SIWAKEY01")
    monkeypatch.setattr(config, "APPLE_SIWA_PRIVATE_KEY", pem)
    monkeypatch.setattr(config, "APPLE_SIWA_TEAM_ID", "")
    monkeypatch.setattr(config, "APNS_TEAM_ID", "TEAM000001")
    monkeypatch.setattr(config, "APPLE_BUNDLE_ID", "com.trainingdiary.ios")
    return public_key


@pytest.fixture
def apple(monkeypatch):
    stub = AppleStub()
    monkeypatch.setattr(
        apple_signin,
        "_http_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(stub.handler), timeout=timeout),
    )
    return stub


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    monkeypatch.setattr(
        apple_signin,
        "verify_identity_token",
        lambda token: apple_signin.AppleIdentity(apple_user_id=APPLE_SUB, email=None),
    )


async def _stored_tokens(fresh_db):
    cur = await fresh_db.conn().execute(
        "SELECT refresh_token, access_token FROM auth_identities WHERE provider_user_id = ?",
        (APPLE_SUB,),
    )
    row = await cur.fetchone()
    return (row["refresh_token"], row["access_token"]) if row else None


async def _sign_in(client_factory, **extra):
    client = client_factory()
    resp = await client.post("/auth/apple", json={"identity_token": "t", **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()["user_id"]


async def test_sign_in_exchanges_code_and_stores_refresh_token(fresh_db, client_factory, siwa_key, apple):
    await _sign_in(client_factory, authorization_code="one-time-code")

    assert apple.paths() == ["/auth/token"]
    form = apple.requests[0][1]
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "one-time-code"
    assert form["client_id"] == "com.trainingdiary.ios"
    # client_secret — ES256 JWT, как требует Apple: iss = Team ID (взят у
    # APNs — свой не задан), sub = client_id, aud = appleid.apple.com, kid.
    secret = form["client_secret"]
    assert jwt.get_unverified_header(secret) == {"alg": "ES256", "kid": "SIWAKEY01", "typ": "JWT"}
    claims = jwt.decode(secret, siwa_key, algorithms=["ES256"], audience="https://appleid.apple.com")
    assert claims["iss"] == "TEAM000001"
    assert claims["sub"] == "com.trainingdiary.ios"
    assert claims["exp"] > claims["iat"]

    assert await _stored_tokens(fresh_db) == ("stub-refresh", "stub-access")


async def test_apns_key_is_reused_when_key_ids_match(fresh_db, client_factory, monkeypatch, apple):
    """Один ключ на APNs и Sign in with Apple: APPLE_SIWA_KEY_ID = APNS_KEY_ID
    без своего APPLE_SIWA_PRIVATE_KEY — подпись тем же .p8, что у пушей."""
    pem, public_key = _ephemeral_p8()
    monkeypatch.setattr(config, "APNS_KEY_P8", pem)
    monkeypatch.setattr(config, "APNS_KEY_ID", "SHARED0001")
    monkeypatch.setattr(config, "APNS_TEAM_ID", "TEAM000001")
    monkeypatch.setattr(config, "APPLE_SIWA_KEY_ID", "SHARED0001")
    monkeypatch.setattr(config, "APPLE_SIWA_PRIVATE_KEY", "")
    monkeypatch.setattr(config, "APPLE_SIWA_TEAM_ID", "")

    await _sign_in(client_factory, authorization_code="code")

    secret = apple.requests[0][1]["client_secret"]
    jwt.decode(secret, public_key, algorithms=["ES256"], audience="https://appleid.apple.com")
    assert await _stored_tokens(fresh_db) == ("stub-refresh", "stub-access")


async def test_other_key_without_private_key_is_not_configured(monkeypatch):
    monkeypatch.setattr(config, "APNS_KEY_ID", "APNSKEY001")
    monkeypatch.setattr(config, "APNS_KEY_P8", "apns-pem")
    monkeypatch.setattr(config, "APNS_TEAM_ID", "TEAM000001")
    monkeypatch.setattr(config, "APPLE_SIWA_KEY_ID", "OTHERKEY01")
    monkeypatch.setattr(config, "APPLE_SIWA_PRIVATE_KEY", "")
    assert not apple_signin.revocation_configured()


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(400, json={"error": "invalid_grant"}),
        httpx.Response(503),
        httpx.ConnectTimeout("apple is slow"),
    ],
)
async def test_failed_exchange_does_not_break_sign_in(fresh_db, client_factory, siwa_key, apple, failure):
    apple.token_response = failure
    user_id = await _sign_in(client_factory, authorization_code="code")
    assert user_id < 0
    assert await _stored_tokens(fresh_db) == (None, None)


async def test_code_issued_to_another_apple_id_is_not_stored(fresh_db, client_factory, siwa_key, apple):
    apple.token_response = httpx.Response(
        200,
        json={
            "access_token": "a",
            "refresh_token": "r",
            "id_token": _id_token("someone-else"),
        },
    )
    await _sign_in(client_factory, authorization_code="code")
    assert await _stored_tokens(fresh_db) == (None, None)


async def test_sign_in_without_code_does_not_call_apple(fresh_db, client_factory, siwa_key, apple):
    """Старые сборки кода не шлют — вход как раньше, без запроса к Apple."""
    await _sign_in(client_factory)
    assert apple.requests == []


async def test_without_env_nothing_calls_apple(fresh_db, client_factory, monkeypatch, apple):
    monkeypatch.setattr(config, "APPLE_SIWA_KEY_ID", "")
    user_id = await _sign_in(client_factory, authorization_code="code")
    # Токен могли бы положить руками — всё равно отзывать нечем и незачем.
    await fresh_db.set_auth_identity_tokens("apple", APPLE_SUB, "r", None)
    left = await account_deletion.delete_account(user_id)
    assert left == {}
    assert apple.requests == []


async def test_delete_account_revokes_refresh_token_before_wipe(
    fresh_db, client_factory, siwa_key, apple, monkeypatch
):
    user_id = await _sign_in(client_factory, authorization_code="code")

    seen_at_revoke: list = []
    original_handler = apple.handler

    def handler(request):
        if request.url.path == "/auth/revoke":
            # Строка ещё на месте — значит отзыв идёт до сноса.
            seen_at_revoke.append(True)
        return original_handler(request)

    monkeypatch.setattr(
        apple_signin,
        "_http_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout),
    )
    wiped_after_revoke: list[bool] = []
    original_wipe = fresh_db.wipe_user_account

    async def wipe(uid):
        wiped_after_revoke.append(bool(seen_at_revoke))
        await original_wipe(uid)

    monkeypatch.setattr(fresh_db, "wipe_user_account", wipe)

    left = await account_deletion.delete_account(user_id)

    assert left == {}
    assert wiped_after_revoke == [True]
    path, form = apple.requests[-1]
    assert path == "/auth/revoke"
    assert form["token"] == "stub-refresh"
    assert form["token_type_hint"] == "refresh_token"
    assert form["client_id"] == "com.trainingdiary.ios"
    assert await _stored_tokens(fresh_db) is None


async def test_delete_account_falls_back_to_access_token(fresh_db, client_factory, siwa_key, apple):
    user_id = await _sign_in(client_factory)
    await fresh_db.set_auth_identity_tokens("apple", APPLE_SUB, None, "only-access")
    await account_deletion.delete_account(user_id)
    path, form = apple.requests[-1]
    assert path == "/auth/revoke"
    assert form["token"] == "only-access"
    assert form["token_type_hint"] == "access_token"


@pytest.mark.parametrize(
    "failure",
    [httpx.Response(400, json={"error": "invalid_client"}), httpx.ConnectError("no route")],
)
async def test_failed_revoke_does_not_cancel_deletion(fresh_db, client_factory, siwa_key, apple, failure):
    user_id = await _sign_in(client_factory, authorization_code="code")
    apple.revoke_response = failure
    left = await account_deletion.delete_account(user_id)
    assert left == {}
    assert await fresh_db.get_user(user_id) is None


async def test_delete_via_api_revokes_tokens(fresh_db, client_factory, siwa_key, apple):
    client = client_factory()
    resp = await client.post("/auth/apple", json={"identity_token": "t", "authorization_code": "code"})
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    resp = await client.delete("/account?confirm=delete")
    assert resp.status_code == 200, resp.text
    assert apple.paths() == ["/auth/token", "/auth/revoke"]


async def test_account_without_apple_tokens_deletes_without_calls(fresh_db, siwa_key, apple):
    await fresh_db.get_or_create_user(telegram_id=4242, username="tg")
    assert await account_deletion.delete_account(4242) == {}
    assert apple.requests == []
