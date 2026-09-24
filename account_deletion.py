"""Снести аккаунт целиком — одно место для всех, кто это умеет.

Точек входа три и они в разных процессах-мирах: админская кнопка
(`handlers/admin.py`), кнопка самого атлета в настройках
(`handlers/settings.py`) и `DELETE /v1/account` из приложения
(`api_v1_account.py`). Снос — необратимая операция, и три её копии разъехались
бы молча: одна забыла бы кэш, другая — файл состояния, и «снёс целиком»
означало бы разное в зависимости от того, откуда нажали.

Почему это вообще больше одного вызова `db.wipe_user_account`: база — не
единственное место, где живут данные атлета. Незакрытая тренировка и черновик
программы лежат в файле FSM (`fsm_storage.JSONFileStorage.drop_user`), снимки
экранов — в словарях-кэшах хендлеров, доля в суточном расходе — в `ai_limits`.
Пережившее снос возвращалось первым же тапом по висящей в чате кнопке, и
новичок оказывался с чужими черновиками.

Требование Apple 5.1.1(v) — «удалить аккаунт можно из самого приложения» — и
есть причина, по которой у сноса появился второй и третий вызывающий; см.
раздел 7 в `IOS_APP_PLAN.md`.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import ai_limits
import apple_signin
import db

logger = logging.getLogger(__name__)

# Хранилище FSM у бота ровно одно — то, что создано в main.py и отдано
# диспетчеру. REST-слой живёт в том же процессе, но до диспетчера не достаёт,
# а завести себе второй JSONFileStorage поверх того же файла нельзя: он читает
# файл целиком в __init__ и пишет целиком в _save, так что второй инстанс при
# первой же записи затёр бы состояние всех, кто говорит с ботом прямо сейчас.
# Поэтому main.py кладёт сюда тот самый, единственный.
_fsm_storage: Any = None


def set_fsm_storage(storage: Any) -> None:
    """Зовётся один раз из main.py сразу после создания диспетчера."""
    global _fsm_storage
    _fsm_storage = storage


async def _drop_fsm_state(user_id: int, storage: Any) -> None:
    if storage is None:
        # Снос базы уже состоялся, а состояние диалога — нет. Молчать про
        # несделанное нельзя: черновики вернутся, и выглядеть это будет как
        # «снёс не до конца», без единой строки в логе о причине.
        logger.warning(
            "account_deletion: хранилище FSM не зарегистрировано — "
            "состояние диалога %s осталось", user_id,
        )
        return
    drop_user = getattr(storage, "drop_user", None)
    if drop_user is None:
        # MemoryStorage в тестах и любое чужое хранилище без этого метода.
        logger.warning(
            "account_deletion: хранилище %s не умеет drop_user — состояние диалога %s осталось",
            type(storage).__name__, user_id,
        )
        return
    await drop_user(user_id)


def _forget_caches(user_id: int) -> None:
    """Кэши экранов в памяти процесса. Импорт локальный: хендлеры тянут за
    собой aiogram, а этот модуль зовёт и REST-слой."""
    from handlers import history as history_handlers
    from handlers import workout as workout_handlers

    history_handlers._progress_view_cache.pop(user_id, None)
    workout_handlers._heatmap_cache.pop(user_id, None)
    # Суточный расход — общий на всех, но в нём была и доля снесённого; пусть
    # пересчитается по тому, что осталось.
    ai_limits.reset_cache()


async def delete_account(user_id: int, storage: Optional[Any] = None) -> dict[str, int]:
    """Снести аккаунт и всё, что о нём помнят вне базы.

    Возвращает то же, что `db.user_data_left`: таблица → сколько строк
    уцелело. Пустой словарь и есть «снесли целиком» — вызывающий отвечает
    человеку по этому ответу, а не по факту «вызов не упал».

    `storage` — хранилище FSM; None означает «возьми то, что зарегистрировал
    main.py». Передаётся явно там, где оно уже под рукой (у хендлера есть
    `state.storage`), чтобы бот не зависел от порядка инициализации.

    Исключение `db.wipe_user_account` наружу не гасится: снос идёт одной
    транзакцией, упало — значит не снеслось ничего, и вызывающий обязан
    сказать об этом прямо. Проглоченная ошибка выглядела бы для человека ровно
    как успешное удаление.

    Перед сносом — отзыв токенов Sign in with Apple (требование Apple,
    TN3194): после сноса строк auth_identities токенов уже не найти. Отзыв
    никогда не бросает и удаление не отменяет — человек просил удалить
    аккаунт, и недоступный Apple не повод оставить его данные.
    """
    await apple_signin.revoke_user_tokens(user_id)
    await db.wipe_user_account(user_id)
    await _drop_fsm_state(user_id, storage if storage is not None else _fsm_storage)
    _forget_caches(user_id)
    return await db.user_data_left(user_id)
