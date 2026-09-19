"""Аккаунт без Telegram: заводится Sign in with Apple в приложении (см.
api_v1.auth_apple), связывается с Telegram позже — тут его самое опасное
место, слияние (db.link_telegram_to_app_account).

Три сценария слияния и их гарантии:
  - телеграм-аккаунта ещё нет — данные app-only переезжают под настоящий id;
  - телеграм-аккаунт есть, но пустой — то же самое, пустая строка исчезает;
  - у обоих есть история — отказ, и НИЧЕГО не тронуто (это и проверяем
    отдельно, а не только код ответа).

Список таблиц для проверки «ничего не забыто» — не выписан руками: тест
заводит по строке в каждой таблице, которую возвращает db._user_scoped_tables
(тот же источник правды, что использует сама merge), и после переноса
проверяет ровно этот список.
"""

import asyncio

import pytest

import db


async def _seed_content(telegram_id: int, *, tag: str) -> None:
    """Одна строка в каждой содержательной таблице — так, чтобы после
    переноса можно было проверить presence по всем сразу, а не по трём
    примерам из ТЗ (тренировки/еда/вес)."""
    workout_id = await db.create_finished_workout(
        telegram_id, "2026-01-01T10:00:00", "2026-01-01T11:00:00"
    )
    block_id = await db.create_block(workout_id, "single")
    exercise_id = await db.create_exercise(telegram_id, f"{tag} exercise", None)
    await db.add_block_exercise(block_id, exercise_id, 0)
    await db.append_set(block_id, exercise_id, 0, 100.0, 5, None)
    await db.add_bodyweight_log(telegram_id, 80.0, "2026-01-01T09:00:00")
    await db.add_food_entry(telegram_id, "2026-01-01", f"{tag} meal", calories=500)
    await db.create_program(telegram_id, f"{tag} program")
    await db.award_achievements(telegram_id, {f"{tag}_ach"})
    await db.issue_api_token(telegram_id)
    await db.register_push_token(telegram_id, "ios", f"{tag}-device")


async def _content_counts(telegram_id: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table, column in await db._user_scoped_tables():
        if table == "users":
            continue
        cur = await db.conn().execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (telegram_id,)
        )
        (n,) = await cur.fetchone()
        if n:
            counts[table] = n
    return counts


@pytest.mark.asyncio
async def test_synthetic_ids_do_not_collide_under_concurrency(fresh_db):
    users = await asyncio.gather(*(db.create_app_only_user() for _ in range(20)))
    ids = [u["telegram_id"] for u in users]
    assert len(set(ids)) == 20, "два app-only аккаунта получили один и тот же id"
    assert all(i < 0 for i in ids)
    assert all(i <= db.SYNTHETIC_TELEGRAM_ID_START for i in ids)


@pytest.mark.asyncio
async def test_merge_when_telegram_account_does_not_exist(fresh_db):
    app_user = await db.create_app_only_user()
    app_id = app_user["telegram_id"]
    await _seed_content(app_id, tag="a")
    before = await _content_counts(app_id)
    assert before, "seed didn't create anything — test is broken, not the code"

    telegram_id = 555111
    status = await db.link_telegram_to_app_account(app_id, telegram_id, username="realname")
    assert status == "ok"

    assert await db.get_user(app_id) is None
    target = await db.get_user(telegram_id)
    assert target is not None
    assert target["telegram_linked"] == 1
    assert target["username"] == "realname"

    after = await _content_counts(telegram_id)
    assert after == before, "не все таблицы переехали под новый id"
    assert await _content_counts(app_id) == {}, "под старым id ещё что-то осталось"

    # token minted before the merge is still resolvable, now to the real id
    tokens = await db.conn().execute("SELECT token FROM api_tokens WHERE user_id = ?", (telegram_id,))
    row = await tokens.fetchone()
    assert row is not None
    assert await db.resolve_api_token(row["token"]) == telegram_id


@pytest.mark.asyncio
async def test_merge_when_telegram_account_exists_but_empty(fresh_db):
    app_user = await db.create_app_only_user()
    app_id = app_user["telegram_id"]
    await _seed_content(app_id, tag="a")
    before = await _content_counts(app_id)

    telegram_id = 555222
    await db.get_or_create_user(telegram_id, username="tg-user")

    status = await db.link_telegram_to_app_account(app_id, telegram_id)
    assert status == "ok"

    assert await db.get_user(app_id) is None
    target = await db.get_user(telegram_id)
    assert target is not None
    assert target["telegram_linked"] == 1

    after = await _content_counts(telegram_id)
    assert after == before


@pytest.mark.asyncio
async def test_merge_refuses_when_both_accounts_have_data(fresh_db):
    app_user = await db.create_app_only_user()
    app_id = app_user["telegram_id"]
    await _seed_content(app_id, tag="app")

    telegram_id = 555333
    await db.get_or_create_user(telegram_id, username="tg-user")
    await _seed_content(telegram_id, tag="tg")

    app_before = await _content_counts(app_id)
    tg_before = await _content_counts(telegram_id)

    status = await db.link_telegram_to_app_account(app_id, telegram_id)
    assert status == "both_accounts_have_data"

    # nothing moved, nothing deleted, on either side
    assert await _content_counts(app_id) == app_before
    assert await _content_counts(telegram_id) == tg_before
    assert await db.get_user(app_id) is not None
    assert await db.get_user(telegram_id) is not None


@pytest.mark.asyncio
async def test_merge_rejects_non_app_only_source(fresh_db):
    """Код был выдан обычному, уже telegram-привязанному аккаунту (или уже
    израсходован раньше) — слияние должно отказаться, а не переписать чужой
    настоящий аккаунт поверх другого."""
    real_user = await db.get_or_create_user(111, username="real")
    status = await db.link_telegram_to_app_account(real_user["telegram_id"], 222)
    assert status == "not_app_account"
    assert await db.get_user(111) is not None
    assert await db.get_user(222) is None


@pytest.mark.asyncio
async def test_full_link_code_roundtrip_via_bot_handler(fresh_db, monkeypatch):
    """Сквозной путь: приложение просит код, человек присылает его боту
    командой /link_app — handlers/ios_link.cmd_link_app должен слить аккаунты."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from handlers import ios_link

    app_user = await db.create_app_only_user()
    app_id = app_user["telegram_id"]
    await _seed_content(app_id, tag="a")

    code = await db.issue_oauth_link_code(app_id, ttl_seconds=600)

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=555444, username="realname"),
        answer=AsyncMock(),
    )
    command = SimpleNamespace(args=code)

    await ios_link.cmd_link_app(message, command)

    message.answer.assert_awaited_once()
    assert await db.get_user(app_id) is None
    target = await db.get_user(555444)
    assert target is not None
    assert target["telegram_linked"] == 1


@pytest.mark.asyncio
async def test_link_app_handler_reports_conflict_without_changing_anything(fresh_db):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from handlers import ios_link

    app_user = await db.create_app_only_user()
    app_id = app_user["telegram_id"]
    await _seed_content(app_id, tag="app")

    telegram_id = 555555
    await db.get_or_create_user(telegram_id, username="tg-user")
    await _seed_content(telegram_id, tag="tg")

    code = await db.issue_oauth_link_code(app_id, ttl_seconds=600)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=telegram_id, username="tg-user"),
        answer=AsyncMock(),
    )
    command = SimpleNamespace(args=code)

    await ios_link.cmd_link_app(message, command)

    message.answer.assert_awaited_once()
    assert await db.get_user(app_id) is not None
    assert await db.get_user(telegram_id) is not None


@pytest.mark.asyncio
async def test_app_only_account_gets_ios_push_but_no_telegram_message(fresh_db, monkeypatch):
    """Аккаунт из приложения не имеет чата с ботом: телеграмная отправка ушла
    бы в никуда и каждый день сорила ошибкой в логах. Но пуш ему нужен — он
    едет на телефон через APNs, и запись о нём обязана появиться, иначе
    has_push_today не сработает и баннер уедет повторно тем же днём."""
    import datetime as dt

    import engagement

    user = await fresh_db.create_app_only_user()
    user_id = user["telegram_id"]

    sent_to_telegram: list[int] = []
    sent_to_apns: list[int] = []

    async def fake_send_photo(bot, telegram_id, decision, kb):
        sent_to_telegram.append(telegram_id)
        raise AssertionError("телеграмная отправка для app-only аккаунта не должна вызываться")

    async def fake_send_apns(telegram_id, decision):
        sent_to_apns.append(telegram_id)

    monkeypatch.setattr(engagement, "_send_push_photo", fake_send_photo)
    monkeypatch.setattr(engagement, "_send_apns_push", fake_send_apns)

    decision = engagement.PushDecision(category="win_back", text="текст", with_cta=True)
    await engagement._deliver(None, user_id, decision, dt.date(2026, 1, 1))

    assert sent_to_telegram == []
    assert sent_to_apns == [user_id]
    assert await fresh_db.has_push_today(user_id, "2026-01-01")
