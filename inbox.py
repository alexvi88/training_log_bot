"""«Уведомления» в приложении: каждый пуш человеку с приложением ложится строкой
в `db.inbox` — одним вызовом `deliver`, который заодно и отправляет баннер.

Зачем одно место. Пуши уходят из пяти мест (ежедневные в engagement.py,
анонсы в announcements.py, тренер в coach_notify.py, ответ поддержки в
api_v1_support.py и всё, что придёт следом), и у каждого до этого модуля была
своя копия «APNs настроен? токены есть? на каждое устройство». Запись во
«Уведомления» в каждой копии забылась бы в первой же новой — поэтому здесь
и запись, и баннер: новый пуш, отправленный через `deliver`, попадает в
«Уведомления» сам.

Что ложится:

- **у кого.** Только у человека с приложением (`db.is_app_user`: входил в
  приложение хоть раз, app-only аккаунт или привязанное устройство).
  Кто живёт только в Telegram, видит каждый пуш в чате с ботом — копия в
  разделе, которого у него нет, была бы мёртвым грузом, а при первом входе в
  приложение встречала бы его десятком «непрочитанных» старых напоминаний.
- **когда.** Независимо от доставки: строка пишется ДО баннера, и её не
  отменяют ни выключенные пуши (`push=False` у engagement для тех, кто
  выключил пуши в настройках), ни запрет уведомлений в iOS, ни отсутствие
  токена, ни ненастроенный APNs, ни ответ Apple с ошибкой.
- **сколько.** Одно событие — одна строка, сколько бы каналов его ни везли:
  Telegram и APNs одного пуша — это один вызов `deliver` (Telegram идёт мимо
  этого модуля, своим кодом). Служебный пуш «в поддержку написали» админу сюда
  не идёт — он зовёт `apns.send_alert` напрямую. Проверочный `/testpush` идёт
  сюда нарочно: им проверяют и баннер, и сам раздел.
- **на каком языке.** Готовым текстом на языке пуша (`lang`), а не ключом:
  см. комментарий к таблице inbox в db.py.

Число на иконке (`aps.badge`) — сколько у человека непрочитанных в
«Уведомлениях» вместе с этим пушем; считает сервер в момент отправки.

Не бросает: к этому моменту событие (комментарий, ответ, тренировка) уже
записано, и сбой доставки не должен стать ошибкой у того, кто его вызвал.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import apns
import db

logger = logging.getLogger(__name__)


@dataclass
class Delivery:
    # Строка в «Уведомлениях» или None — человек без приложения или запись
    # сорвалась.
    inbox_id: Optional[int]
    # Сколько устройств Apple принял баннер.
    accepted: int


async def deliver(
    user_id: int,
    category: str,
    title: str,
    body: str,
    *,
    lang: str,
    route: Optional[dict] = None,
    push: bool = True,
) -> Delivery:
    """Записать пуш во «Уведомления» и (при `push`) показать баннер на каждом
    iOS-устройстве человека. `title`/`body` — уже на языке `lang`."""
    inbox_id: Optional[int] = None
    try:
        if await db.is_app_user(user_id):
            inbox_id = await db.add_inbox_item(user_id, category, title, body, route, lang)
    except Exception:
        logger.exception("inbox: failed to store %s for user %s", category, user_id)
    if not push or not apns.is_configured():
        return Delivery(inbox_id, 0)
    accepted = 0
    try:
        tokens = await db.get_push_tokens(user_id, "ios")
        if not tokens:
            return Delivery(inbox_id, 0)
        badge = await db.count_inbox_unread(user_id, lang) if inbox_id is not None else None
        for token in tokens:
            if await apns.send_alert(
                user_id, token, title, body,
                category=category, route=route, inbox_id=inbox_id, badge=badge,
            ):
                accepted += 1
    except Exception:
        logger.exception("inbox: banner %s for user %s failed", category, user_id)
    return Delivery(inbox_id, accepted)
