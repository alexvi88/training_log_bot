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
    # `signup` пишет само заведение аккаунта (lifecycle_events) — у этих
    # тестов он фон, а не предмет проверки.
    cur = await db.conn().execute(
        "SELECT event, props, platform, app_version FROM analytics_events "
        "WHERE user_id = ? AND event != 'signup' ORDER BY id",
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


def test_review_prompt_event_keeps_reason_and_counts():
    assert product_metrics.clean_props(
        "review_prompt_requested", {"reason": "finish", "workouts": 1, "nth": 1, "text": "что ввёл человек"}
    ) == {"reason": "finish", "workouts": 1, "nth": 1}
    assert product_metrics.clean_props(
        "review_prompt_requested", {"reason": "hack", "workouts": "1", "nth": True}
    ) == {}


def test_you_story_events_keep_source_enum_and_capped_activity():
    for event in ("you_story_open", "you_story_share"):
        assert product_metrics.clean_props(event, {"source": "menu", "text": "что ввёл человек"}) == {
            "source": "menu"
        }
        assert product_metrics.clean_props(event, {"source": "Finish"}) == {"source": "finish"}
        # Источник не из списка — выкидывается, а не пишется как есть.
        assert product_metrics.clean_props(event, {"source": "push"}) == {}
    save = "com.apple.UIKit.activity.SaveToCameraRoll"
    assert product_metrics.clean_props(
        "you_story_shared", {"source": "finish", "activity": save, "caption": "подпись"}
    ) == {"source": "finish", "activity": save}
    assert product_metrics.clean_props("you_story_shared", {"activity": "cancelled"}) == {
        "activity": "cancelled"
    }
    long = "com.example." + "a" * 200
    assert product_metrics.clean_props("you_story_shared", {"activity": long}) == {
        "activity": long[:100]
    }
    for junk in ("жим лёжа", "two words", 5, None, ""):
        assert product_metrics.clean_props("you_story_shared", {"activity": junk}) == {}


def test_you_video_events_keep_source_enum_and_capped_activity():
    """Видео «Как ты рос» — тем же белым списком, что сторис «ТЫ»: источник
    только из списка, activity — тип действия iOS, текст человека не проходит."""
    assert product_metrics.clean_props("you_video_open", {"source": "menu", "text": "что ввёл человек"}) == {
        "source": "menu"
    }
    assert product_metrics.clean_props("you_video_open", {"source": "push"}) == {}
    save = "com.apple.UIKit.activity.SaveToCameraRoll"
    assert product_metrics.clean_props(
        "you_video_shared", {"source": "menu", "activity": save, "caption": "подпись"}
    ) == {"source": "menu", "activity": save}
    assert product_metrics.clean_props("you_video_shared", {"activity": "жим лёжа"}) == {}


@pytest.mark.asyncio
async def test_app_stores_you_story_events_and_drops_unknown_props(fresh_db):
    await db.get_or_create_user(telegram_id=7, username="c")
    token = await db.issue_api_token(7)
    transport = httpx.ASGITransport(app=api_v1.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/events",
            headers={"authorization": f"Bearer {token}", "x-app-version": "1.9 (80)"},
            json={"events": [
                {"name": "you_story_open", "props": {"source": "menu", "rank": "Новичок"}},
                {"name": "you_story_share", "props": {"source": "finish", "junk": 1}},
                {"name": "you_story_shared", "props": {
                    "source": "menu",
                    "activity": "com.apple.UIKit.activity.SaveToCameraRoll",
                    "text": "секрет",
                }},
                {"name": "you_story_shared", "props": {"source": "elsewhere", "activity": "cancelled"}},
            ]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"stored": 4}

    def dumped(props):
        return json.dumps(props, separators=(",", ":"), sort_keys=True)

    assert await _events(7) == [
        ("you_story_open", dumped({"source": "menu"}), "ios", "1.9 (80)"),
        ("you_story_share", dumped({"source": "finish"}), "ios", "1.9 (80)"),
        ("you_story_shared", dumped({
            "activity": "com.apple.UIKit.activity.SaveToCameraRoll", "source": "menu",
        }), "ios", "1.9 (80)"),
        ("you_story_shared", dumped({"activity": "cancelled"}), "ios", "1.9 (80)"),
    ]


# --- пуши в сводке ----------------------------------------------------------------


async def _tg_push(uid, category, stamp, channel="tg"):
    await db.record_push(uid, category, "текст", stamp[:10], channel=channel)
    await db.conn().execute(
        "UPDATE pushes SET sent_at = ? WHERE id = (SELECT MAX(id) FROM pushes)", (stamp,)
    )
    await db.conn().commit()


async def _ios_push(uid, category, stamp, ok=True):
    await db.log_analytics_events([
        (uid, "push_sent", product_metrics.props_json({"category": category, "ok": ok}), "ios", None, stamp)
    ])


async def _workout(uid, started, finished=None, source="manual"):
    await db.conn().execute(
        "INSERT INTO workouts (user_id, started_at, finished_at, status, source) VALUES (?, ?, ?, ?, ?)",
        (uid, started, finished, "finished" if finished else "active", source),
    )
    await db.conn().commit()


async def _push_day(monkeypatch):
    """Синтетические сутки: 1 — Telegram, 2 — iOS (один баннер дошёл, один нет,
    один тап), 3 — только отметка без телеграмной отправки; 900 — свой аккаунт
    по обоим каналам. Первый тренируется через 3 часа после пуша."""
    monkeypatch.setattr(config, "ADMIN_ID", 900)
    monkeypatch.setattr(config, "TEST_USER_ID", None)
    for uid in (1, 2, 3, 900):
        await _user(uid, DAY - dt.timedelta(days=10))
    d = DAY.isoformat()
    await _tg_push(1, "skip_7", f"{d}T10:00:00")
    await _tg_push(3, "release_x", f"{d}T10:00:00", channel=None)
    await _ios_push(2, "skip_7", f"{d}T11:00:00")
    await _ios_push(2, "plateau", f"{d}T12:00:00", ok=False)
    await _event(2, "push_open", DAY, props={"category": "skip_7"})
    # Нажатие кнопки под пушем в Telegram (record_tg_push_tap).
    await _event(1, "push_open", DAY, platform="tg", props={"category": "skip_7"})
    await _workout(1, f"{d}T13:00:00", f"{d}T14:00:00")
    # Импорт — история, а не ответ на пуш.
    await _workout(2, f"{d}T15:00:00", f"{d}T16:00:00", source="import")
    await _tg_push(900, "skip_7", f"{d}T10:00:00")
    await _ios_push(900, "skip_7", f"{d}T10:00:00")
    await _ios_push(900, "skip_7", f"{d}T10:00:00", ok=False)
    await _event(900, "push_open", DAY, props={"category": "skip_7"})
    await _event(900, "push_open", DAY, platform="tg", props={"category": "skip_7"})
    await _workout(900, f"{d}T13:00:00", f"{d}T14:00:00")


@pytest.mark.asyncio
async def test_rollup_counts_pushes_by_channel_without_own_accounts(fresh_db, monkeypatch):
    await _push_day(monkeypatch)
    metrics = await product_metrics.rollup_day(DAY)
    assert metrics["pushes_sent_tg"] == 1 and metrics["pushes_sent_ios"] == 1
    assert metrics["push_failed_ios"] == 1
    assert metrics["push_users"] == 2 and metrics["pushes_per_user"] == 1.0
    assert metrics["push_ctr_ios"] == 1.0 and metrics["push_ctr_tg"] == 1.0
    assert metrics["push_ctr"] == 1.0
    # Тапы — хранимая метрика суток: всего и по каналам, свои аккаунты не в счёт.
    assert metrics["push_opens"] == 2
    assert metrics["push_opens_ios"] == 1 and metrics["push_opens_tg"] == 1
    assert metrics["pushes_sent:skip_7"] == 2 and metrics["push_opens:skip_7"] == 2
    assert "pushes_sent:plateau" not in metrics and "pushes_sent:release_x" not in metrics
    # Окно суток ещё открыто: доля пишется, только когда кончатся следующие.
    assert "push_to_workout_24h" not in metrics
    names, rows = await product_metrics.pivot()
    assert "push_to_workout_24h" not in rows[0]

    await product_metrics.rollup_day(DAY + dt.timedelta(days=1))
    names, rows = await product_metrics.pivot()
    by_day = {r["day"]: r for r in rows}
    assert by_day[DAY.isoformat()]["push_to_workout_24h"] == 0.5

    table = admin.format_metrics_table(rows[:1])
    assert "skip_7" not in table
    assert (
        "📣 Пуши 09-01: 2 (TG 1 · iOS 1), 2 чел., тапов 2 (TG 1 · iOS 1), "
        "CTR iOS 100%, CTR TG 100%, не доставлено iOS 1" in table
    )
    csv_head = admin.metrics_csv(names, rows).decode().splitlines()[0].split(",")
    assert "pushes_sent:skip_7" in csv_head and "push_opens:skip_7" in csv_head


@pytest.mark.asyncio
async def test_push_to_workout_window_is_24_hours_after_the_push(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    monkeypatch.setattr(config, "TEST_USER_ID", None)
    for uid in (1, 2, 3):
        await _user(uid, DAY)
    d, nxt = DAY.isoformat(), (DAY + dt.timedelta(days=1)).isoformat()
    for uid in (1, 2, 3):
        await _tg_push(uid, "skip_7", f"{d}T22:00:00")
    await _workout(1, f"{nxt}T21:00:00")  # на следующий день, но в окне
    await _workout(2, f"{d}T08:00:00", f"{d}T09:00:00")  # до пуша
    await _workout(3, f"{nxt}T23:00:00")  # позже 24 часов
    assert await product_metrics.push_to_workout(DAY, set()) == (1, 3)


@pytest.mark.asyncio
async def test_catch_up_fills_push_to_workout_for_the_day_before_yesterday(fresh_db, monkeypatch):
    await _push_day(monkeypatch)
    await product_metrics.catch_up(DAY + dt.timedelta(days=2))
    names, rows = await product_metrics.pivot()
    by_day = {r["day"]: r for r in rows}
    assert by_day[DAY.isoformat()]["push_to_workout_24h"] == 0.5
    assert "push_to_workout_24h" not in by_day[(DAY + dt.timedelta(days=1)).isoformat()]


@pytest.mark.asyncio
async def test_receiving_a_push_is_not_activity(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    monkeypatch.setattr(config, "TEST_USER_ID", None)
    await _user(1, DAY - dt.timedelta(days=3))
    await _ios_push(1, "skip_7", f"{DAY.isoformat()}T10:00:00")
    metrics = await product_metrics.rollup_day(DAY)
    assert metrics["pushes_sent_ios"] == 1
    assert metrics["active_users"] == 0 and metrics["active_ios"] == 0


@pytest.mark.asyncio
async def test_daily_admin_report_has_a_push_block(fresh_db, monkeypatch):
    import admin_tasks

    await _push_day(monkeypatch)
    d = DAY + dt.timedelta(days=1)
    await _tg_push(1, "plateau", f"{d.isoformat()}T10:00:00")
    for stamp in ("T09:00:00", "T09:30:00"):
        await _ios_push(2, "skip_7", d.isoformat() + stamp)
    await _ios_push(2, "plateau", f"{d.isoformat()}T11:00:00")
    await _event(2, "push_open", d, props={"category": "skip_7"})
    await _tg_push(1, "rank_near", f"{d.isoformat()}T12:00:00")
    await _tg_push(1, "digest", f"{d.isoformat()}T13:00:00")
    await _event(1, "push_open", d, platform="tg", props={"category": "rank_near"})

    text = await admin_tasks._build_push_report(d)
    assert text.splitlines() == [
        "",
        "📣 Пуши",
        "Отправлено: 6 (Telegram 3 · iOS 3)",
        "Получили: 2 чел., 3.0 на человека",
        "Тапы: 2 (iOS 1 · Telegram 1), CTR iOS 33% · Telegram 33%",
        "Потренировались за 24 ч после пуша (01.09): 50% (1 из 2)",
        "  └ skip_7: отправлено 2 · тапов 1, CTR 50%",
        "  └ plateau: отправлено 2 · тапов 0, CTR 0%",
        "  └ rank_near: отправлено 1 · тапов 1, CTR 100%",
    ]
    empty = await admin_tasks._build_push_report(DAY + dt.timedelta(days=5))
    assert empty.splitlines() == ["", "📣 Пуши", "Не уходили", "Тапы: 0 (iOS 0 · Telegram 0)"]


@pytest.mark.asyncio
async def test_daily_report_counts_taps_on_the_tap_day_even_without_sends(fresh_db, monkeypatch):
    """Тап считается в сутки тапа: вчерашний пуш, открытый сегодня, — строка
    «Тапы» сегодня, хотя пушей в эти сутки не уходило."""
    import admin_tasks

    monkeypatch.setattr(config, "ADMIN_ID", None)
    monkeypatch.setattr(config, "TEST_USER_ID", None)
    await _user(1, DAY - dt.timedelta(days=3))
    await _user(2, DAY - dt.timedelta(days=3))
    await _tg_push(1, "skip_7", f"{DAY.isoformat()}T22:00:00")
    nxt = DAY + dt.timedelta(days=1)
    await _event(1, "push_open", nxt, platform="tg", props={"category": "skip_7"})
    await _event(2, "push_open", nxt, props={"category": "plateau"})

    text = await admin_tasks._build_push_report(nxt)
    assert text.splitlines() == [
        "",
        "📣 Пуши",
        "Не уходили",
        "Тапы: 2 (iOS 1 · Telegram 1)",
        "Потренировались за 24 ч после пуша (01.09): 0% (0 из 1)",
        "  └ plateau: отправлено 0 · тапов 1",
        "  └ skip_7: отправлено 0 · тапов 1",
    ]
    metrics = await product_metrics.rollup_day(nxt)
    assert metrics["push_opens"] == 2
    assert metrics["push_opens_tg"] == 1 and metrics["push_opens_ios"] == 1
    # Знаменателя нет — доли не пишутся вовсе, а не «0%».
    assert "push_ctr" not in metrics and "push_ctr_tg" not in metrics
    _, rows = await product_metrics.pivot()
    line = admin._metrics_push_line([r for r in rows if r["day"] == nxt.isoformat()])
    assert line == "📣 Пуши 09-02: 0 (TG 0 · iOS 0), тапов 2 (TG 1 · iOS 1)"


# --- тап по кнопке под пушем в Telegram --------------------------------------------


def _push_button_tap(uid: int, message_id, data: str = "push:start_workout"):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from aiogram.types import CallbackQuery

    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=uid, username="u", language_code=None)
    callback.message = SimpleNamespace(
        message_id=message_id, chat=SimpleNamespace(id=uid), reply_markup=None
    )
    callback.data = data
    return callback


@pytest.mark.asyncio
async def test_button_under_a_telegram_push_is_one_tap_with_its_category(fresh_db, monkeypatch):
    import activity_log

    monkeypatch.setattr(config, "ADMIN_ID", None)
    monkeypatch.setattr(config, "TEST_USER_ID", None)
    await _user(1, DAY - dt.timedelta(days=3))
    await db.record_push(1, "streak_at_risk", "текст", DAY.isoformat(), tg_message_id=55)
    # Старый пуш (до tg_message_id) и отметка без телеграмной отправки.
    await db.record_push(1, "skip_7", "текст", DAY.isoformat())
    await db.record_push(1, "release_x", "текст", DAY.isoformat(), channel=None)

    async def handled(event, data):
        return "ok"

    middleware = activity_log.LogCallbackQueries()
    await middleware(handled, _push_button_tap(1, 55), {})
    # Второе нажатие под тем же пушем (например, «пояс» после «Начать») — не тап.
    await middleware(handled, _push_button_tap(1, 55, data="settings:tz"), {})
    # Кнопка под обычным экраном бота — не тап по пушу.
    await middleware(handled, _push_button_tap(1, 77, data="wo:finish"), {})
    # Чужой человек с тем же id сообщения — не его пуш.
    await middleware(handled, _push_button_tap(2, 55), {})

    taps = [e for e in await _events(1) if e[0] == "push_open"]
    assert taps == [("push_open", '{"category":"streak_at_risk"}', "tg", None)]
    assert not [e for e in await _events(2) if e[0] == "push_open"]
    cur = await db.conn().execute("SELECT category FROM pushes WHERE tapped_at IS NOT NULL")
    assert [r[0] for r in await cur.fetchall()] == ["streak_at_risk"]


@pytest.mark.asyncio
async def test_tap_check_never_breaks_the_button(fresh_db, monkeypatch):
    """Сбой базы на проверке тапа — не повод сорвать нажатие кнопки."""
    async def boom(*args):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "claim_push_tap", boom)
    assert await product_metrics.record_tg_push_tap(1, 55) is False
    # Не id сообщения (MagicMock, None) — даже не спрашиваем базу.
    assert await product_metrics.record_tg_push_tap(1, None) is False
    assert await product_metrics.record_tg_push_tap(1, "55") is False


def test_event_time_is_utc_whatever_the_process_zone():
    """Время тапа с телефона — в UTC, как created_at у всего остального: при
    TZ≠UTC тап иначе уезжал в чужие сутки сводки."""
    import os
    import time

    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Moscow"
    time.tzset()
    try:
        now = dt.datetime(2026, 9, 1, 12, 0)
        assert api_v1_events.event_time("2026-09-01T10:00:00Z", now) == "2026-09-01T10:00:00"
        assert api_v1_events.event_time("2026-09-01T13:00:00+03:00", now) == "2026-09-01T10:00:00"
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_game_open_keeps_source_and_game_enums():
    assert product_metrics.clean_props("game_open", {"game": "runner", "source": "menu_arcade"}) == {
        "game": "runner", "source": "menu_arcade"
    }
    for source in ("rest", "achievements"):
        assert product_metrics.clean_props("game_open", {"game": "catch", "source": source})["source"] == source
    assert product_metrics.clean_props("game_open", {"game": "chess", "source": "push", "x": 1}) == {}
