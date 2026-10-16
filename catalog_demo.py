"""Демо-аккаунт для ревьюеров каталогов AI-коннекторов (Claude, ChatGPT).

Ревьюер каталога подключает `https://<домен>/mcp` и проходит страницу
согласия кодом, а код берёт на `/mcp/demo-code` (mcp_oauth.demo_code_route) по
логину и паролю из config.CATALOG_DEMO_*. Аккаунт — свой, отдельный от демо
App Review (config.REVIEW_DEMO_*): тот — витрина для Apple, и ревьюеры
каталогов не должны ни видеть его, ни писать в него (у коннектора есть
пишущие инструменты). Вход по паролю в приложении (POST /v1/auth/password,
config.demo_accounts) эту пару не принимает.

Личность — та же схема, что у остальных демо-входов (review_demo.py):
auth_identities, провайдер `review_demo`, идентификатор — логин из секрета,
аккаунт — обычный app-only (db.create_app_only_user). Фиксированного id нет
нарочно: синтетические id выдаются подряд вниз от -1000000000, и «зарезервировать»
-1000000002 в живой базе значит отнять его у настоящего атлета, который его уже
получил. Общий провайдер даёт и все отсечки сразу: сайт (db.site_week_numbers),
пуш «Как тебе дневник?» (db.list_feedback_push_candidates), таблицы игр
(api_v1_games) отсекают по провайдеру; квот ai_limits он не держит
(db.is_catalog_demo в ai_limits.quota_exempt), потолки по деньгам — держит.

Заполнение — тот же review_demo.seed_history (12 тренировок за четыре недели и
вес тела), поверх — то, что демо App Review не нужно, а инструментам
коннектора нужно: программа A/B, собранная из этих же тренировок (тренировки
к её дням привязаны — get_program_adherence видит, как по ней ходили), и
профиль тренирующегося. Каждая часть заполняется, только если её ещё нет, — вход
можно повторять сколько угодно. Аккаунты других демо этот модуль не трогает:
он находит свой только по своему логину.
"""

from __future__ import annotations

import json
from typing import Optional

import config
import db
import i18n
import review_demo

PROVIDER = review_demo.PROVIDER


def lang() -> str:
    """Язык аккаунта: CATALOG_DEMO_LANG, по умолчанию английский. Функция, а не
    константа: тесты подменяют config на лету."""
    return i18n.normalize((config.CATALOG_DEMO_LANG or "en").strip().lower())


def check_credentials(username: str, password: str) -> bool:
    """Подошла ли пара к CATALOG_DEMO_*. Сравнение за постоянное время — тот же
    review_demo.check_credentials, только со своим списком пар: пары App Review
    и прогона скриншотов здесь не подходят никогда."""
    if not config.catalog_demo_available():
        return False
    pair = (config.CATALOG_DEMO_USERNAME, config.CATALOG_DEMO_PASSWORD)
    return review_demo.check_credentials(username, password, accounts=[pair]) is not None


async def ensure_user() -> int:
    """user_id демо-аккаунта каталогов: найти по логину или завести и
    заполнить. Под тем же замком, что у review_demo.ensure_demo_user: два
    параллельных входа иначе завели бы два аккаунта или удвоили историю."""
    if not config.catalog_demo_available():
        raise RuntimeError("catalog demo credentials are not configured")
    username = config.CATALOG_DEMO_USERNAME.strip()
    account_lang = lang()
    async with review_demo._get_lock():
        user_id = await db.resolve_auth_identity(PROVIDER, username)
        if user_id is not None and await db.get_user(user_id) is None:
            user_id = None
        if user_id is None:
            user = await db.create_app_only_user(language_code=account_lang, track_signup=False)
            user_id = user["telegram_id"]
            await db.link_auth_identity(user_id, PROVIDER, username)
        else:
            user = await db.get_user(user_id)
            if user["lang"] != account_lang:
                await db.set_user_lang(user_id, account_lang)
        await seed(user_id)
    return user_id


async def seed(user_id: int) -> None:
    """Заполнить то, чего в аккаунте ещё нет: историю (с программой по ней) и
    профиль."""
    with i18n.use_lang(lang()):
        if (
            await db.count_workouts(user_id) == 0
            and await db.count_workouts(user_id, "active") == 0
        ):
            await review_demo.seed_history(user_id)
            await _seed_program(user_id)
        await _seed_profile(user_id)


async def _seed_program(user_id: int) -> Optional[int]:
    """Программа из двух дней, снятая с первых тренировок A и B
    (db.create_routine_from_workout — тот же «Из тренировки», что у атлета), и
    привязка всех тренировок истории к их дням. Есть уже любая программа —
    ничего не заводим: ревьюер мог её переименовать или скопировать."""
    if await db.list_programs(user_id):
        return None
    workouts = await db.list_finished_workouts_meta(user_id)
    if len(workouts) < 2:
        return None
    name = await db.unique_program_name(user_id, i18n.t("catalog_demo.program.name"))
    program_id = await db.create_program(
        user_id, name, description=i18n.t("catalog_demo.program.description")
    )
    if program_id is None:
        return None
    # review_demo.seed_history чередует A и B, начиная с A.
    day_a = await db.create_routine_from_workout(
        user_id, workouts[0]["id"], i18n.t("catalog_demo.program.day_a"), program_id=program_id
    )
    day_b = await db.create_routine_from_workout(
        user_id, workouts[1]["id"], i18n.t("catalog_demo.program.day_b"), program_id=program_id
    )
    for i, workout in enumerate(workouts):
        await db.set_workout_routine(workout["id"], day_a if i % 2 == 0 else day_b)
    return program_id


async def _seed_profile(user_id: int) -> None:
    """Профиль тренирующегося (то, что AI-тренер записал бы со слов атлета).
    Пишется, только если профиль пуст целиком: правку ревьюера не затираем."""
    user = await db.get_user(user_id)
    if any(user[key] for key in ("experience", "goal", "equipment", "limitations")):
        return
    await db.update_user(
        user_id,
        experience=i18n.t("catalog_demo.profile.experience"),
        goal=i18n.t("catalog_demo.profile.goal"),
        equipment=json.dumps(
            [i18n.t("catalog_demo.profile.equipment.barbell"), i18n.t("catalog_demo.profile.equipment.pullup_bar")],
            ensure_ascii=False,
        ),
    )
