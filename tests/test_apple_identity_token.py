"""Проверка identity token Sign in with Apple — настоящими JWT, а не подменой.

Во всех тестах входа (`test_api_v1.py` и др.) `verify_identity_token`
подменяется целиком, так что сама проверка подписи, issuer, audience и срока
не исполнялась ни разу. А это и есть замок входа: пропусти она токен с чужим
audience или алгоритмом `none` — любой зашёл бы под чужим Apple ID. Здесь
токены подписываются своим RSA-ключом, а JWKS-клиент подменяется так, чтобы
отдавать его публичную половину — в сеть к Apple тест не ходит.
"""

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import apple_signin
import config

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeJwks:
    """PyJWKClient, который на любой токен отдаёт наш публичный ключ."""

    def __init__(self, public_key):
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):
        # Как настоящий клиент: битый заголовок — ошибка PyJWT.
        jwt.get_unverified_header(token)
        return SimpleNamespace(key=self._public_key)


@pytest.fixture(autouse=True)
def _jwks(monkeypatch):
    monkeypatch.setattr(apple_signin, "_client", lambda: _FakeJwks(_KEY.public_key()))


def _claims(**overrides):
    now = int(time.time())
    claims = {
        "iss": apple_signin.APPLE_ISSUER,
        "aud": config.APPLE_BUNDLE_ID,
        "sub": "001234.abcdef",
        "email": "a@privaterelay.appleid.com",
        "iat": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _token(claims=None, key=_KEY, algorithm="RS256"):
    return jwt.encode(claims if claims is not None else _claims(), key, algorithm=algorithm)


def test_valid_token_yields_sub_and_email():
    identity = apple_signin.verify_identity_token(_token())
    assert identity.apple_user_id == "001234.abcdef"
    assert identity.email == "a@privaterelay.appleid.com"


def test_email_is_optional():
    # Apple отдаёт email только на самом первом входе.
    identity = apple_signin.verify_identity_token(_token(_claims(email=None)))
    assert identity.email is None


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param(_claims(aud="com.someone.else"), id="foreign-audience"),
        pytest.param(_claims(iss="https://evil.example.com"), id="foreign-issuer"),
        pytest.param(_claims(exp=int(time.time()) - 3600), id="expired"),
        pytest.param(_claims(sub=None), id="no-sub"),
        pytest.param(_claims(sub=""), id="empty-sub"),
    ],
)
def test_rejects_token_with_wrong_claims(claims):
    with pytest.raises(apple_signin.AppleTokenError):
        apple_signin.verify_identity_token(_token(claims))


def test_rejects_token_signed_by_another_key():
    with pytest.raises(apple_signin.AppleTokenError):
        apple_signin.verify_identity_token(_token(key=_OTHER_KEY))


def test_rejects_unsigned_token():
    # alg=none: подпись пустая — классическая подделка JWT.
    with pytest.raises(apple_signin.AppleTokenError):
        apple_signin.verify_identity_token(_token(key=None, algorithm="none"))


def test_rejects_hs256_token_signed_with_the_public_key():
    # Подмена алгоритма: HS256 с публичным ключом в роли общего секрета.
    # PyJWT сам не даст подписать PEM-ключом как HMAC-секретом, поэтому
    # подписываем произвольным секретом — проверка должна упасть на списке
    # разрешённых алгоритмов, ещё до сверки подписи.
    token = jwt.encode(_claims(), "x" * 32, algorithm="HS256")
    with pytest.raises(apple_signin.AppleTokenError):
        apple_signin.verify_identity_token(token)


def test_garbage_string_is_a_token_error_not_a_crash():
    with pytest.raises(apple_signin.AppleTokenError):
        apple_signin.verify_identity_token("not-a-jwt")
