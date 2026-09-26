"""Смена кг↔lb для того, что бот держит в состоянии диалога (FSM), а не в базе.

Веса в базе пересчитывают db.scale_* — но у атлета, который прямо сейчас в
тренировке или в чате с тренером, часть чисел живёт в FSM: кэши открытой
тренировки, неподобранный черновик программы, припаркованный вопрос «555 кг?
да/нет», ждущее подтверждения взвешивание, описания кнопок «↩️ Отменить».
Без пересчёта они продолжают отвечать в той единице, из которой человек
только что ушёл: голое «8» переносит 100 как будто это всё ещё кг.

Раньше это жило в handlers/settings.py и звалось только из кнопки бота.
Смена единиц из приложения (PATCH /v1/settings) той же тренировкой в том же
аккаунте FSM не трогала вовсе — поэтому общий модуль: бот передаёт свой
FSMContext, REST-слой — только user_id, а хранилище берёт то же, единственное,
что main.py отдал account_deletion (второй JSONFileStorage поверх того же
файла затёр бы чужие состояния — см. комментарий там).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import db

logger = logging.getLogger(__name__)


def weight_cache_updates(data: dict, factor: float) -> dict:
    """Что поменять в данных FSM после пересчёта весов в базе на `factor`.

    Чистая функция: возвращает только изменившиеся ключи (пустой словарь —
    трогать нечего), сами `data` не меняет, кроме вложенных объектов, которые
    всё равно уезжают в ответ целиком.
    """
    updates: dict = {}

    # Кэши открытой тренировки: перенос веса для голого «8», подсказка «в
    # прошлый раз», шаг прогрессии и ответ «да, 555 кг».
    last_by = data.get("last_by_exercise")
    if last_by:
        updates["last_by_exercise"] = {
            ex_id: (weight * factor, reps) for ex_id, (weight, reps) in last_by.items()
        }

    last_session_sets = data.get("last_session_sets")
    if last_session_sets:
        updates["last_session_sets"] = {
            ex_id: [(weight * factor, reps, rpe) for weight, reps, rpe in sets]
            for ex_id, sets in last_session_sets.items()
        }

    for key in ("weight_steps", "confirmed_weights"):
        values = data.get(key)
        if values:
            updates[key] = {ex_id: value * factor for ex_id, value in values.items()}

    draft = data.get("ai_program_draft")
    if draft and db.scale_draft_progression_steps(draft, factor):
        updates["ai_program_draft"] = draft

    # Подходы, припаркованные до ответа на «555 кг? да/нет» (handlers/workout.
    # _ask_weight_confirmation): «да» после смены единиц записало бы их в
    # новой единице старым числом.
    pending = data.get("pending_weight_confirm")
    if isinstance(pending, dict) and pending.get("sets"):
        updates["pending_weight_confirm"] = {
            **pending,
            "sets": [
                [weight * factor if isinstance(weight, (int, float)) else weight, *rest]
                for weight, *rest in pending["sets"]
            ],
        }

    # Взвешивание, ждущее «да» (handlers/bodyweight), — то же самое.
    bw_pending = data.get("bw_pending_weight")
    if isinstance(bw_pending, (int, float)) and not isinstance(bw_pending, bool):
        updates["bw_pending_weight"] = round(bw_pending * factor, 1)

    # Кнопки «↩️ Отменить» под ответами тренера: откат удалённого взвешивания
    # хранит сам вес (db.scale_ai_undo_weights).
    undo_store = data.get("ai_undo")
    if isinstance(undo_store, dict):
        changed = False
        for undo in undo_store.values():
            changed = db.scale_ai_undo_weights(undo, factor) or changed
        if changed:
            updates["ai_undo"] = undo_store

    return updates


async def rescale_state(state: Any, factor: float) -> None:
    """Пересчитать FSM одного диалога — `state` это FSMContext хендлера."""
    updates = weight_cache_updates(await state.get_data(), factor)
    if updates:
        await state.update_data(**updates)


def _registered_storage() -> Any:
    # Импорт локальный: account_deletion тянет apple_signin и прочее, что
    # этому модулю на импорте не нужно.
    import account_deletion

    return account_deletion._fsm_storage


async def rescale_user_state(user_id: int, factor: float, storage: Optional[Any] = None) -> int:
    """Пересчитать FSM атлета во всех его диалогах с ботом — для смены единиц
    не из апдейта Telegram (PATCH /v1/settings). Возвращает, сколько
    состояний поменяли.

    Никогда не бросает: веса в базе к этому моменту уже пересчитаны, и
    упавший пересчёт кэша не должен превращать успешную смену единиц в 500 —
    худшее, что останется, это кэш старой единицы до конца тренировки, ровно
    как было до этого модуля. Нет хранилища (app-only процесс, тесты) или оно
    не умеет перечислить ключи атлета — пропуск с записью в лог.
    """
    storage = storage if storage is not None else _registered_storage()
    if storage is None:
        return 0
    user_keys = getattr(storage, "user_keys", None)
    if user_keys is None:
        logger.warning(
            "fsm_unit_rescale: storage %s has no user_keys, FSM cache of %s not rescaled",
            type(storage).__name__, user_id,
        )
        return 0
    changed = 0
    try:
        for key in user_keys(user_id):
            updates = weight_cache_updates(await storage.get_data(key), factor)
            if updates:
                await storage.update_data(key, updates)
                changed += 1
    except Exception:
        logger.exception("fsm_unit_rescale: failed to rescale FSM cache of %s", user_id)
    return changed
