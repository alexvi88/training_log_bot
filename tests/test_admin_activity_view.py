"""Лента /activity: чья строка и по каким часам."""

import datetime as dt

import activity_log
import config
from handlers import admin


def _row(kind: str, content: str, created_at: str, username: str | None = "athlete"):
    return {
        "kind": kind,
        "content": content,
        "created_at": created_at,
        "username": username,
        "telegram_id": 111,
    }


def test_coach_answers_have_their_own_marker(monkeypatch):
    """Ответ тренера в ленте видно, и он не путается с репликой человека."""
    monkeypatch.setattr(config, "ADMIN_TZ_OFFSET", 0)
    said = admin._activity_line(_row(activity_log.KIND_MESSAGE, "хочу программу", "2026-08-17T09:00:00"))
    answered = admin._activity_line(_row(activity_log.KIND_AI_REPLY, "Собрал верх/низ", "2026-08-17T09:01:00"))
    tapped = admin._activity_line(_row(activity_log.KIND_CALLBACK, "✅ Добавить себе", "2026-08-17T09:02:00"))

    assert said.endswith("💬 хочу программу")
    assert answered.endswith("🤖 Собрал верх/низ")
    assert tapped.endswith("👉 ✅ Добавить себе")


def test_time_is_shown_on_the_admins_clock(monkeypatch):
    """В базе время серверное (UTC), на экране — московское: иначе вечерний
    всплеск в логе съезжает на день назад."""
    monkeypatch.setattr(config, "ADMIN_TZ_OFFSET", 3)
    row = _row(activity_log.KIND_MESSAGE, "поздний подход", "2026-08-17T22:30:00")

    assert admin._activity_line(row).startswith("18.08 01:30")
    assert admin._activity_line_all(row).startswith("18.08 01:30")


def test_admin_time_shifts_by_the_configured_offset(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TZ_OFFSET", 3)
    assert admin.admin_time("2026-08-17T09:00:00") == dt.datetime(2026, 8, 17, 12, 0)


async def test_coach_reply_lands_in_the_log(fresh_db):
    await fresh_db.get_or_create_user(111, "athlete")

    await activity_log.record_ai_reply(111, "Записал. Погнали дальше.")

    (row,) = await fresh_db.list_user_events(111, limit=10)
    assert row["kind"] == activity_log.KIND_AI_REPLY
    assert row["content"] == "Записал. Погнали дальше."


async def test_long_coach_reply_is_truncated_like_any_other_event(fresh_db):
    await fresh_db.get_or_create_user(111, "athlete")

    await activity_log.record_ai_reply(111, "х" * (activity_log.MAX_CONTENT_LEN + 500))

    (row,) = await fresh_db.list_user_events(111, limit=10)
    assert len(row["content"]) == activity_log.MAX_CONTENT_LEN
    assert row["content"].endswith("…")
