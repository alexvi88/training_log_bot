"""apns.py: provider JWT caching, HTTP/2 send, dead-token cleanup, and the
"never take the bot down" contract.

HTTP is mocked throughout — nothing here makes a real network call. `httpx`
isn't monkeypatched to avoid a real dependency on the `h2` package for most
tests: apns._get_client is patched directly to hand back a fake client
recording calls, except in the one test that specifically exercises the
"h2 not installed" path, which patches httpx.AsyncClient itself.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import apns
import config

pytestmark = pytest.mark.asyncio


class FakeResponse:
    def __init__(self, status_code: int, reason: str | None = None):
        self.status_code = status_code
        self._reason = reason

    def json(self):
        if self._reason is None:
            raise ValueError("no body")
        return {"reason": self._reason}


class FakeClient:
    def __init__(self, response: FakeResponse | Exception):
        self._response = response
        self.post = AsyncMock(side_effect=self._post)
        self.calls: list[dict] = []

    async def _post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """Full APNs config + reset of apns.py's module-level caches (provider
    JWT, HTTP client, h2-missing warning flag) — each test starts clean,
    the way a fresh process would."""
    monkeypatch.setattr(config, "APNS_KEY_P8", "fake-key")
    monkeypatch.setattr(config, "APNS_KEY_ID", "KEYID123")
    monkeypatch.setattr(config, "APNS_TEAM_ID", "TEAMID456")
    monkeypatch.setattr(config, "APNS_BUNDLE_ID", "com.trainingdiary.ios")
    monkeypatch.setattr(config, "APNS_ENV", "sandbox")
    monkeypatch.setattr(apns, "_provider_token", None)
    monkeypatch.setattr(apns, "_provider_token_issued_at", 0.0)
    monkeypatch.setattr(apns, "_client", None)
    monkeypatch.setattr(apns, "_h2_missing_warned", False)
    # "fake-key" isn't a real EC private key, so real jwt.encode(algorithm="ES256")
    # would reject it — tests that don't care about the JWT's actual signing
    # (everything except the "cached, not resigned" tests below) get a stub
    # that just returns a fixed string.
    monkeypatch.setattr(apns.jwt, "encode", MagicMock(return_value="signed.jwt.token"))
    yield


def _patch_client(monkeypatch, response: FakeResponse | Exception) -> FakeClient:
    fake = FakeClient(response)

    async def _get_client():
        return fake

    monkeypatch.setattr(apns, "_get_client", _get_client)
    return fake


async def test_is_configured_requires_every_setting(monkeypatch):
    assert apns.is_configured()
    monkeypatch.setattr(config, "APNS_KEY_ID", "")
    assert not apns.is_configured()


async def test_not_configured_sends_nothing_and_never_touches_the_network(monkeypatch):
    monkeypatch.setattr(config, "APNS_KEY_P8", "")
    get_client = AsyncMock()
    monkeypatch.setattr(apns, "_get_client", get_client)

    ok = await apns.send_alert(1, "devtoken", "Title", "Body")

    assert ok is False
    get_client.assert_not_awaited()


async def test_h2_missing_disables_sending_without_crashing(monkeypatch):
    """httpx.AsyncClient(http2=True) raises ImportError when the `h2` package
    isn't installed (see apns.py's module docstring) — that must turn into
    "APNs quietly does nothing", never an exception the caller has to catch."""
    def _raise_import_error(*args, **kwargs):
        raise ImportError("h2 package is not installed")

    monkeypatch.setattr(apns.httpx, "AsyncClient", _raise_import_error)

    ok = await apns.send_alert(1, "devtoken", "Title", "Body")

    assert ok is False
    assert apns._h2_missing_warned is True


async def test_send_alert_success_returns_true(monkeypatch):
    fake = _patch_client(monkeypatch, FakeResponse(200))

    ok = await apns.send_alert(1, "devtoken", "Title", "Body", category="skip_3")

    assert ok is True
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"].endswith("/3/device/devtoken")
    assert call["json"] == {"aps": {"alert": {"title": "Title", "body": "Body"}}}
    assert call["headers"]["apns-topic"] == "com.trainingdiary.ios"
    assert call["headers"]["apns-push-type"] == "alert"
    assert call["headers"]["apns-collapse-id"] == "skip_3"
    assert call["headers"]["authorization"].startswith("bearer ")


async def test_sandbox_vs_production_host(monkeypatch):
    fake = _patch_client(monkeypatch, FakeResponse(200))
    await apns.send_alert(1, "devtoken", "Title", "Body")
    assert fake.calls[0]["url"].startswith("https://api.sandbox.push.apple.com")

    monkeypatch.setattr(config, "APNS_ENV", "production")
    fake2 = _patch_client(monkeypatch, FakeResponse(200))
    await apns.send_alert(1, "devtoken", "Title", "Body")
    assert fake2.calls[0]["url"].startswith("https://api.push.apple.com")


async def test_provider_jwt_is_cached_not_resigned_per_push(monkeypatch):
    _patch_client(monkeypatch, FakeResponse(200))
    fake_encode = MagicMock(return_value="signed.jwt.token")
    monkeypatch.setattr(apns.jwt, "encode", fake_encode)

    await apns.send_alert(1, "devtoken", "Title", "Body")
    await apns.send_alert(1, "devtoken", "Title", "Body")
    await apns.send_alert(1, "devtoken", "Title", "Body")

    fake_encode.assert_called_once()


async def test_provider_jwt_is_resigned_once_the_cache_expires(monkeypatch):
    _patch_client(monkeypatch, FakeResponse(200))
    fake_encode = MagicMock(side_effect=["first.jwt", "second.jwt"])
    monkeypatch.setattr(apns.jwt, "encode", fake_encode)

    await apns.send_alert(1, "devtoken", "Title", "Body")
    # Простое время назад, чтобы кэш выглядел истёкшим — не ждём реальный час.
    monkeypatch.setattr(apns, "_provider_token_issued_at", 0.0)
    await apns.send_alert(1, "devtoken", "Title", "Body")

    assert fake_encode.call_count == 2


async def test_unregistered_deletes_the_dead_token(monkeypatch, fresh_db, user_id):
    await fresh_db.register_push_token(user_id, "ios", "dead-token")
    _patch_client(monkeypatch, FakeResponse(410, reason="Unregistered"))

    ok = await apns.send_alert(user_id, "dead-token", "Title", "Body")

    assert ok is False
    cur = await fresh_db.conn().execute(
        "SELECT 1 FROM push_tokens WHERE user_id = ? AND platform = 'ios'", (user_id,)
    )
    assert await cur.fetchone() is None


async def test_bad_device_token_deletes_the_dead_token(monkeypatch, fresh_db, user_id):
    await fresh_db.register_push_token(user_id, "ios", "dead-token")
    _patch_client(monkeypatch, FakeResponse(400, reason="BadDeviceToken"))

    ok = await apns.send_alert(user_id, "dead-token", "Title", "Body")

    assert ok is False
    cur = await fresh_db.conn().execute(
        "SELECT 1 FROM push_tokens WHERE user_id = ? AND platform = 'ios'", (user_id,)
    )
    assert await cur.fetchone() is None


async def test_other_400_reason_does_not_delete_the_token(monkeypatch, fresh_db, user_id):
    """400 alone isn't enough — only the specific BadDeviceToken reason means
    the token itself is dead; any other 400 (e.g. a malformed payload on our
    side) must not silently unregister a perfectly good token."""
    await fresh_db.register_push_token(user_id, "ios", "live-token")
    _patch_client(monkeypatch, FakeResponse(400, reason="PayloadTooLarge"))

    ok = await apns.send_alert(user_id, "live-token", "Title", "Body")

    assert ok is False
    cur = await fresh_db.conn().execute(
        "SELECT 1 FROM push_tokens WHERE user_id = ? AND platform = 'ios'", (user_id,)
    )
    assert await cur.fetchone() is not None


async def test_server_error_does_not_raise(monkeypatch):
    _patch_client(monkeypatch, FakeResponse(500))

    ok = await apns.send_alert(1, "devtoken", "Title", "Body")

    assert ok is False


async def test_network_error_does_not_raise(monkeypatch):
    _patch_client(monkeypatch, apns.httpx.ConnectError("boom"))

    ok = await apns.send_alert(1, "devtoken", "Title", "Body")

    assert ok is False


async def test_no_secrets_in_the_payload_or_headers_beyond_the_bearer_token(monkeypatch):
    """Sanity check: the .p8 key material itself never goes anywhere near the
    request — only a short-lived signed JWT does."""
    fake = _patch_client(monkeypatch, FakeResponse(200))
    monkeypatch.setattr(config, "APNS_KEY_P8", "-----BEGIN PRIVATE KEY-----supersecret-----END PRIVATE KEY-----")
    monkeypatch.setattr(apns.jwt, "encode", MagicMock(return_value="signed.jwt.token"))

    await apns.send_alert(1, "devtoken", "Title", "Body")

    call = fake.calls[0]
    assert "supersecret" not in str(call)
    assert call["headers"]["authorization"] == "bearer signed.jwt.token"
