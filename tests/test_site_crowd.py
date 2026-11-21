"""Обезличенные цифры атлетов на сайте (site_crowd.py) и чистка тонких
страниц: порог K, свои аккаунты мимо, честный lastmod, 301 со старых
адресов, что не попадает в sitemap, и «О проекте» на всех языках."""

import datetime as dt
import json
import re
import xml.etree.ElementTree as ET

import httpx
import pytest

import config
import mcp_server
import seo_catalog
import seo_library
import seo_pages
import seo_swaps
import site_crowd

HOST = "https://training-log.example.com"
BENCH = "bench-press"


@pytest.fixture(autouse=True)
def site(monkeypatch):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", HOST)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(config, "SITE_BASE_URL", "")
    monkeypatch.setattr(config, "APP_STORE_URL", "")
    site_crowd.reset()
    yield
    site_crowd.reset()


async def _get_many(paths):
    transport = httpx.ASGITransport(app=mcp_server.build_app())
    async with httpx.AsyncClient(transport=transport, base_url=HOST) as client:
        return {p: await client.get(p) for p in paths}


def _identity(url: str) -> str:
    return seo_swaps.identity_of(url)


async def _athlete(db, user_id: int, identity: str, weights: list[float], *, reps: int = 8, gender=None,
                   unit: str = "kg", bodyweight=None, days_apart: int = 4, n_sets: int = 3) -> None:
    """Атлет с тренировками: по одной на вес из `weights`, `n_sets` подходов,
    последний — самый тяжёлый."""
    now = dt.datetime.now(dt.timezone.utc)
    iso = now.strftime("%Y-%m-%dT%H:%M:%S")
    c = db.conn()
    await c.execute("INSERT INTO users (telegram_id, created_at, unit, gender) VALUES (?, ?, ?, ?)",
                    (user_id, iso, unit, gender))
    cur = await c.execute(
        "INSERT INTO exercises (user_id, name, display_name, original_name, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, identity, identity, identity, iso))
    ex_id = cur.lastrowid
    if bodyweight:
        await c.execute("INSERT INTO bodyweight_logs (telegram_id, weight, logged_at) VALUES (?, ?, ?)",
                        (user_id, bodyweight, iso))
    for i, top in enumerate(weights):
        started = (now - dt.timedelta(days=days_apart * (len(weights) - i))).strftime("%Y-%m-%dT%H:%M:%S")
        cur = await c.execute("INSERT INTO workouts (user_id, started_at, finished_at, status) VALUES (?, ?, ?, 'finished')",
                              (user_id, started, started))
        cur = await c.execute("INSERT INTO workout_blocks (workout_id, order_index) VALUES (?, 0)", (cur.lastrowid,))
        block = cur.lastrowid
        for n in range(n_sets):
            weight = top if n == n_sets - 1 else top * 0.6
            await c.execute(
                "INSERT INTO sets (block_id, exercise_id, round_index, weight, reps, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (block, ex_id, n, weight, reps if n == n_sets - 1 else 12, started))
    await c.commit()


async def _crowd(db, count: int, identity: str, *, start: int = 1000, **kw) -> None:
    for i in range(count):
        await _athlete(db, start + i, identity, [80.0 + (i % 5) * 5] * 3, **kw)


# ---------- порог K ----------


@pytest.mark.asyncio
async def test_below_k_athletes_nothing_renders(fresh_db):
    identity = _identity(BENCH)
    await _crowd(fresh_db, site_crowd.K_MIN_USERS - 1, identity, gender="male", bodyweight=80)
    await site_crowd.refresh(force=True)
    assert site_crowd.cached() == {}
    for lang in ("ru", "en"):
        assert "id=crowd" not in seo_pages.render(seo_pages.Page("exercise", BENCH), lang)
    # И в базе нет ни строки — показывать нечего.
    assert await fresh_db.site_crowd_state() == {}


@pytest.mark.asyncio
async def test_own_and_demo_accounts_do_not_count_toward_k(fresh_db, monkeypatch):
    identity = _identity(BENCH)
    monkeypatch.setattr(config, "ADMIN_ID", 777)
    await _crowd(fresh_db, site_crowd.K_MIN_USERS - 1, identity)
    await _athlete(fresh_db, 777, identity, [300.0] * 3)
    await site_crowd.refresh(force=True)
    assert site_crowd.stats_for(identity) is None


@pytest.mark.asyncio
async def test_at_k_the_block_shows_rounded_aggregates_only(fresh_db):
    identity = _identity(BENCH)
    await _crowd(fresh_db, site_crowd.K_MIN_USERS, identity, gender="male", bodyweight=80)
    # Пять женщин — меньше K: женской строки нет, мужская есть.
    await _crowd(fresh_db, 5, identity, start=5000, gender="female", bodyweight=60)
    await site_crowd.refresh(force=True)
    stats = site_crowd.stats_for(identity)
    assert stats["athletes"] == 20
    assert set(stats) <= {"athletes", "weight", "weight_m", "weight_f", "bw", "reps", "sets", "every_days"}
    assert "weight_m" in stats and "weight_f" not in stats
    low, med, high = stats["weight"]
    assert low <= med <= high and all(v % 5 == 0 for v in stats["weight"])
    assert stats["reps"] == [8, 8, 8] and stats["sets"] == 3 and stats["every_days"] == 4
    ru = seo_pages.render(seo_pages.Page("exercise", BENCH), "ru")
    en = seo_pages.render(seo_pages.Page("exercise", BENCH), "en")
    assert "id=crowd" in ru and "20+ атлетов" in ru and "Мужчины" in ru and "Женщины" not in ru
    assert "20+ athletes" in en and f"{med:g}<span class=u> kg</span>" in en and " lb</span>" in en
    # Ни одного id в показанном и в сохранённом.
    saved = await fresh_db.site_crowd_state()
    assert set(saved) == {identity}
    assert not re.search(r"\b(1\d{3}|5\d{3})\b", saved[identity][0])


@pytest.mark.asyncio
async def test_pounds_are_converted_and_typos_dropped(fresh_db):
    identity = _identity(BENCH)
    for i in range(site_crowd.K_MIN_USERS):
        await _athlete(fresh_db, 2000 + i, identity, [100 * config.LB_PER_KG] * 3, unit="lb")
    await _athlete(fresh_db, 3000, identity, [5000.0] * 3)  # опечатка: в вес не идёт
    await site_crowd.refresh(force=True)
    assert site_crowd.stats_for(identity)["weight"] == [100.0, 100.0, 100.0]


@pytest.mark.asyncio
async def test_alternatives_page_lists_only_swaps_above_k(fresh_db):
    url = next(u for u, _s in seo_pages.EXERCISES if seo_swaps.has_page(u))
    alts = seo_catalog.swaps(_identity(url))
    await _crowd(fresh_db, site_crowd.K_MIN_USERS, alts[0])
    await _crowd(fresh_db, site_crowd.K_MIN_USERS - 1, alts[1], start=4000)
    page = seo_pages.Page("exercise_alt", url)
    assert "id=crowd" not in seo_pages.render(page, "en")
    await site_crowd.refresh(force=True)
    import seed_data

    html = seo_pages.render(page, "en")
    block = html.split("id=crowd>", 1)[1].split("</section>", 1)[0]
    assert seed_data.localized_exercise_name(alts[0], "en") in block
    assert seed_data.localized_exercise_name(alts[1], "en") not in block


# ---------- честный lastmod ----------


@pytest.mark.asyncio
async def test_lastmod_moves_only_when_shown_numbers_change(fresh_db):
    identity = _identity(BENCH)
    await _crowd(fresh_db, site_crowd.K_MIN_USERS, identity)
    await site_crowd.refresh(force=True)
    payload = (await fresh_db.site_crowd_state())[identity][0]
    # Цифры показаны давно: дата — та, старая.
    await fresh_db.save_site_crowd_state({identity: (payload, "2026-11-01")})
    # Мелкая дрожь — ещё одна тренировка на 1 кг тяжелее у одного атлета.
    await _athlete(fresh_db, 9000, identity, [81.0] * 3)
    await site_crowd.refresh(force=True)
    assert (await fresh_db.site_crowd_state())[identity] == (payload, "2026-11-01")
    page = seo_pages.Page("exercise", BENCH)
    assert seo_pages.page_updated(page) == "2026-11-01"
    # Картина правда сдвинулась — дата сегодняшняя.
    for i in range(40):
        await _athlete(fresh_db, 10000 + i, identity, [140.0] * 3)
    await site_crowd.refresh(force=True)
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    assert (await fresh_db.site_crowd_state())[identity][1] == today
    assert seo_pages.page_updated(page) == today
    # Страница без цифр атлетов живёт своей датой.
    assert seo_pages.page_updated(seo_pages.Page("tools")) == seo_pages.SITE_UPDATED


def test_hysteresis_keeps_shown_value_near_a_rounding_edge():
    shown = {"athletes": 20, "weight": [60.0, 80.0, 100.0], "sets": 3}
    raw = {"athletes": 52, "weight": (61.0, 82.4, 98.0), "sets": 3.4}
    assert site_crowd.publish(raw, shown) == shown
    moved = site_crowd.publish({"athletes": 60, "weight": (61.0, 90.0, 98.0), "sets": 4}, shown)
    assert moved["weight"][1] == 90.0 and moved["athletes"] == 50 and moved["sets"] == 4
    # Вниз — сразу: «50+» не остаётся, когда атлетов стало 40.
    assert site_crowd.publish({"athletes": 40}, {"athletes": 50})["athletes"] == 20


# ---------- тонкие страницы: 301, sitemap, noindex ----------


@pytest.mark.asyncio
async def test_old_strength_and_glossary_urls_are_301_within_the_language():
    moved = seo_pages.moved_pages()
    assert "/ru/strength/bench-press/men" in moved and "/en/glossary/rpe" in moved
    responses = await _get_many(list(moved))
    for old, resp in responses.items():
        assert resp.status_code == 301, old
        target = resp.headers["location"]
        assert target == moved[old]
        assert target.split("/")[1] == old.split("/")[1], (old, target)
    assert moved["/en/strength/squat/women"] == "/en/strength/squat#women"
    assert moved["/pt-pt/glossary/rpe"] == "/pt-pt/glossary#rpe"
    # Цель редиректа — живая страница.
    targets = await _get_many(sorted({t.split("#")[0] for t in moved.values()}))
    assert all(r.status_code == 200 for r in targets.values())


@pytest.mark.asyncio
async def test_sitemap_has_no_noindexed_or_redirecting_pages():
    responses = await _get_many(["/sitemap.xml"])
    root = ET.fromstring(responses["/sitemap.xml"].text)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [u.find("s:loc", ns).text.removeprefix(HOST) for u in root.findall("s:url", ns)]
    moved = seo_pages.moved_pages()
    assert not [loc for loc in locs if loc in moved]
    assert not [loc for loc in locs if "/glossary/" in loc]
    assert not [loc for loc in locs if re.search(r"/muscles/[^/]+/[^/]+$", loc)]
    assert not [loc for loc in locs if re.search(r"/strength/[^/]+/(men|women)", loc)]
    assert len(locs) == len(set(locs))
    expected = [p for p in seo_pages.pages() if p.kind not in seo_pages.THIN_KINDS]
    assert len(locs) == len(expected) * len(seo_pages.INDEXED_LANGS)


@pytest.mark.asyncio
async def test_muscle_equipment_pages_stay_open_but_noindex():
    page = next(p for p in seo_pages.pages() if p.kind == "muscle_equip")
    path = seo_pages.url_for(page, "en")
    resp = (await _get_many([path]))[path]
    assert resp.status_code == 200
    assert resp.headers["x-robots-tag"] == seo_pages.NOINDEX and 'name=robots content="noindex' in resp.text
    # Людям ссылка остаётся — со страницы мышцы.
    muscle = seo_pages.render(seo_pages.Page("muscle", page.arg), "en")
    assert f'href="{path}"' in muscle


def test_no_page_links_to_a_redirecting_url():
    moved = set(seo_pages.moved_pages())
    for page in seo_pages.pages():
        if page.kind not in ("glossary", "strength", "strength_lift", "strength_bw", "guide", "tools", "home"):
            continue
        for lang in seo_pages.INDEXED_LANGS:
            hrefs = set(re.findall(r'href="([^"#]+)', seo_pages.render(page, lang)))
            assert not hrefs & moved, (page, lang, hrefs & moved)


# ---------- о проекте ----------


@pytest.mark.asyncio
async def test_about_page_renders_on_every_language_without_a_person():
    paths = [seo_pages.url_for(seo_pages.Page("about"), lang) for lang in seo_pages.LANGS]
    responses = await _get_many(paths)
    for path, resp in responses.items():
        assert resp.status_code == 200, path
        blocks = [json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', resp.text)]
        org = next(b for b in blocks if b["@type"] == "Organization")
        assert "founder" not in org and org["contactPoint"]["url"].startswith(HOST + "/support")
        assert '"Person"' not in resp.text
        assert "free-exercise-db" in resp.text and str(site_crowd.K_MIN_USERS) in resp.text
        sections = re.findall(r"<section class='reveal sub'><h2>", resp.text)
        assert len(sections) >= seo_library.ABOUT_SECTIONS
        # Ссылка на страницу — в подвале любой страницы.
        assert f'href="{path}"' in resp.text
