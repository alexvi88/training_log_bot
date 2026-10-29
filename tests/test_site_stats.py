"""Счётчик сайта: кто такой «other» (семейство UA в базе, сырой UA — только в
логе) и честные «Просмотров людьми» — только подтверждённые маячком браузера
(`POST /go/seen`), роботы и рендер Googlebot не в счёт, история до маячка
читается и подписана."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

import httpx
import pytest

import acquisition
import config
import db
import mcp_server
import seo_pages
import seo_static
import site_stats

HOST = "https://training-log.example.com"
UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")
UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
UA_GOOGLEBOT = ("Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0 Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)")
UA_SEZNAM = "Mozilla/5.0 (compatible; SeznamBot/4.0; +https://o-seznam.cz/napoveda/vyhledavani/en/seznambot-crawler/)"
BROWSER_HEADERS = {"Accept-Language": "tr-TR,tr;q=0.9", "Sec-Fetch-Mode": "navigate"}
SEEN_HEADERS = {"Sec-Fetch-Site": "same-origin"}


@pytest.fixture(autouse=True)
def public_url(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", HOST)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(config, "SITE_BASE_URL", "")
    monkeypatch.setattr(config, "APP_STORE_URL", "")
    monkeypatch.setattr(seo_static, "OG_CACHE_DIR", str(tmp_path / "og"))


def _client():
    transport = httpx.ASGITransport(app=mcp_server.build_app(), client=("203.0.113.7", 5555))
    return httpx.AsyncClient(transport=transport, base_url=HOST)


async def _rows(fresh_db):
    cur = await fresh_db.conn().execute("SELECT * FROM site_events ORDER BY id")
    return [dict(r) for r in await cur.fetchall()]


@pytest.mark.parametrize("ua, family", [
    (UA_SEZNAM, "SeznamBot"),
    ("python-requests/2.31.0", "python-requests"),
    ("curl/8.4.0", "curl"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/120.0 Safari/537.36",
     "HeadlessChrome"),
    ("Mozilla/5.0 (compatible; DataForSeoBot/1.0; +https://dataforseo.com/dataforseo-bot)", "DataForSeoBot"),
    (UA_DESKTOP, "Chrome/120 Windows"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
     "Safari/17 Mac"),
    ("Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0", "Firefox/128 Linux"),
    ("Java/1.8.0_292", "Java"),
    (UA_IPHONE, None),
    (UA_GOOGLEBOT, None),
    ("", None),
])
def test_ua_family_is_a_short_token_not_the_user_agent(ua, family):
    assert site_stats.ua_family(ua) == family


def test_automation_hints_from_headers():
    assert site_stats.looks_automated(None) is None
    assert site_stats.looks_automated({}) == "no-al,no-sf"
    assert site_stats.looks_automated({"accept-language": "en"}) == "no-sf"
    assert site_stats.looks_automated({"accept-language": "en", "sec-fetch-mode": "navigate"}) is None


async def test_raw_ua_goes_to_log_only_for_unnamed_bots_and_automated_desktops(fresh_db, caplog):
    caplog.set_level("INFO", logger="site_stats")
    site_stats._unnamed_seen.clear()
    long_tail = "x" * 400
    async with _client() as client:
        await client.get("/tr", headers={"User-Agent": UA_SEZNAM + long_tail})
        await client.get("/fr", headers={"User-Agent": UA_DESKTOP})  # без Accept-Language/Sec-Fetch — скрейпер
        await client.get("/de", headers={"User-Agent": UA_DESKTOP, **BROWSER_HEADERS})
        await client.get("/es", headers={"User-Agent": UA_GOOGLEBOT})
    lines = [r.getMessage() for r in caplog.records
             if r.name == "site_stats" and r.levelname == "INFO" and r.getMessage().startswith("site view")]
    # Названный робот (Googlebot) в лог INFO не пишется — только в базу.
    bot, scraper, human = lines
    google = ""
    assert "crawler=other" in bot and 'ua="Mozilla/5.0 (compatible; SeznamBot/4.0;' in bot
    raw = bot.split('ua="', 1)[1].rstrip('"')
    assert len(raw) == site_stats.RAW_UA_LOG_CHARS
    assert "suspect=" in scraper and "Chrome/120.0.0.0" in scraper
    assert "ua=" not in human and "suspect" not in human
    assert "ua=" not in google
    assert "203.0.113" not in "\n".join(lines)

    rows = await _rows(fresh_db)
    assert [r["ua_family"] for r in rows] == ["SeznamBot", "Chrome/120 Windows", "Chrome/120 Windows", None]
    dump = json.dumps(rows)
    assert "Mozilla" not in dump and "203.0.113" not in dump

    stats = await fresh_db.site_stats(7)
    assert stats["other_bots"] == [("SeznamBot", 1)]
    block = site_stats.format_growth_block(stats, "7 дн.", [])
    assert "Роботы без имени (other) по UA: SeznamBot 1" in block


async def test_every_counted_page_carries_the_beacon_with_its_own_key():
    page = seo_pages.Page("tool", "1rm")
    key = seo_pages.url_for(page, "tr")
    async with _client() as client:
        html = (await client.get(key, headers={"User-Agent": UA_DESKTOP})).text
        missing = await client.get("/tr/no-such-page", headers={"User-Agent": UA_DESKTOP})
    assert site_stats.beacon_script(key) in html
    assert "sendBeacon" in html and "/go/seen" in html
    # Предзагрузка, фоновая вкладка и webdriver маячок не шлют.
    for guard in ("d.prerendering", "visibilityState", "n.webdriver"):
        assert guard in html
    assert missing.status_code == 404 and "sendBeacon" not in missing.text
    # Робот не ходит по /go/ — маячок закрыт в robots.txt вместе с кликами.
    assert "Disallow: /go/" in seo_pages.robots_txt()


async def test_human_views_are_the_confirmed_ones(fresh_db):
    async with _client() as client:
        # Человек: страница + маячок.
        await client.get("/tr/tools/1rm?utm_source=Habr", headers={
            "User-Agent": UA_DESKTOP, "Referer": "https://www.google.com/search?q=x", **BROWSER_HEADERS})
        seen = await client.post("/go/seen", params={
            "p": "/tr/tools/1rm", "l": "tr", "r": "https://www.google.com/search?q=secret", "utm_source": "Habr"},
            headers={"User-Agent": UA_DESKTOP, **SEEN_HEADERS})
        assert seen.status_code == 204 and seen.headers["cache-control"] == "no-store"
        # Скрейпер с браузерным UA: страницу берёт, JS не исполняет.
        for _ in range(3):
            await client.get("/tr/exercises", headers={"User-Agent": UA_DESKTOP, "Referer": f"{HOST}/tr"})
        # Рендер Googlebot исполняет JS — маячок отброшен по UA.
        await client.post("/go/seen", params={"p": "/tr"}, headers={"User-Agent": UA_GOOGLEBOT, **SEEN_HEADERS})
        # Чужой сайт, мусорный ключ и GET — тоже мимо.
        await client.post("/go/seen", params={"p": "/tr"}, headers={"User-Agent": UA_DESKTOP, "Sec-Fetch-Site": "cross-site"})
        await client.post("/go/seen", params={"p": "/wp-admin/x"}, headers={"User-Agent": UA_DESKTOP, **SEEN_HEADERS})
        await client.post("/go/seen", params={"p": "/tr/../../etc"}, headers={"User-Agent": UA_DESKTOP, **SEEN_HEADERS})
        assert (await client.get("/go/seen", params={"p": "/tr"}, headers={"User-Agent": UA_DESKTOP})).status_code == 405
        # Клик по App Store — считается как раньше.
        await client.get("/go/bot?f=rm1", headers={"User-Agent": UA_DESKTOP, "Referer": f"{HOST}/tr/tools/1rm"})

    rows = await _rows(fresh_db)
    seen_rows = [r for r in rows if r["kind"] == "seen"]
    assert len(seen_rows) == 1
    (s,) = seen_rows
    assert (s["page"], s["lang"], s["referrer"], s["device"], s["utm"], s["ua_family"]) == (
        "/tr/tools/1rm", "tr", "google", "desktop", "habr", "Chrome/120 Windows")
    assert "secret" not in json.dumps(rows)

    stats = await fresh_db.site_stats(7)
    assert stats["views"] == 1
    assert stats["pages"] == [("/tr/tools/1rm", 1)]
    assert stats["referrers"] == [("google", 1)]
    assert stats["devices"] == [("desktop", 1)]
    assert stats["unconfirmed"] == 4
    assert stats["unconfirmed_families"] == [("Chrome/120 Windows", 4)]
    assert stats["legacy_views"] == 0
    assert stats["confirmed_since"] is not None
    block = site_stats.format_growth_block(stats, "7 дн.", [])
    today = dt.date.today()
    assert f"Просмотров людьми: 1 (с {today:%d.%m} — только подтверждённые браузером)" in block
    assert "Без подтверждения браузером: 4 (компьютер: Chrome/120 Windows 4)" in block
    assert "Откуда: google 1" in block
    assert "Клики: в бота 1, в App Store 0 — 100.0% от просмотров" in block

    feed = [r for r in await fresh_db.site_visits_feed() if r["crawler"] is None]
    assert [(r["page"], r["n"]) for r in feed] == [("/tr/tools/1rm", 1)]
    assert await fresh_db.count_site_visit_buckets() == 1


async def test_history_before_the_beacon_stays_readable_and_labelled(fresh_db):
    # До деплоя маячков нет — людьми остаются серверные просмотры, и это подписано.
    await fresh_db.record_site_event("view", "/en", "en", "direct", "desktop")
    await fresh_db.record_site_event("view", "/ru", "ru", "google", "ios")
    stats = await fresh_db.site_stats(7)
    assert stats["views"] == 2 and stats["confirmed_since"] is None and stats["unconfirmed"] == 0
    block = site_stats.format_growth_block(stats, "7 дн.", [])
    assert "Просмотров людьми: 2 (серверный счёт: подтверждений браузером ещё не было)" in block

    # Старые строки — вчера; сегодня пришёл первый маячок.
    yesterday = (dt.datetime.now() - dt.timedelta(days=1)).isoformat(timespec="seconds")
    await fresh_db.conn().execute("UPDATE site_events SET created_at = ?", (yesterday,))
    await fresh_db.record_site_event("view", "/en", "en", "direct", "desktop")
    await fresh_db.record_site_event("seen", "/en", "en", "direct", "desktop")
    await fresh_db.record_site_event("view", "/de", "de", "internal", "desktop")  # без маячка
    stats = await fresh_db.site_stats(7)
    assert stats["views"] == 3 and stats["legacy_views"] == 2 and stats["unconfirmed"] == 2
    block = site_stats.format_growth_block(stats, "7 дн.", [])
    assert (f"Просмотров людьми: 3 (с {dt.date.today():%d.%m} — только подтверждённые браузером; "
            "2 из них — раньше, серверный счёт без проверки)") in block
    # Страница приглашения в метриках — по тому же правилу.
    await fresh_db.record_site_event("view", acquisition.REFERRAL_WEB_PAGE_KEY, "en", "direct", "ios")
    assert await fresh_db.site_page_views(dt.date.today().isoformat(), acquisition.REFERRAL_WEB_PAGE_KEY) == 0
    await fresh_db.record_site_event("seen", acquisition.REFERRAL_WEB_PAGE_KEY, "en", "direct", "ios")
    assert await fresh_db.site_page_views(dt.date.today().isoformat(), acquisition.REFERRAL_WEB_PAGE_KEY) == 1


async def test_referral_page_beacon_uses_the_shared_key(fresh_db):
    async with _client() as client:
        page = await client.get(f"/i/{acquisition.referral_payload(123456)}",
                                headers={"User-Agent": UA_IPHONE, "Accept-Language": "en"})
        await client.post("/go/seen", params={"p": acquisition.REFERRAL_WEB_PAGE_KEY, "l": "en"},
                          headers={"User-Agent": UA_IPHONE, **SEEN_HEADERS})
    assert site_stats.beacon_script(acquisition.REFERRAL_WEB_PAGE_KEY) in page.text
    rows = await _rows(fresh_db)
    assert [(r["kind"], r["page"], r["lang"]) for r in rows] == [
        ("view", "/i/ref", "en"), ("seen", "/i/ref", "en")]


async def test_old_database_gets_the_ua_family_column(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE site_events (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, page TEXT NOT NULL, "
        "lang TEXT, referrer TEXT NOT NULL, device TEXT NOT NULL, crawler TEXT, utm TEXT, target TEXT, "
        "created_at TEXT NOT NULL)")
    raw.execute("INSERT INTO site_events (kind, page, lang, referrer, device, created_at) "
                "VALUES ('view', '/en', 'en', 'direct', 'desktop', '2026-10-01T10:00:00')")
    raw.commit()
    raw.close()
    await db.init_db(str(path))
    try:
        cur = await db.conn().execute("SELECT ua_family FROM site_events")
        assert [r[0] for r in await cur.fetchall()] == [None]
        await db.record_site_event("view", "/en", "en", "direct", "bot", "other", ua_family="curl")
        assert (await db.site_stats(3650))["other_bots"] == [("curl", 1)]
    finally:
        await db.close_db()


async def test_bots_dont_flood_the_log(fresh_db, caplog):
    """Названный робот — не в INFO вовсе; безымянный — одна строка на
    семейство в сутки. В базу пишется каждый заход."""
    caplog.set_level("INFO", logger="site_stats")
    site_stats._unnamed_seen.clear()
    async with _client() as client:
        for path in ("/tr", "/fr", "/de"):
            await client.get(path, headers={"User-Agent": UA_SEZNAM})
            await client.get(path, headers={"User-Agent": UA_GOOGLEBOT})
    info = [r.getMessage() for r in caplog.records
            if r.name == "site_stats" and r.levelname == "INFO" and r.getMessage().startswith("site view")]
    assert len(info) == 1 and "crawler=other" in info[0]
    assert len(await _rows(fresh_db)) == 6
