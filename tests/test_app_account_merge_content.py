"""Слияние app-only аккаунта с Telegram: что считается «историей», а что нет.

Раньше проверка «у аккаунта есть данные» шла по чёрному списку служебных
таблиц, и любая служебная строка (user_events от первого же запроса к /v1,
push_rotation, счётчик лимита тренера…) делала app-only аккаунт «с историей» —
слияние с telegram-аккаунтом, где тренировки есть, почти всегда отказывало
с both_accounts_have_data. Теперь — белый список db._MERGE_CONTENT_TABLES.
"""

import pytest

import db

pytestmark = pytest.mark.asyncio

# Служебные таблицы — переезжают при слиянии, но не мешают ему. Каждая
# таблица с хозяином обязана быть либо здесь, либо в db._MERGE_CONTENT_TABLES
# (см. test_every_owned_table_is_classified): новая таблица заставит решить,
# история это или нет, а не проскочит молча.
SERVICE_TABLES = {
    "ai_food_usage", "ai_limit_ack", "ai_program_drafts", "ai_question_usage",
    "ai_search_usage", "ai_setup_states", "ai_undo_actions", "ai_video_usage",
    "api_tokens", "auth_identities", "cost_events", "diagnostics", "donations",
    "game_results",
    "mcp_tokens", "oauth_auth_codes", "oauth_link_codes", "oauth_tokens",
    "push_rotation", "push_tokens", "pushes", "set_write_attempts", "user_events",
    # Переписка с поддержкой переезжает целиком (UNIQUE нет), но слиянию не
    # мешает: написать в поддержку до привязки Telegram — не «своя история».
    "support_messages",
}


async def _tg_with_history(telegram_id: int) -> int:
    await db.get_or_create_user(telegram_id, username="tg")
    workout_id = await db.create_finished_workout(
        telegram_id, "2026-01-01T10:00:00", "2026-01-01T11:00:00"
    )
    block_id = await db.create_block(workout_id, "single")
    exercise_id = await db.create_exercise(telegram_id, "Жим", None)
    await db.add_block_exercise(block_id, exercise_id, 0)
    await db.append_set(block_id, exercise_id, 0, 100.0, 5, None)
    return workout_id


async def _service_rows(user_id: int, *, device: str) -> None:
    """То, что app-only аккаунт набирает, просто войдя и потыкав экраны."""
    await db.log_user_event(user_id, "api_action", "POST /push/register")
    await db.save_rotation_bag(user_id, "skip_7", [1, 2, 3])
    await db.increment_ai_question_count(user_id)
    await db.set_ai_setup_state(user_id, {"step": "goal"})
    await db.issue_api_token(user_id)
    await db.register_push_token(user_id, "ios", device)


async def _count(table: str, column: str, user_id: int) -> int:
    cur = await db.conn().execute(
        f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (user_id,)
    )
    return (await cur.fetchone())[0]


async def test_every_owned_table_is_classified(fresh_db):
    owned = {t for t, _ in await db._user_scoped_tables()} - {"users"}
    unclassified = owned - db._MERGE_CONTENT_TABLES - SERVICE_TABLES
    assert not unclassified, (
        f"таблицы {sorted(unclassified)} не отнесены ни к истории атлета "
        "(db._MERGE_CONTENT_TABLES), ни к служебным (SERVICE_TABLES здесь)"
    )
    assert not (db._MERGE_CONTENT_TABLES & SERVICE_TABLES)


async def test_service_rows_do_not_block_merge_into_account_with_history(fresh_db):
    telegram_id = 555
    await _tg_with_history(telegram_id)
    # У приёмника тоже есть служебные строки — в том числе с тем же ключом
    # (сегодняшний счётчик вопросов, та же категория ротации пушей, своё
    # состояние AI-диалога): простой UPDATE упал бы на UNIQUE.
    await _service_rows(telegram_id, device="tg-device")

    app = await db.create_app_only_user(language_code="ru")
    app_id = app["telegram_id"]
    await _service_rows(app_id, device="app-device")
    assert not await db._has_content_data(app_id)

    status = await db.link_telegram_to_app_account(app_id, telegram_id)

    assert status == "ok"
    assert await db.get_user(app_id) is None
    # История приёмника на месте.
    assert await _count("workouts", "user_id", telegram_id) == 1
    assert await _count("exercises", "user_id", telegram_id) == 1
    # Служебные строки app-аккаунта переехали или (на конфликте ключа)
    # уступили строке приёмника — но под старым id не осталось ничего.
    for table, column in await db._user_scoped_tables():
        assert await _count(table, column, app_id) == 0, table
    assert await _count("user_events", "telegram_id", telegram_id) == 2
    assert await _count("ai_question_usage", "telegram_id", telegram_id) == 1
    assert await db.get_ai_question_count_today(telegram_id) == 1
    assert await _count("api_tokens", "user_id", telegram_id) == 2


async def test_merge_still_refuses_when_both_sides_have_real_history(fresh_db):
    telegram_id = 555
    await _tg_with_history(telegram_id)
    app = await db.create_app_only_user(language_code="ru")
    app_id = app["telegram_id"]
    await _service_rows(app_id, device="app-device")
    # Одной записи веса тела достаточно: это уже история атлета.
    await db.add_bodyweight_log(app_id, 80.0, "2026-01-02T09:00:00")

    status = await db.link_telegram_to_app_account(app_id, telegram_id)

    assert status == "both_accounts_have_data"
    assert await db.get_user(app_id) is not None
    assert await _count("bodyweight_logs", "telegram_id", app_id) == 1
    assert await _count("user_events", "telegram_id", app_id) == 1
    assert await _count("workouts", "user_id", telegram_id) == 1


@pytest.mark.parametrize("seed", ["workout", "food", "program", "exercise", "ai_turn"])
async def test_each_kind_of_history_counts_as_content(fresh_db, seed):
    app = await db.create_app_only_user(language_code="ru")
    app_id = app["telegram_id"]
    if seed == "workout":
        await db.get_or_create_active_workout(app_id)
    elif seed == "food":
        await db.add_food_entry(app_id, "2026-01-01", "еда", calories=500)
    elif seed == "program":
        await db.create_program(app_id, "Моя программа")
    elif seed == "exercise":
        await db.create_exercise(app_id, "Своё упражнение", None)
    elif seed == "ai_turn":
        await db.conn().execute(
            "INSERT INTO ai_conversation_turns (telegram_id, question, answer, wire_json, "
            "created_at) VALUES (?, 'q', 'a', '[]', '2026-01-01T00:00:00')",
            (app_id,),
        )
        await db.conn().commit()
    assert await db._has_content_data(app_id)
