"""Разовая правка data_fixes.MARATHON_FROM_STARTED_AT: «Марафонец», выданный
до #882 по started_at (открытие экрана), а не по первому подходу, забирается;
настоящий остаётся; демо-аккаунты не трогаются; второй прогон ничего не делает.

Тренировки — через /v1, как в tests/test_stale_workout_times.py; ошибочный
значок кладётся руками: финиш уже починен и сам его не выдаст.
"""

import logging

import pytest

import data_fixes
from tests.test_stale_workout_times import _linked_client, _live_workout, client_factory  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _analytics_count(db) -> int:
    cur = await db.conn().execute("SELECT COUNT(*) FROM analytics_events")
    return (await cur.fetchone())[0]


async def test_marathon_fix(fresh_db, client_factory, caplog):  # noqa: F811
    db = fresh_db
    # Ошибочный: подходы 72 минуты, экран открыт за 150.
    wrong = await _linked_client(db, client_factory, telegram_id=111)
    workout_id = await _live_workout(db, wrong, opened_min=150, set_minutes_ago=[72, 40, 1])
    assert (await wrong.post(f"/workouts/{workout_id}/finish", json={})).status_code == 200
    await db.award_achievements(111, {"marathon"})

    # Настоящий: от первого подхода до последнего больше двух часов.
    real = await _linked_client(db, client_factory, telegram_id=222)
    workout_id = await _live_workout(db, real, opened_min=150, set_minutes_ago=[130, 60, 1])
    assert (await real.post(f"/workouts/{workout_id}/finish", json={})).status_code == 200
    assert "marathon" in await db.list_achievement_codes(222)

    # Демо App Review (-1000000000) и прогона (-1000000001) — по id, без
    # тренировок вовсе: будь они проверены, значок бы ушёл.
    review = await db.create_app_only_user(track_signup=False)
    walk = await db.create_app_only_user(track_signup=False)
    assert (review["telegram_id"], walk["telegram_id"]) == (-1000000000, -1000000001)
    # И любой демо-вход по паролю — по провайдеру.
    await db.get_or_create_user(telegram_id=333, username="demo")
    await db.link_auth_identity(333, "review_demo", "catalog-demo")
    for uid in (-1000000000, -1000000001, 333):
        await db.award_achievements(uid, {"marathon"})

    events_before = await _analytics_count(db)
    with caplog.at_level(logging.INFO, logger="data_fixes"):
        await data_fixes.run_pending()

    assert "marathon" not in await db.list_achievement_codes(111)
    assert "marathon" in await db.list_achievement_codes(222)
    for uid in (-1000000000, -1000000001, 333):
        assert "marathon" in await db.list_achievement_codes(uid)
    # Прочие значки ошибочного атлета на месте.
    assert await db.list_achievement_codes(111)
    assert await _analytics_count(db) == events_before
    assert await db.is_data_fix_done(data_fixes.MARATHON_FROM_STARTED_AT)
    summary = [r.getMessage() for r in caplog.records if "проверено" in r.getMessage()]
    assert summary == [
        f"data_fixes: {data_fixes.MARATHON_FROM_STARTED_AT} — проверено 2, "
        "Марафонцев забрано 1, сбоев 0"
    ]

    # Второй прогон — ничего: даже снова выданный руками значок не трогается.
    await db.award_achievements(111, {"marathon"})
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="data_fixes"):
        await data_fixes.run_pending()
    assert "marathon" in await db.list_achievement_codes(111)
    assert not caplog.records


async def test_failure_is_logged_and_not_marked_done(fresh_db, monkeypatch, caplog):
    db = fresh_db
    await db.get_or_create_user(telegram_id=111, username="tester")
    await db.award_achievements(111, {"marathon"})

    async def boom(user_id, codes):
        raise RuntimeError("boom")

    monkeypatch.setattr(data_fixes.achievement_sync, "revoke_unearned", boom)
    await data_fixes.run_pending()

    assert not await db.is_data_fix_done(data_fixes.MARATHON_FROM_STARTED_AT)
    assert "marathon" in await db.list_achievement_codes(111)
    assert any(r.levelno == logging.ERROR for r in caplog.records)

    monkeypatch.undo()
    await data_fixes.run_pending()
    assert await db.is_data_fix_done(data_fixes.MARATHON_FROM_STARTED_AT)
    assert "marathon" not in await db.list_achievement_codes(111)
