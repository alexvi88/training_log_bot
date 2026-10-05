"""Продуктовая аналитика (product_metrics.py, api_v1_events.py): белые списки
событий, приём пачки из приложения, серверные события и суточная сводка."""

import datetime as dt
import json

import httpx
import pytest

import api_v1
import api_v1_events
import config
import db
import product_metrics
from handlers import admin

DAY = dt.date(2026, 9, 1)


# --- белые списки ---------------------------------------------------------------


def test_props_keep_only_whitelisted_machine_values():
    assert product_metrics.clean_props("screen_view", {"screen": "Progress", "text": "секрет"}) == {
        "screen": "progress"
    }
    assert product_metrics.clean_props("screen_view", {"screen": "жим лёжа"}) == {}
    assert product_metrics.clean_props("push_open", {"category": "support_thread:123"}) == {
        "category": "support_thread"
    }
    assert product_metrics.clean_props("app_background", {"seconds": 95.7}) == {"seconds": 95}
    assert product_metrics.clean_props("app_background", {"seconds": -1}) == {}
    assert product_metrics.clean_props("app_background", {"seconds": True}) == {}
    assert product_metrics.clean_props("app_open", None) == {}
    assert product_metrics.clean_props("hack", {}) is None


def test_event_time_accepts_recent_phone_clock_only():
    now = dt.datetime(2026, 9, 29, 12, 0, 0)
    local = dt.datetime(2026, 9, 29, 11, 0, 0).astimezone()
    assert api_v1_events.event_time(local.isoformat(), now) == "2026-09-29T11:00:00"
    old = (now - dt.timedelta(days=8)).astimezone().isoformat()
    assert api_v1_events.event_time(old, now) is None
    future = (now + dt.timedelta(hours=1)).astimezone().isoformat()
    assert api_v1_events.event_time(future, now) is None
    # Часы телефона спешат на минуту — событие ложится «сейчас», а не в будущее.
    skew = (now + dt.timedelta(minutes=1)).astimezone().isoformat()
    assert api_v1_events.event_time(skew, now) == "2026-09-29T12:00:00"
    assert api_v1_events.event_time("2026-09-29T11:00:00", now) is None  # без пояса
    assert api_v1_events.event_time("garbage", now) is None


# --- приём из приложения ------------------------------------------------------------


async def _events(user):
    cur = await db.conn().execute(
        "SELECT event, props, platform, app_version FROM analytics_events WHERE user_id = ? ORDER BY id",
        (user,),
    )
    return [tuple(r) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_app_batch_is_stored_with_version_and_unknown_skipped(fresh_db):
    await db.get_or_create_user(telegram_id=5, username="a")
    token = await db.issue_api_token(5)
    transport = httpx.ASGITransport(app=api_v1.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/events",
            headers={"authorization": f"Bearer {token}", "x-app-version": "1.4 (57)"},
            json={"events": [
                {"name": "app_open"},
                {"name": "screen_view", "props": {"screen": "coach", "junk": "x"}},
                {"name": "from_the_future_build"},
                "garbage",
            ]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"stored": 2}
        # Сама пачка — не действие человека: ни в ленте, ни событием `action`.
        resp = await client.post(
            "/events", headers={"authorization": f"Bearer {token}"}, json={"events": [{}] * 101}
        )
        assert resp.status_code == 400
        resp = await client.post("/events", json={"events": []})
        assert resp.status_code == 401
    assert await _events(5) == [
        ("app_open", None, "ios", "1.4 (57)"),
        ("screen_view", json.dumps({"screen": "coach"}, separators=(",", ":")), "ios", "1.4 (57)"),
    ]
    cur = await db.conn().execute("SELECT COUNT(*) FROM user_events WHERE telegram_id = 5")
    assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_app_action_is_tracked_by_route_template(fresh_db):
    await db.get_or_create_user(telegram_id=6, username="b")
    token = await db.issue_api_token(6)
    transport = httpx.ASGITransport(app=api_v1.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/workouts/active", headers={"authorization": f"Bearer {token}", "x-app-version": "2.0"}
        )
        assert resp.status_code in (200, 201), resp.text
    assert ("action", '{"route":"POST /workouts/active"}', "ios", "2.0") in await _events(6)


def test_telegram_message_props_carry_no_typed_text():
    class Msg:
        def __init__(self, text, content_type="text"):
            self.text = text
            self.content_type = content_type

    import activity_log

    assert activity_log.message_props(Msg("100 8"), False) == {"type": "text"}
    assert activity_log.message_props(Msg("/start@AthleteBot ref_x"), False) == {
        "type": "command", "command": "start",
    }
    assert activity_log.message_props(Msg("🏋️ Тренировка"), True) == {"type": "reply_button"}
    assert activity_log.message_props(Msg(None, "photo"), False) == {"type": "photo"}


# --- суточная сводка ---------------------------------------------------------------


async def _user(uid, created: dt.date):
    await db.get_or_create_user(telegram_id=uid, username=f"u{uid}")
    await db.conn().execute(
        "UPDATE users SET created_at = ? WHERE telegram_id = ?", (f"{created.isoformat()}T10:00:00", uid)
    )
    await db.conn().commit()


async def _set(uid, day: dt.date):
    exercise_id = await db.create_exercise(uid, "Жим", None)
    stamp = f"{day.isoformat()}T12:00:00"
    cur = await db.conn().execute(
        "INSERT INTO workouts (user_id, started_at, finished_at, status) VALUES (?, ?, ?, 'finished')",
        (uid, stamp, stamp),
    )
    workout_id = cur.lastrowid
    cur = await db.conn().execute(
        "INSERT INTO workout_blocks (workout_id, order_index) VALUES (?, 0)", (workout_id,)
    )
    await db.conn().execute(
        "INSERT INTO sets (block_id, exercise_id, round_index, weight, reps, created_at) "
        "VALUES (?, ?, 0, 100, 5, ?)",
        (cur.lastrowid, exercise_id, stamp),
    )
    await db.conn().commit()


async def _event(uid, event, day: dt.date, platform="ios", props=None):
    await db.log_analytics_events(
        [(uid, event, json.dumps(props) if props else None, platform, "1.0", f"{day.isoformat()}T09:00:00")]
    )


@pytest.mark.asyncio
async def test_rollup_counts_active_by_platform_and_skips_own_accounts(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 900)
    monkeypatch.setattr(config, "TEST_USER_ID", None)
    await _user(1, DAY)
    await _user(2, DAY - dt.timedelta(days=3))
    await _user(900, DAY - dt.timedelta(days=3))
    await _set(1, DAY)
    await _event(1, "app_open", DAY)
    await _event(1, "app_background", DAY, props={"seconds": 120})
    await _event(2, "tg_message", DAY, platform="tg")
    await _event(900, "app_open", DAY)
    await _set(900, DAY)

    metrics = await product_metrics.rollup_day(DAY)
    assert metrics["active_users"] == 2
    assert metrics["active_ios"] == 1 and metrics["active_tg"] == 1
    assert metrics["new_users"] == 1
    assert metrics["trained_users"] == 1 and metrics["sets_logged"] == 1
    assert metrics["workouts_finished"] == 1
    assert metrics["app_opens"] == 1 and metrics["app_session_seconds"] == 120
    assert metrics["wau"] == 2 and metrics["mau"] == 2


@pytest.mark.asyncio
async def test_retention_is_written_on_cohort_day(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    monkeypatch.setattr(config, "TEST_USER_ID", None)
    cohort = DAY - dt.timedelta(days=7)
    await _user(1, cohort)
    await _user(2, cohort)
    await _set(1, DAY)
    await product_metrics.rollup_day(DAY)
    names, rows = await product_metrics.pivot()
    by_day = {r["day"]: r for r in rows}
    assert by_day[cohort.isoformat()]["cohort_d7"] == 2
    assert by_day[cohort.isoformat()]["retained_d7"] == 1


@pytest.mark.asyncio
async def test_catch_up_backfills_history_once_and_always_redoes_yesterday(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    monkeypatch.setattr(config, "TEST_USER_ID", None)
    await _user(1, DAY)
    await _set(1, DAY + dt.timedelta(days=1))
    today = DAY + dt.timedelta(days=3)
    assert await product_metrics.catch_up(today) == 3
    assert await product_metrics.catch_up(today) == 1
    names, rows = await product_metrics.pivot()
    assert [r["day"] for r in rows] == [
        (DAY + dt.timedelta(days=i)).isoformat() for i in range(3)
    ]
    assert rows[1]["active_users"] == 1 and rows[0]["active_users"] == 0


@pytest.mark.asyncio
async def test_old_events_are_pruned_but_daily_metrics_stay(fresh_db):
    await _user(1, DAY)
    old = dt.date.today() - dt.timedelta(days=config.ANALYTICS_RETENTION_DAYS + 5)
    await _event(1, "app_open", old)
    await _event(1, "app_open", dt.date.today())
    await db.upsert_daily_metrics(old.isoformat(), {"active_users": 3})
    assert await db.prune_old_analytics_events(config.ANALYTICS_RETENTION_DAYS) == 1
    names, rows = await product_metrics.pivot()
    assert rows[0]["active_users"] == 3


def test_metrics_table_and_csv():
    rows = [
        {"day": "2026-09-01", "active_users": 5, "wau": 7, "mau": 9, "new_users": 2,
         "trained_users": 3, "active_ios": 1, "cohort_d1": 4, "retained_d1": 1},
        {"day": "2026-09-02", "active_users": 6, "ai_cost_usd": 0.25},
    ]
    text = admin.format_metrics_table(rows)
    assert "09-01" in text and "D1 25% (1/4)" in text
    assert text.count("<pre>") == 1
    csv_text = admin.metrics_csv(["active_users", "ai_cost_usd"], rows).decode()
    assert csv_text.splitlines() == ["day,active_users,ai_cost_usd", "2026-09-01,5,", "2026-09-02,6,0.25"]
    assert "пустая" in admin.format_metrics_table([])


def test_plan_offer_event_keeps_only_action():
    for action in ("shown", "accept", "later"):
        assert product_metrics.clean_props(
            "plan_offer", {"action": action, "text": "что ввёл человек", "screen": "x"}
        ) == {"action": action}
    assert product_metrics.clean_props("plan_offer", {}) == {}
