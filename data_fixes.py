"""Разовые правки данных, которые идут фоновой задачей после старта бота.

Доступа к шеллу прода нет, а бот выкатывается сам на мерже, поэтому правка,
которой нужен код поверх db (achievement_sync и т.п.), живёт здесь: каждая —
под своим именем, сделанная отмечается строкой в `data_fixes` (db.py) и на
следующих стартах не повторяется. Отметка ставится только когда правка
прошла по всем без единого сбоя; упавшая пишет в лог и повторится на
следующем старте — поэтому каждая правка обязана быть идемпотентной.

Почему не `PRAGMA user_version` (db._run_one_shot_migrations): те миграции
идут внутри init_db, до того как бот начал отвечать, и не могут тянуть модули
поверх db (круговой импорт). Здесь — после старта и не задерживая его.

Пушей и сообщений правки не шлют, событий аналитики не пишут: они только
приводят базу в соответствие с правилом, которое уже починено в коде.
"""

from __future__ import annotations

import logging

import achievement_sync
import db

logger = logging.getLogger(__name__)

# «Марафонец» из POST /v1/workouts/{id}/finish до #882 считался от started_at
# (открытия экрана), а не от первого подхода: 72 минуты подходов при экране,
# открытом за 2.5 часа, давали значок. Код починен; эта правка забирает уже
# выданные зря.
MARATHON_FROM_STARTED_AT = "2026-10-marathon-from-started-at"

# Пустой ответ модели на комментарий к тренировке до этой правки подменялся
# русской заглушкой, и она ложилась в workouts.ai_comment: вместо комментария
# навсегда (повторно его не генерировали), на любом языке атлета. Код больше её
# не пишет (ai_trainer.EmptyComment); правка снимает уже сохранённые — следующий
# показ сгенерирует комментарий заново или покажет кнопку «Разобрать».
EMPTY_AI_COMMENT_STUB = "2026-10-empty-ai-comment-stub"
_EMPTY_AI_COMMENT_STUB_TEXT = "Не получилось сформулировать комментарий, попробуй ещё раз позже."

# Демо App Review и прогона скриншотов не трогаем ни при каких условиях — даже
# если связь с auth_identities потеряется (сама связь проверяется ниже).
_NEVER_TOUCH = frozenset({-1000000000, -1000000001})


async def _skip(user_id: int) -> bool:
    return user_id in _NEVER_TOUCH or await db.is_password_demo_account(user_id)


async def fix_marathon_from_started_at() -> tuple[int, int, int]:
    """Забрать «Марафонца» у тех, чья история его не подтверждает.

    Только у держателей значка и только этот значок: остальные значки
    (и выдача новых) — забота achievement_sync.resync на ближайшей правке.
    Возвращает (проверено, забрано, сбоев)."""
    checked = removed = failed = 0
    for user_id in await db.list_users_with_achievement("marathon"):
        if await _skip(user_id):
            continue
        checked += 1
        try:
            if await achievement_sync.revoke_unearned(user_id, {"marathon"}):
                removed += 1
        except Exception:
            failed += 1
            logger.exception("data_fixes: Марафонец у %s не пересчитан", user_id)
    return checked, removed, failed


async def fix_empty_ai_comment_stub() -> int:
    """Снять сохранённую заглушку пустого комментария. Возвращает, сколько снято."""
    return await db.clear_workout_ai_comments(_EMPTY_AI_COMMENT_STUB_TEXT)


async def _run_marathon() -> int:
    checked, removed, failed = await fix_marathon_from_started_at()
    logger.info(
        "data_fixes: %s — проверено %s, Марафонцев забрано %s, сбоев %s",
        MARATHON_FROM_STARTED_AT, checked, removed, failed,
    )
    return failed


async def _run_empty_ai_comment_stub() -> int:
    cleared = await fix_empty_ai_comment_stub()
    logger.info("data_fixes: %s — заглушек снято %s", EMPTY_AI_COMMENT_STUB, cleared)
    return 0


async def run_pending() -> None:
    """Все ещё не сделанные правки по очереди. Не падает наружу никогда:
    упавшая правка не мешает следующим и повторится на следующем старте."""
    for name, run in (
        (MARATHON_FROM_STARTED_AT, _run_marathon),
        (EMPTY_AI_COMMENT_STUB, _run_empty_ai_comment_stub),
    ):
        try:
            if await db.is_data_fix_done(name):
                continue
            if await run() == 0:
                await db.mark_data_fix_done(name)
        except Exception:
            logger.exception("data_fixes: правка %s упала, повторится на следующем старте", name)
