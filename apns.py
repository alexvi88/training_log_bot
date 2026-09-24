"""Отправка push-уведомлений на iOS через Apple Push Notification service
(APNs), HTTP/2 provider API.

Provider-токен — JWT ES256, подписанный приватным ключом .p8 (тот же приём,
что верификация Sign In with Apple в apple_signin.py, только тут МЫ
подписываем, а не проверяем чужую подпись). Apple разрешает токену жить до
часа и просит не перевыпускать его чаще, чем раз в 20 минут — кэшируем и
переиспользуем, а не подписываем на каждый пуш (см. _provider_token).

HTTP/2 обязателен для APNs provider API (HTTP/1.1 туда не пускают) — у httpx
он есть только если в окружении стоит пакет `h2` (`httpx[http2]`). В
requirements.txt проекта его нет, и эта функция НЕ добавляет его тихо: без
`h2` httpx.AsyncClient(http2=True) бросает ImportError уже на создании
клиента, и это ловится один раз (_get_client), после чего is_configured()
продолжает отвечать по конфигу как обычно, а send_alert просто ничего не
отправляет и не роняет вызывающего — пуш не критичный путь. См. отчёт по
задаче и requirements.txt: пакет `h2` нужно поставить отдельно, прежде чем
APNs реально заработает в этом окружении.

Ответы Apple обязаны разбираться, а не игнорироваться: 410 Unregistered и
400 BadDeviceToken значат, что токен мёртв навсегда (переустановка, логаут,
удаление приложения) — раз получив такой ответ, слать на этот токен снова
бессмысленно и вредно (это раздражает провайдерское соединение Apple), so
токен удаляется из db.push_tokens через db.unregister_push_token_if_current
— и только если это всё ещё тот самый токен, а не свежий, успевший
зарегистрироваться, пока запрос был в полёте. Любая другая ошибка — залогировать (без
секретов: ни .p8, ни JWT, ни сам device token в лог не идут) и вернуть
False, не поднимая исключение — этот путь никогда не должен уронить
engagement.py.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx
import jwt

import config
import db

logger = logging.getLogger(__name__)

# Apple допускает provider-токен до 60 минут и просит не перевыпускать чаще,
# чем раз в 20 минут. 55 — с запасом на то, что между "токен ещё годен" и
# "успел отправить запрос" проходит какое-то время; не 60 ровно, чтобы не
# отправить пуш с токеном, который Apple уже считает истёкшим.
_PROVIDER_TOKEN_TTL_SECONDS = 55 * 60

_provider_token: Optional[str] = None
_provider_token_issued_at: float = 0.0

# Обычный alert-пуш, доставляется немедленно (а не "когда удобно системе",
# как приоритет 5). Все пуши в этом боте — не критичные напоминания, но раз
# уж решили показать баннер, показывать его сразу, а не по расписанию Apple.
APNS_PRIORITY_ALERT = "10"

# 2xx = принят к доставке. Любой другой код — либо мёртвый токен (обрабатывается
# отдельно ниже), либо ошибка, которую в лог и забыть — сеть Apple не входит в
# зону ответственности этого бота.
_DEAD_TOKEN_STATUSES = {400, 410}
_DEAD_TOKEN_REASONS = {"BadDeviceToken", "Unregistered"}

_client: Optional[httpx.AsyncClient] = None
_h2_missing_warned = False


def is_configured() -> bool:
    """Тот же приём, что ai_trainer.is_configured()/video_analysis_available():
    показывать/включать функциональность только когда весь необходимый набор
    секретов реально задан, а не половина."""
    return bool(
        config.APNS_KEY_P8 and config.APNS_KEY_ID and config.APNS_TEAM_ID and config.APNS_BUNDLE_ID
    )


def _host() -> str:
    return (
        "https://api.push.apple.com"
        if config.APNS_ENV == "production"
        else "https://api.sandbox.push.apple.com"
    )


def _provider_token_jwt() -> str:
    """JWT ES256 provider-токена — подписывается заново только когда кэш
    истёк (см. _PROVIDER_TOKEN_TTL_SECONDS), не на каждый вызов."""
    global _provider_token, _provider_token_issued_at
    now = time.time()
    if _provider_token is not None and now - _provider_token_issued_at < _PROVIDER_TOKEN_TTL_SECONDS:
        return _provider_token
    _provider_token = jwt.encode(
        {"iss": config.APNS_TEAM_ID, "iat": int(now)},
        config.APNS_KEY_P8,
        algorithm="ES256",
        headers={"kid": config.APNS_KEY_ID},
    )
    _provider_token_issued_at = now
    return _provider_token


async def _get_client() -> Optional[httpx.AsyncClient]:
    """Клиент на процесс, лениво — APNs держит долгоживущее HTTP/2-соединение,
    а не одно на пуш. None, если `h2` не установлен (см. модульный докстринг):
    предупреждение уходит в лог один раз, а не на каждый пуш."""
    global _client, _h2_missing_warned
    if _client is not None:
        return _client
    try:
        _client = httpx.AsyncClient(http2=True, timeout=10.0)
    except ImportError:
        if not _h2_missing_warned:
            logger.warning(
                "APNs отключён: httpx собран без HTTP/2 (не установлен пакет `h2`), а APNs "
                "provider API принимает только HTTP/2. Поставь `httpx[http2]`/`h2`, чтобы "
                "включить пуши на iOS — без него бот продолжает работать как обычно, просто "
                "без APNs."
            )
            _h2_missing_warned = True
        return None
    return _client


def _reason(response: httpx.Response) -> str:
    try:
        return str(response.json().get("reason", ""))
    except ValueError:
        return ""


async def send_alert(
    user_id: int,
    device_token: str,
    title: str,
    body: str,
    *,
    category: Optional[str] = None,
    route: Optional[dict] = None,
) -> bool:
    """Отправить один alert-пуш. Никогда не бросает исключение — падение
    APNs не должно ронять вызывающий код (тот же контракт, что у
    engagement._deliver на телеграмной стороне, см. её докстринг).

    Возвращает True, если Apple приняла пуш (HTTP 200), иначе False —
    включая случаи "APNs не настроен" и "h2 не установлен", которые тихо
    отключают функциональность, а не считаются ошибкой каждого конкретного
    пуша.

    `category` — категория пуша (push_texts.*), используется как
    apns-collapse-id: второй пуш той же категории тому же человеку схлопывает
    предыдущий непрочитанный баннер вместо того, чтобы копить их в Центре
    уведомлений — та же идея, что db.has_push_today на телеграмной стороне
    (один пуш категории видимым слотом), только на уровне отображения.

    `route` — куда приложение ведёт по тапу на баннер (push_ios.ios_route):
    кладётся ключом `route` рядом с `aps`, вне него — `aps` зарезервирован
    Apple, а свои ключи верхнего уровня приложение получает в `userInfo`
    как есть. None — ключа нет вовсе, и приложение по тапу ведёт себя как
    до маршрутов (просто открывается), так что старые сборки его не
    замечают.
    """
    if not is_configured():
        return False
    client = await _get_client()
    if client is None:
        return False

    headers = {
        "authorization": f"bearer {_provider_token_jwt()}",
        "apns-topic": config.APNS_BUNDLE_ID,
        "apns-push-type": "alert",
        "apns-priority": APNS_PRIORITY_ALERT,
    }
    if category:
        # APNs ограничивает apns-collapse-id 64 байтами — категории в этом
        # проекте короткие ("skip_14", "rank_near"...), но подрезаем на
        # всякий случай, а не полагаемся на то, что так будет всегда.
        headers["apns-collapse-id"] = category[:64]

    payload: dict = {"aps": {"alert": {"title": title, "body": body}}}
    if route:
        payload["route"] = route
    url = f"{_host()}/3/device/{device_token}"

    try:
        response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError:
        logger.exception("APNs: сетевая ошибка при отправке пуша пользователю %s", user_id)
        return False

    if response.status_code == 200:
        return True

    if response.status_code in _DEAD_TOKEN_STATUSES and _reason(response) in _DEAD_TOKEN_REASONS:
        # Только если это всё ещё ТОТ ЖЕ токен: пользователь мог успеть
        # зарегистрировать новый, пока этот запрос был в полёте (см.
        # db.unregister_push_token_if_current), и мёртвый ответ про старый
        # токен не должен стирать свежий, живой.
        await db.unregister_push_token_if_current(user_id, "ios", device_token)
        logger.info(
            "APNs: токен пользователя %s мёртв (%s %s) — удалил из push_tokens",
            user_id, response.status_code, _reason(response),
        )
        return False

    logger.warning(
        "APNs: пуш пользователю %s не доставлен, HTTP %s (%s)",
        user_id, response.status_code, _reason(response),
    )
    return False
