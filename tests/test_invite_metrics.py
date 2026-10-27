"""Воронка приглашений: события экрана «Пригласить» из приложения (белый
список), просмотры страницы `/i/ref_*` в счётчике сайта, приход по
приглашению из бота и из приложения — строки суточной сводки, /metrics,
«кто приводит» в /growth и строка утреннего отчёта."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

import acquisition
import admin_tasks
import api_v1
import config
import db
import product_metrics
from handlers import admin, sharing

DAY = dt.date(2026, 9, 1)
BASE = "https://training-log.example.com"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1"


@pytest.fixture(autouse=True)
def no_own_accounts(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 900)
    monkeypatch.setattr(config, "TEST_USER_ID", None)


# --- белый список -------------------------------------------------------------------


@pytest.mark.parametrize("event", ["invite_open", "invite_share", "invite_copy"])
def test_invite_events_keep_only_known_source(event):
    for source in ("menu", "achievements"):
        assert product_metrics.clean_props(event, {"source": source}) == {"source": source}
    # Неизвестный источник выкидывается, а не схлопывается в «other».
    assert product_metrics.clean_props(event, {"source": "settings"}) == {}
    # Ссылка, текст, кому ушло — не свойства этих событий.
    assert product_metrics.clean_props(
        event, {"source": "menu", "url": "https://x/i/ref_5", "text": "Пойдём качаться"}
    ) == {"source": "menu"}


@pytest.mark.asyncio
async def test_app_stores_invite_events_without_text(fresh_db):
    await db.get_or_create_user(telegram_id=7, username="c")
    token = await db.issue_api_token(7)
    transport = httpx.ASGITransport(app=api_v1.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/events",
            headers={"authorization": f"Bearer {token}", "x-app-version": "2.0 (90)"},
            json={"events": [
                {"name": "invite_open", "props": {"source": "achievements"}},
                {"name": "invite_share", "props": {"source": "menu", "message": "го в зал"}},
                {"name": "invite_copy", "props": {"source": "elsewhere"}},
            ]},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"stored": 3}
    cur = await db.conn().execute(
        "SELECT event, props, platform FROM analytics_events WHERE user_id = 7 "
        "AND event LIKE 'invite_%' ORDER BY id"
    )
    assert [tuple(r) for r in await cur.fetchall()] == [
        ("invite_open", '{"source":"achievements"}', "ios"),
        ("invite_share", '{"source":"menu"}', "ios"),
        ("invite_copy", None, "ios"),
    ]


# --- страница /i/ref_* в счётчике сайта ------------------------------------------------


async def _get(path: str, user_agent: str) -> httpx.Response:
    import mcp_server

    transport = httpx.ASGITransport(app=mcp_server.build_app())
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        return await client.get(path, headers={"user-agent": user_agent})


@pytest.mark.asyncio
async def test_referral_page_view_is_counted_without_the_code(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", BASE)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(config, "APP_STORE_URL", "")
    monkeypatch.setattr(sharing, "_bot_username", None)

    assert (await _get("/i/ref_801", IPHONE)).status_code == 200
    # Превью ссылки в мессенджере — робот, не человек.
    assert (await _get("/i/ref_801", "TelegramBot (like TwitterBot)")).status_code == 200
    # Битый код — не приглашение.
    assert (await _get("/i/nope", IPHONE)).status_code == 404

    cur = await db.conn().execute("SELECT kind, page, device FROM site_events ORDER BY id")
    rows = [tuple(r) for r in await cur.fetchall()]
    assert rows == [("view", "/i/ref", "ios"), ("view", "/i/ref", "bot")]
    assert "801" not in json.dumps(rows)
    assert await db.site_page_views(dt.date.today().isoformat(), acquisition.REFERRAL_WEB_PAGE_KEY) == 1


# --- суточная сводка --------------------------------------------------------------------


async def _user(uid: int, created: dt.date, *, source=None, referrer=None, app_only=False):
    await db.get_or_create_user(telegram_id=uid, username=f"u{uid}")
    await db.conn().execute(
        "UPDATE users SET created_at = ?, telegram_linked = ? WHERE telegram_id = ?",
        (f"{created.isoformat()}T10:00:00", 0 if app_only else 1, uid),
    )
    await db.conn().commit()
    if source:
        await db.set_user_source(uid, source, referrer)


async def _finished(uid: int, when: dt.datetime, source: str = "manual"):
    stamp = when.isoformat(timespec="seconds")
    await db.conn().execute(
        "INSERT INTO workouts (user_id, started_at, finished_at, status, source) "
        "VALUES (?, ?, ?, 'finished', ?)",
        (uid, stamp, stamp, source),
    )
    await db.conn().commit()


async def _event(uid: int, event: str, day: dt.date, source: str = "menu"):
    await db.log_analytics_events(
        [(uid, event, json.dumps({"source": source}), "ios", "2.0", f"{day.isoformat()}T09:00:00")]
    )


async def _page_view(day: dt.date, device: str = "ios"):
    await db.conn().execute(
        "INSERT INTO site_events (kind, page, lang, referrer, device, created_at) "
        "VALUES ('view', ?, 'ru', 'direct', ?, ?)",
        (acquisition.REFERRAL_WEB_PAGE_KEY, device, f"{day.isoformat()}T08:00:00"),
    )
    await db.conn().commit()


async def _invite_day():
    """1 и 2 — приглашают (1 открыл дважды, поделился и скопировал; 2 открыл и
    скопировал), 900 — свой аккаунт, всё то же самое мимо сводки. Пришли: 11 —
    из бота по `ref_1`, 12 — из приложения по коду, 13 — по визитке (не
    приглашение), 14 — по приглашению своего аккаунта (сам он не свой — в
    счёт). Начал тренироваться в неделю после прихода только 11: 12 — на 10-й
    день, у 14 — только импорт. Страницу открыли дважды люди и раз робот."""
    for uid in (1, 2, 900):
        await _user(uid, DAY - dt.timedelta(days=30))
    await _event(1, "invite_open", DAY)
    await _event(1, "invite_open", DAY, "achievements")
    await _event(1, "invite_share", DAY)
    await _event(1, "invite_copy", DAY)
    await _event(2, "invite_open", DAY, "achievements")
    await _event(2, "invite_copy", DAY, "achievements")
    for event in ("invite_open", "invite_share", "invite_copy"):
        await _event(900, event, DAY)
    await _page_view(DAY)
    await _page_view(DAY, "android")
    await _page_view(DAY, "bot")
    await _user(11, DAY, source=acquisition.SOURCE_REFERRAL, referrer=1)
    await _user(12, DAY, source=acquisition.SOURCE_REFERRAL, referrer=1, app_only=True)
    await _user(13, DAY, source=acquisition.SOURCE_SHARED_CARD, referrer=2)
    await _user(14, DAY, source=acquisition.SOURCE_REFERRAL, referrer=900)
    noon = dt.datetime.combine(DAY, dt.time(12))
    await _finished(11, noon + dt.timedelta(days=2))
    await _finished(12, noon + dt.timedelta(days=10))
    await _finished(14, noon, source="import")


@pytest.mark.asyncio
async def test_invite_funnel_rows_in_daily_metrics(fresh_db):
    await _invite_day()
    metrics = await product_metrics.rollup_day(DAY)
    assert {k: metrics[k] for k in metrics if k.startswith(("invite_", "referral_"))} == {
        "invite_opens": 3,
        "invite_open_users": 2,
        "invite_shares": 1,
        "invite_share_users": 1,
        "invite_copies": 2,
        "invite_copy_users": 2,
        "invite_senders": 2,
        "invite_page_views": 2,
        "referral_signups": 3,
        "referral_signups_tg": 2,
        "referral_signups_ios": 1,
        "referral_trained": 1,
    }
    names, rows = await product_metrics.pivot()
    assert "invite_opens" in names and "referral_signups" in names
    assert admin.metrics_csv(names, rows).decode().splitlines()[0].count("invite_") == 8


@pytest.mark.asyncio
async def test_bot_start_with_ref_counts_as_referral_signup(fresh_db):
    await _user(1, DAY - dt.timedelta(days=30))
    attribution = acquisition.attribution_for("ref_1", 50)
    await _user(50, DAY, source=attribution.source, referrer=attribution.referrer_id)
    stats = await product_metrics.invite_stats(DAY, config.limit_preview_ids())
    assert (stats["signups"], stats["signups_tg"], stats["signups_ios"]) == (1, 1, 0)


@pytest.mark.asyncio
async def test_late_code_from_the_app_lands_on_the_signup_day(fresh_db):
    """Код из приложения доехал через три дня после заведения аккаунта, а
    тренировка — на пятый: строка дня заведения пересчитывается следующими
    rollup_day."""
    await _user(1, DAY - dt.timedelta(days=30))
    await _user(60, DAY, app_only=True)
    await product_metrics.rollup_day(DAY)
    await db.set_user_source(60, acquisition.SOURCE_REFERRAL, 1)
    await _finished(60, dt.datetime.combine(DAY + dt.timedelta(days=5), dt.time(18)))
    await product_metrics.rollup_day(DAY + dt.timedelta(days=6))
    _, rows = await product_metrics.pivot()
    by_day = {r["day"]: r for r in rows}
    assert by_day[DAY.isoformat()]["referral_signups"] == 1
    assert by_day[DAY.isoformat()]["referral_signups_ios"] == 1
    assert by_day[DAY.isoformat()]["referral_trained"] == 1
    # Пересчёт трогает только сутки, уже лежащие в сводке, — новых не заводит.
    assert (DAY + dt.timedelta(days=1)).isoformat() not in by_day
    assert (DAY + dt.timedelta(days=2)).isoformat() not in by_day


@pytest.mark.asyncio
async def test_app_referral_after_signup_is_counted(fresh_db, monkeypatch):
    """Путь приложения: аккаунт заводится входом (`signup` без источника), и
    только потом приложение отдаёт код в `POST /v1/me/referral` — из ссылки
    `/i/ref_<id>` или схемы `trainingdiary://i/<код>`. Приход всё равно в
    счёте: считается по users.source/referrer_id, а не по свойствам signup."""
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", BASE)
    await _user(1, DAY - dt.timedelta(days=30))
    user = await db.create_app_only_user(language_code="ru")
    uid = user["telegram_id"]
    cur = await db.conn().execute(
        "SELECT props FROM analytics_events WHERE user_id = ? AND event = 'signup'", (uid,)
    )
    assert all("referral" not in (r[0] or "") for r in await cur.fetchall())

    code = await db.issue_oauth_link_code(uid, ttl_seconds=600, digits=8)
    transport = httpx.ASGITransport(app=api_v1.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token = (await client.post("/auth/link", json={"code": code})).json()["token"]
        resp = await client.post(
            "/me/referral", json={"code": "ref_1"}, headers={"authorization": f"Bearer {token}"}
        )
    assert resp.json() == {"applied": True}
    row = await db.get_user(uid)
    assert (row["source"], row["referrer_id"]) == (acquisition.SOURCE_REFERRAL, 1)

    today = dt.date.fromisoformat(row["created_at"][:10])
    own = config.limit_preview_ids()
    stats = await product_metrics.invite_stats(today, own)
    assert (stats["signups"], stats["signups_ios"], stats["trained"]) == (1, 1, 0)
    await _finished(uid, dt.datetime.fromisoformat(row["created_at"][:19]) + dt.timedelta(hours=1))
    stats = await product_metrics.invite_stats(today, own)
    assert stats["trained"] == 1
    top = await db.top_referrers(10, 7)
    assert [(r["referrer_id"], r["invited"], r["activated"]) for r in top] == [(1, 1, 1)]


# --- /metrics, утренний отчёт, /growth -------------------------------------------------


def test_metrics_table_shows_invite_line():
    rows = [
        {"day": "2026-09-01", "active_users": 5, "invite_opens": 3, "invite_shares": 1,
         "invite_copies": 2, "invite_page_views": 2, "referral_signups_tg": 2,
         "referral_signups_ios": 1, "referral_trained": 1},
        {"day": "2026-09-02", "active_users": 6, "invite_opens": 1},
    ]
    text = admin.format_metrics_table(rows)
    assert (
        "🤝 Пришло по приглашениям за 2 дн.: 3 (TG 2 · iOS 1), из них начали тренироваться: 1\n"
        "   экран «Пригласить» 4, «Поделиться» 1, «Скопировать» 2, страница /i/ref 2"
    ) in text
    assert "приглашениям" not in admin.format_metrics_table([{"day": "2026-09-01", "active_users": 1}])


@pytest.mark.asyncio
async def test_morning_report_has_one_invite_line(fresh_db):
    await _invite_day()
    assert await admin_tasks._build_invite_line(DAY) == (
        "\n🤝 Пришло по приглашениям: 3, из них начали тренироваться: 1 "
        "(экран открыли 2, поделились 2)"
    )
    # Пустой день строки не даёт — отчёт не шумит.
    assert await admin_tasks._build_invite_line(DAY + dt.timedelta(days=5)) == ""


@pytest.mark.asyncio
async def test_top_referrers_by_period(fresh_db):
    await _user(1, DAY - dt.timedelta(days=60))
    await _user(2, DAY - dt.timedelta(days=60))
    today = dt.date.today()
    await _user(21, today, source=acquisition.SOURCE_REFERRAL, referrer=1)
    await _user(22, today, source=acquisition.SOURCE_REFERRAL, referrer=1)
    await _user(23, today - dt.timedelta(days=40), source=acquisition.SOURCE_REFERRAL, referrer=2)

    week = await db.top_referrers(10, 7)
    assert [(r["referrer_id"], r["invited"]) for r in week] == [(1, 2)]
    ever = await db.top_referrers(10)
    assert [(r["referrer_id"], r["invited"]) for r in ever] == [(1, 2), (2, 1)]
    one_day = await db.top_referrers(10, 30, day=today.isoformat())
    assert [r["referrer_id"] for r in one_day] == [1]

    text = acquisition.format_referrers(week, 7)
    assert "КТО ПРИВОДИТ · 7 дн." in text and "@u1 — привёл 2" in text
    assert "Пригласить" in acquisition.format_referrers([], 7)
