"""Верификация identity token от Sign In with Apple — RS256 JWT, подписанный
Apple, а не нами. Подпись проверяется против публичных ключей Apple (JWKS),
плюс issuer, audience (bundle id приложения) и срок действия — без этого
любой мог бы прислать самодельный токен и выдать себя за чужой Apple ID.

`PyJWKClient` сам кэширует набор ключей в памяти процесса — Apple ротирует их
редко, дёргать https://appleid.apple.com/auth/keys на каждый вход незачем.

Вторая половина модуля — токены Apple и их отзыв (TN3194, «Revoke tokens»):
приложение с Sign in with Apple и удалением аккаунта обязано при удалении
отозвать токены пользователя. Приложение присылает на вход одноразовый
`authorization_code` (живёт 5 минут), сервер меняет его на refresh_token
(`exchange_authorization_code`, POST /auth/token) и хранит в
`auth_identities`; `revoke_user_tokens` при сносе аккаунта отзывает его
(POST /auth/revoke). Оба запроса подписаны client_secret — ES256 JWT ключом
.p8 (apns.sign_es256_jwt, та же подпись, что у APNs). Без APPLE_SIWA_KEY_ID
(config.py) к Apple не ходим вовсе. Ни один сбой Apple не срывает ни вход, ни
удаление: логируем и продолжаем — удаление аккаунта важнее отзыва, а вход не
должен зависеть от необязательного обмена. Ни токены, ни client_secret, ни
код в лог не пишутся.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import jwt
from jwt import PyJWKClient

import apns
import config
import db

logger = logging.getLogger(__name__)

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"
APPLE_REVOKE_URL = "https://appleid.apple.com/auth/revoke"

# Обмен идёт прямо внутри запроса входа — человек ждёт ответа, поэтому
# таймаут короткий: не успел Apple — вход всё равно проходит, без токена.
# Отзыв — внутри удаления аккаунта, там можно подождать чуть дольше.
_EXCHANGE_TIMEOUT_SECONDS = 5.0
_REVOKE_TIMEOUT_SECONDS = 10.0

# Apple разрешает client_secret жить до 6 месяцев; подписываем свежий на
# каждый вызов (вызовы редкие — вход и удаление), так что хватает минут.
_CLIENT_SECRET_TTL_SECONDS = 5 * 60

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


# ---------- токены Apple: обмен кода и отзыв (TN3194) ----------


@dataclass
class _SiwaCredentials:
    team_id: str
    key_id: str
    private_key: str
    client_id: str


def _credentials() -> Optional[_SiwaCredentials]:
    """Настройки обмена/отзыва или None, если сервер для этого не настроен
    (тогда к Apple не ходим вовсе). Ключ — свой APPLE_SIWA_PRIVATE_KEY или,
    если APPLE_SIWA_KEY_ID совпадает с APNS_KEY_ID, тот же .p8, что у APNs
    (на ключе включены обе возможности) — см. config.py."""
    key_id = config.APPLE_SIWA_KEY_ID
    if not key_id:
        return None
    private_key = config.APPLE_SIWA_PRIVATE_KEY
    if not private_key and key_id == config.APNS_KEY_ID:
        private_key = config.APNS_KEY_P8
    team_id = config.APPLE_SIWA_TEAM_ID or config.APNS_TEAM_ID
    client_id = config.APPLE_BUNDLE_ID
    if not (private_key and team_id and client_id):
        logger.warning(
            "Sign in with Apple: задан APPLE_SIWA_KEY_ID, но не хватает ключа, "
            "Team ID или bundle id — обмен и отзыв токенов выключены"
        )
        return None
    return _SiwaCredentials(team_id, key_id, private_key, client_id)


def revocation_configured() -> bool:
    return _credentials() is not None


def _client_secret(creds: _SiwaCredentials) -> str:
    now = int(time.time())
    return apns.sign_es256_jwt(
        {
            "iss": creds.team_id,
            "iat": now,
            "exp": now + _CLIENT_SECRET_TTL_SECONDS,
            "aud": APPLE_ISSUER,
            "sub": creds.client_id,
        },
        creds.private_key,
        creds.key_id,
    )


def _http_client(timeout: float) -> httpx.AsyncClient:
    """Отдельная функция ради тестов: там её подменяют клиентом с
    httpx.MockTransport, и ни один тест не ходит к Apple по-настоящему."""
    return httpx.AsyncClient(timeout=timeout)


def _apple_error(response: httpx.Response) -> str:
    """Машинный код ошибки Apple ("invalid_grant", "invalid_client") — без
    остального тела ответа, в котором секретов нет, но и пользы для лога тоже."""
    try:
        return str(response.json().get("error", ""))
    except ValueError:
        return ""


async def exchange_authorization_code(apple_user_id: str, authorization_code: str) -> bool:
    """Обменять код авторизации на токены и запомнить их у личности
    `apple_user_id` (она уже привязана — зовётся после link/resolve).

    Никогда не бросает: True — токены сохранены, False — не настроено,
    Apple отказал или недоступен (в лог), вход продолжается без них.
    """
    creds = _credentials()
    if creds is None:
        return False
    try:
        async with _http_client(_EXCHANGE_TIMEOUT_SECONDS) as client:
            response = await client.post(
                APPLE_TOKEN_URL,
                data={
                    "client_id": creds.client_id,
                    "client_secret": _client_secret(creds),
                    "code": authorization_code,
                    "grant_type": "authorization_code",
                },
            )
    except Exception:  # noqa: BLE001 — сбой обмена не должен сорвать вход
        logger.warning("Sign in with Apple: обмен кода не удался (сеть/подпись)", exc_info=True)
        return False
    if response.status_code != 200:
        logger.warning(
            "Sign in with Apple: /auth/token ответил HTTP %s (%s)",
            response.status_code, _apple_error(response),
        )
        return False
    try:
        payload = response.json()
    except ValueError:
        logger.warning("Sign in with Apple: /auth/token вернул не JSON")
        return False
    refresh_token = payload.get("refresh_token") or None
    access_token = payload.get("access_token") or None
    if not refresh_token and not access_token:
        logger.warning("Sign in with Apple: в ответе /auth/token нет токенов")
        return False
    # Код прислал клиент, а не Apple: сверяем, что он выпущен тому же Apple
    # ID, что и проверенный identity token, — иначе чужой код лёг бы токенами
    # в эту личность. id_token пришёл от Apple напрямую по TLS в ответ на наш
    # подписанный запрос, поэтому подпись тут не перепроверяем.
    id_token = payload.get("id_token")
    if id_token:
        try:
            sub = jwt.decode(id_token, options={"verify_signature": False}).get("sub")
        except jwt.PyJWTError:
            sub = None
        if sub != apple_user_id:
            logger.warning("Sign in with Apple: код авторизации выпущен другому Apple ID — не сохраняю")
            return False
    await db.set_auth_identity_tokens("apple", apple_user_id, refresh_token, access_token)
    return True


async def _revoke(creds: _SiwaCredentials, token: str, token_type_hint: str) -> bool:
    try:
        async with _http_client(_REVOKE_TIMEOUT_SECONDS) as client:
            response = await client.post(
                APPLE_REVOKE_URL,
                data={
                    "client_id": creds.client_id,
                    "client_secret": _client_secret(creds),
                    "token": token,
                    "token_type_hint": token_type_hint,
                },
            )
    except Exception:  # noqa: BLE001 — сбой отзыва не отменяет удаление
        logger.warning("Sign in with Apple: отзыв токена не удался (сеть/подпись)", exc_info=True)
        return False
    if response.status_code != 200:
        logger.warning(
            "Sign in with Apple: /auth/revoke ответил HTTP %s (%s)",
            response.status_code, _apple_error(response),
        )
        return False
    return True


async def revoke_user_tokens(user_id: int) -> int:
    """Отозвать у Apple токены всех Apple ID этого аккаунта — зовётся из
    account_deletion.delete_account ДО сноса строк (после них токенов уже не
    найти). Предпочитаем refresh_token (отзывает всю связку), иначе
    access_token. Никогда не бросает; возвращает, сколько токенов Apple
    принял к отзыву."""
    creds = _credentials()
    if creds is None:
        return 0
    try:
        rows = await db.auth_identity_tokens_for_user(user_id, "apple")
    except Exception:  # noqa: BLE001
        logger.warning("Sign in with Apple: не прочитал токены аккаунта %s", user_id, exc_info=True)
        return 0
    revoked = 0
    for row in rows:
        if row.get("refresh_token"):
            ok = await _revoke(creds, row["refresh_token"], "refresh_token")
        else:
            ok = await _revoke(creds, row["access_token"], "access_token")
        if ok:
            revoked += 1
        else:
            logger.warning("Sign in with Apple: токен аккаунта %s не отозван — удаление продолжается", user_id)
    return revoked
