"""Забытая тренировка в приложении (workout_autoclose.py): через 90 минут без
подходов — баннер «Закрыть тренировку?» один раз, через 4 часа — тренировка
закрывается сама концом по последнему подходу и попадает в воронку
(`workout_finished` от клиента ios). Бот-only атлета и пустую тренировку не
трогаем."""

from __future__ import annotations

import datetime as dt
import json

import pytest

import apns
import db
import workout_autoclose

NOW = dt.datetime(2026, 11, 5, 15, 0, 0)


async def _workout_with_sets(user_id: int, last_set_minutes_ago: float, sets: int = 3) -> int:
    workout_id, _ = await db.get_or_create_active_workout(user_id)
    exercise_id = await db.create_exercise(user_id, "Жим лёжа", None)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, exercise_id, 0)
    for i in range(sets):
        moment = NOW - dt.timedelta(minutes=last_set_minutes_ago + (sets - 1 - i) * 3)
        await db.add_set(block_id, exercise_id, i, 0, 60, 8, created_at=moment.isoformat(timespec="seconds"))
    return workout_id


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_send(user_id, token, title, body, **kw):
        calls.append((user_id, title, body, kw.get("category")))
        return True

    async def fake_tokens(user_id, platform="ios"):
        return ["tok"]

    monkeypatch.setattr(apns, "is_configured", lambda: True)
    monkeypatch.setattr(apns, "send_alert", fake_send)
    monkeypatch.setattr(db, "get_push_tokens", fake_tokens)
    return calls


async def test_reminds_once_after_idle_then_finishes_after_four_hours(fresh_db, sent):
    user = await db.create_app_only_user(language_code="ru")
    uid = user["telegram_id"]
    workout_id = await _workout_with_sets(uid, last_set_minutes_ago=95)

    assert await workout_autoclose.tick(NOW) == (1, 0)
    assert len(sent) == 1
    _, title, body, category = sent[0]
    assert title == "Закрыть тренировку?"
    assert body.startswith("Записал 3 подхода.")
    assert category == workout_autoclose.CATEGORY_REMINDER
    # Повтор того же часа — второго баннера нет.
    assert await workout_autoclose.tick(NOW + dt.timedelta(minutes=10)) == (0, 0)
    assert len(sent) == 1
    assert (await db.get_workout(workout_id))["status"] == "active"

    later = NOW + dt.timedelta(hours=4)
    assert await workout_autoclose.tick(later) == (0, 1)
    workout = await db.get_workout(workout_id)
    assert workout["status"] == "finished"
    # Конец — последний подход, а не момент проверки.
    assert workout["finished_at"] == (NOW - dt.timedelta(minutes=95)).isoformat(timespec="seconds")
    assert len(sent) == 1  # закрытие — без баннера

    cur = await db.conn().execute(
        "SELECT event, platform FROM analytics_events WHERE user_id = ? AND event = 'workout_finished'", (uid,)
    )
    assert [tuple(r) for r in await cur.fetchall()] == [("workout_finished", "ios")]
    cur = await db.conn().execute("SELECT category, title, body FROM inbox WHERE user_id = ?", (uid,))
    rows = await cur.fetchall()
    assert [r["category"] for r in rows] == [workout_autoclose.CATEGORY_AUTO_FINISHED]
    assert rows[0]["title"] == "Закрыл тренировку сам"
    assert "3 подхода" in rows[0]["body"]


async def test_long_idle_finishes_without_reminder(fresh_db, sent):
    user = await db.create_app_only_user(language_code="en")
    workout_id = await _workout_with_sets(user["telegram_id"], last_set_minutes_ago=6 * 60, sets=1)
    assert await workout_autoclose.tick(NOW) == (0, 1)
    assert sent == []
    assert (await db.get_workout(workout_id))["status"] == "finished"


async def test_fresh_workout_and_bot_only_athlete_untouched(fresh_db, sent, user_id):
    fresh_user = await db.create_app_only_user(language_code="ru")
    fresh = await _workout_with_sets(fresh_user["telegram_id"], last_set_minutes_ago=20)
    # Бот-only атлет (user_id фикстуры — Telegram без приложения).
    bot_workout = await _workout_with_sets(user_id, last_set_minutes_ago=6 * 60)
    assert await workout_autoclose.tick(NOW) == (0, 0)
    assert sent == []
    assert (await db.get_workout(fresh))["status"] == "active"
    assert (await db.get_workout(bot_workout))["status"] == "active"


async def test_empty_workout_is_not_a_candidate(fresh_db, sent):
    user = await db.create_app_only_user(language_code="ru")
    workout_id, _ = await db.get_or_create_active_workout(user["telegram_id"])
    assert await workout_autoclose.tick(NOW + dt.timedelta(hours=10)) == (0, 0)
    assert (await db.get_workout(workout_id))["status"] == "active"


async def test_no_reminder_at_night_when_timezone_is_known(fresh_db, sent):
    # UTC+9: 15:00 UTC — полночь у атлета.
    user = await db.create_app_only_user(language_code="ru", tz_offset=9)
    await _workout_with_sets(user["telegram_id"], last_set_minutes_ago=95)
    assert await workout_autoclose.tick(NOW) == (0, 0)
    assert sent == []


def test_reminder_texts_fit_the_banner_in_every_language():
    import i18n
    import push_ios

    for lang in ("ru", "en", "es", "pt", "pt-PT", "de", "fr", "tr"):
        with i18n.use_lang(lang):
            for n in (1, 2, 5, 21, 128):
                for key in ("finish_reminder", "auto_finished"):
                    assert len(i18n.t(f"push.ios.{key}.title")) <= push_ios.TITLE_LIMIT
                    body = i18n.t(f"push.ios.{key}.body", sets=n)
                    # Напоминание — баннер (лимит баннера), закрытие — только
                    # строка «Уведомлений», ей можно длиннее.
                    limit = push_ios.BODY_LIMIT if key == "finish_reminder" else 160
                    assert len(body) <= limit, (lang, key, body)
                    assert json.dumps(body)
