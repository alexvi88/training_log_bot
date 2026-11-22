"""Какой поисковик привёл человека с сайта: буква в sessionStorage, `&e=` у
кнопок /go/*, метка бота `src_seo_<блок>_<буква>`, клик с источником
`engine:<буква>` и разбор по поисковикам в /growth (site_stats.ENGINE_JS,
seo_pages.bot_payload / seo_source_parts)."""

import json
import re
import shutil
import subprocess
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import acquisition
import config
import mcp_server
import seo_pages
import seo_static
import site_stats

HOST = "https://training-log.example.com"
STORE = "https://apps.apple.com/app/apple-store/id6815211995?pt=129483617&mt=8"
UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
UA_ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36"
ENGINES = tuple(site_stats.SEARCH_ENGINES)


@pytest.fixture(autouse=True)
def public_url(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", HOST)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(config, "SITE_BASE_URL", "")
    monkeypatch.setattr(config, "APP_STORE_URL", "")
    monkeypatch.setattr(seo_static, "OG_CACHE_DIR", str(tmp_path / "og"))


async def _get_many(paths, headers=None):
    transport = httpx.ASGITransport(app=mcp_server.build_app())
    async with httpx.AsyncClient(transport=transport, base_url=HOST) as client:
        return {p: await client.get(p, headers=headers or {}) for p in paths}


def test_engine_letters_are_the_agreed_ones():
    assert ENGINES == ("g", "y", "b", "d", "o", "n")


@pytest.mark.parametrize("block", sorted(seo_pages._KNOWN_BLOCKS))
def test_payload_with_engine_fits_every_limit_and_parses_back(block):
    old = seo_pages.bot_payload(block)
    assert old == f"src_seo_{block}"
    assert seo_pages.seo_source_parts(old) == (block, None)
    for engine in ENGINES:
        payload = seo_pages.bot_payload(block, engine)
        assert payload.endswith(f"_{engine}") and payload.startswith("src_seo_")
        # Метка доживает до users.source как есть — acquisition её не режет.
        assert acquisition.parse_start_payload(payload).source == payload
        assert len(payload) - len(acquisition.CHANNEL_PREFIX) <= acquisition.MAX_SLUG_LEN
        # Ссылка без схемы — в 48 символов, как у каталогов ботов.
        assert len(f"t.me/{config.BOT_USERNAME}?start={payload}") <= 48, payload
        assert seo_pages.seo_source_parts(payload) == (block, engine)


@pytest.mark.parametrize("bad", [None, "", "x", "G", "gg", "g;", "../", "Яндекс"])
def test_unknown_engine_keeps_the_old_payload(bad):
    assert seo_pages.bot_payload("ex", bad) == "src_seo_ex"
    assert site_stats.valid_engine(bad) is None


def test_aliases_do_not_collide_and_no_block_looks_engine_tagged():
    aliases = seo_pages._ENGINE_BLOCK_ALIASES
    assert set(aliases) <= seo_pages._KNOWN_BLOCKS
    assert len(set(aliases.values())) == len(aliases)
    assert not set(aliases.values()) & seo_pages._KNOWN_BLOCKS
    for code in seo_pages._KNOWN_BLOCKS | set(aliases.values()):
        # Иначе старая метка без буквы читалась бы как метка с поисковиком.
        assert not re.search(r"_[gybdon]$", code), code
    # Синоним — только там, где без него ссылка не влезает.
    for block, alias in aliases.items():
        assert len(f"t.me/{config.BOT_USERNAME}?start=src_seo_{block}_g") > 48, block
        assert len(alias) < len(block)


def test_seo_source_parts_rejects_foreign_sources():
    assert seo_pages.seo_source_parts("src_boto") is None
    assert seo_pages.seo_source_parts("organic") is None
    assert seo_pages.seo_source_parts("src_seo_vs_fbod_y") == ("vs_fitbod", "y")
    # Неизвестный блок с похожим хвостом — не метка поисковика.
    assert seo_pages.seo_source_parts("src_seo_nosuch_g") == ("nosuch_g", None)


def test_source_titles_name_the_engine():
    title = acquisition._source_title
    assert title("src_seo_ex_g") == "🌐 Сайт · ex · Google"
    assert title("src_seo_ex_y") == "🌐 Сайт · ex · Яндекс"
    assert title("src_seo_alt_str_b") == "🌐 Сайт · alt_strng · Bing"
    assert title("src_seo_home_d") == "🌐 Сайт · home · DuckDuckGo"
    assert title("src_seo_rm1_o") == "🌐 Сайт · rm1 · другой сайт"
    assert title("src_seo_rm1_n") == "🌐 Сайт · rm1 · без источника"
    # Старые метки без буквы — как раньше.
    assert title("src_seo_ex") == "📣 seo_ex"
    assert title("src_boto") == "📣 boto"


def _stats(**extra):
    return {"views": 10, "clicks": {"bot": 4}, "crawlers": [], "pages": [], "referrers": [], "devices": [],
            "embeds": 0, "confirmed_since": "2026-11-01", **extra}


def test_growth_groups_seo_signups_by_block_and_engine():
    rows = [
        {"source": "src_seo_ex_g", "users": 2, "activated": 1},
        {"source": "src_seo_ex_y", "users": 1, "activated": 0},
        {"source": "src_seo_rm1_log", "users": 2, "activated": 1},
        {"source": "src_seo_ex", "users": 1, "activated": 0},
        {"source": "src_seo_vs_fbod_g", "users": 1, "activated": 1},
    ]
    assert site_stats.group_seo_signups(rows) == [
        ("ex", {"g": 2, "y": 1, "?": 1}), ("rm1_log", {"?": 2}), ("vs_fitbod", {"g": 1})]
    block = site_stats.format_growth_block(_stats(), "30 дн.", rows)
    assert ("Пришли в бота по ссылкам сайта: 7, из них первую тренировку закрыли 3 "
            "(ex 4 (g 2, y 1, ? 1), rm1_log 2, vs_fitbod 1 (g 1))") in block
    assert "Пришли по поисковику: Google 3, поисковик неизвестен 3, Яндекс 1" in block
    assert "Буквы: g — Google, y — Яндекс" in block


def test_growth_without_engines_reads_as_before():
    rows = [{"source": "src_seo_rm1_log", "users": 2, "activated": 1}]
    block = site_stats.format_growth_block(_stats(click_referrers=[("internal", 4)]), "30 дн.", rows)
    assert "Пришли в бота по ссылкам сайта: 2, из них первую тренировку закрыли 1 (rm1_log 2)" in block
    assert "поисковик" not in block.lower() and "Буквы" not in block


async def test_page_carries_the_engine_script():
    resp = (await _get_many(["/ru/exercises"]))["/ru/exercises"]
    assert site_stats.ENGINE_JS in resp.text
    assert "sessionStorage" in site_stats.ENGINE_JS and "cookie" not in site_stats.ENGINE_JS
    # Ссылки сервер отдаёт как раньше — букву дописывает браузер.
    assert re.search(r'href="/go/bot\?f=[a-z0-9_]+"', resp.text)


async def test_go_bot_uses_the_engine_and_ignores_garbage():
    paths = ["/go/bot?f=ex&e=y", "/go/bot?f=ex&e=zz", "/go/bot?f=ex", "/go/bot?f=vs_fitbod&e=g",
             "/go/diary?f=rm1_log&e=g"]
    responses = await _get_many(paths, headers={"User-Agent": UA_ANDROID})
    loc = {p: r.headers["location"] for p, r in responses.items()}
    assert loc["/go/bot?f=ex&e=y"].endswith("?start=src_seo_ex_y")
    assert loc["/go/bot?f=ex&e=zz"].endswith("?start=src_seo_ex")
    assert loc["/go/bot?f=ex"].endswith("?start=src_seo_ex")
    assert loc["/go/bot?f=vs_fitbod&e=g"].endswith("?start=src_seo_vs_fbod_g")
    assert loc["/go/diary?f=rm1_log&e=g"].endswith("?start=src_seo_rm1_log_g")


async def test_app_store_link_stays_without_engine(monkeypatch):
    monkeypatch.setattr(config, "APP_STORE_URL", STORE)
    resp = (await _get_many(["/go/appstore?f=ex&e=g"], headers={"User-Agent": UA_IPHONE}))["/go/appstore?f=ex&e=g"]
    assert parse_qs(urlsplit(resp.headers["location"]).query)["ct"] == ["site_ex"]
    # Android: страница-развилка несёт букву дальше, в бота и в App Store.
    gate = (await _get_many(["/go/appstore?f=ex&e=g"], headers={"User-Agent": UA_ANDROID}))["/go/appstore?f=ex&e=g"]
    assert 'href="/go/bot?f=ex&amp;e=g"' in gate.text
    assert "/go/appstore?f=ex&amp;android=1&amp;e=g" in gate.text
    # Без App Store — в бота, с той же буквой.
    monkeypatch.setattr(config, "APP_STORE_URL", "")
    plain = (await _get_many(["/go/appstore?f=ex&e=y"]))["/go/appstore?f=ex&e=y"]
    assert plain.headers["location"] == "/go/bot?f=ex&e=y"


async def test_click_records_the_engine_and_growth_shows_it(fresh_db):
    await _get_many(["/go/bot?f=ex&e=y", "/go/bot?f=ex&e=g", "/go/bot?f=ex&e=g", "/go/bot?f=ex"],
                    headers={"User-Agent": UA_IPHONE, "Referer": f"{HOST}/ru/exercises"})
    cur = await fresh_db.conn().execute("SELECT referrer FROM site_events WHERE kind = 'click' ORDER BY id")
    assert [r[0] for r in await cur.fetchall()] == ["engine:y", "engine:g", "engine:g", "internal"]
    await fresh_db.record_site_event("seen", "/ru/exercises", "ru", "google", "ios")
    stats = await fresh_db.site_stats(30)
    assert stats["clicks"] == {"bot": 4}
    block = site_stats.format_growth_block(stats, "30 дн.", [])
    assert "Клики по поисковику: Google 2, Яндекс 1, поисковик неизвестен 1" in block


_HARNESS = r"""
const vm = require('vm');
const js = process.argv[1];
function anchor(href) {
  const a = {h: href, getAttribute() { return this.h; }, setAttribute(_k, v) { this.h = v; },
             closest() { return /^\/go\//.test(this.h) ? this : null; }};
  return a;
}
function run(c) {
  const store = Object.assign({}, c.store || {});
  const links = c.links.map(anchor);
  let listener = null;
  const sessionStorage = c.broken ? {getItem() { throw new Error('x'); }, setItem() { throw new Error('x'); }}
    : {getItem(k) { return k in store ? store[k] : null; }, setItem(k, v) { store[k] = String(v); }};
  const document = {referrer: c.referrer, querySelectorAll() { return links; },
                    addEventListener(_t, fn) { listener = fn; }};
  vm.runInNewContext(js, {document, sessionStorage, URL, location: {hostname: 'athletediary.app'}});
  const late = anchor('/go/bot?f=rm1_log');
  if (listener) listener({target: late});
  return {links: links.map(a => a.h), late: late.h, store};
}
const cases = JSON.parse(process.argv[2]);
console.log(JSON.stringify(cases.map(run)));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node не установлен")
def test_page_script_remembers_the_first_engine_and_rewrites_go_links():
    links = ["/go/bot?f=ex", "/go/appstore?f=ex", "/go/diary?f=rm1_log", "/ru/tools", "/go/bot?f=ex&e=y"]
    cases = [
        {"referrer": "https://www.google.com/", "links": links},
        {"referrer": "https://www.google.co.uk/", "links": links},
        {"referrer": "https://yandex.ru/search/?text=1", "links": links},
        {"referrer": "https://ya.ru/", "links": links},
        {"referrer": "https://www.bing.com/", "links": links},
        {"referrer": "https://duckduckgo.com/", "links": links},
        {"referrer": "https://news.ycombinator.com/", "links": links},
        {"referrer": "", "links": links},
        # Переход по своим страницам не перезаписывает первую букву…
        {"referrer": "https://athletediary.app/ru", "links": links, "store": {"se": "y"}},
        # …а без неё своя страница буквы не даёт.
        {"referrer": "https://www.athletediary.app/ru", "links": links},
        # Хранилище закрыто — буква всё равно доезжает до ссылок этой страницы.
        {"referrer": "https://www.google.com/", "links": links, "broken": True},
    ]
    out = json.loads(subprocess.run(["node", "-e", _HARNESS, site_stats.ENGINE_JS, json.dumps(cases)],
                                    capture_output=True, text=True, check=True).stdout)
    expected = ["g", "g", "y", "y", "b", "d", "o", "n", "y", None, "g"]
    for case, result, letter in zip(cases, out, expected, strict=True):
        if letter is None:
            assert result["links"] == links and result["late"] == "/go/bot?f=rm1_log", case
            assert result["store"] == {}
            continue
        assert result["links"] == [f"/go/bot?f=ex&e={letter}", f"/go/appstore?f=ex&e={letter}",
                                   f"/go/diary?f=rm1_log&e={letter}", "/ru/tools", "/go/bot?f=ex&e=y"], case
        assert result["late"] == f"/go/bot?f=rm1_log&e={letter}", case
        if not case.get("broken"):
            assert result["store"] == {"se": letter}, case
