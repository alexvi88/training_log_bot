"""Верификация identity token от Sign In with Apple — RS256 JWT, подписанный
Apple, а не нами. Подпись проверяется против публичных ключей Apple (JWKS),
плюс issuer, audience (bundle id приложения) и срок действия — без этого
любой мог бы прислать самодельный токен и выдать себя за чужой Apple ID.

`PyJWKClient` сам кэширует набор ключей в памяти процесса — Apple ротирует их
редко, дёргать https://appleid.apple.com/auth/keys на каждый вход незачем.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import jwt
from jwt import PyJWKClient

import config

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"

_jwks_client: Optional[PyJWKClient] = None


def _client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(APPLE_JWKS_URL)
    return _jwks_client


@dataclass
class AppleIdentity:
    apple_user_id: str  # "sub" — стабильный на всю жизнь этого Apple ID + приложения
    email: Optional[str]  # Apple отдаёt его только при самом первом входе


class AppleTokenError(Exception):
    pass


def verify_identity_token(identity_token: str) -> AppleIdentity:
    try:
        signing_key = _client().get_signing_key_from_jwt(identity_token)
        claims: dict[str, Any] = jwt.decode(
            identity_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=config.APPLE_BUNDLE_ID,
            issuer=APPLE_ISSUER,
        )
    except jwt.PyJWTError as exc:
        raise AppleTokenError(str(exc)) from exc
    sub = claims.get("sub")
    if not sub:
        raise AppleTokenError("missing sub claim")
    return AppleIdentity(apple_user_id=sub, email=claims.get("email"))
