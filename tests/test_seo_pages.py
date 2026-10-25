"""Публичные страницы для поиска (seo_pages.py), их статика (seo_static.py),
счётчик посещений (site_stats.py) и математика калькуляторов (seo_tools.py).

Страницы гоняются через то же приложение, что поднимает прод
(`mcp_server.build_app`): страница, которая есть в коде, но не доехала до
маршрутов, поисковику отдаст 404. Калькуляторы сверяются с функциями бота и
опубликованными примерами, а их браузерный скрипт — в node на тех же входах
(нет node — пропуск).
"""

import datetime as dt
import io
import json
import re
import shutil
import subprocess
import urllib.robotparser
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import acquisition
import analytics
import config
import exercise_descriptions
import exercise_media
import i18n
import mcp_server
import qr_code
import seed_data
import seo_pages
import seo_static
import seo_tools
import site_stats

HOST = "https://training-log.example.com"
STORE = "https://apps.apple.com/app/apple-store/id6815211995?pt=129483617&mt=8"
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def _cyr(text):
    """Кириллица на странице, кроме автонима «Русский» в переключателе языка
    (автонимы не переводятся — как в кнопке языка у бота)."""
    return _CYRILLIC.search(text.replace("Русский", ""))
UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
UA_ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36"
UA_DESKTOP = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
UA_GOOGLEBOT = ("Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0 Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)")


@pytest.fixture(autouse=True)
def public_url(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", HOST)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(config, "SITE_BASE_URL", "")
    monkeypatch.setattr(config, "APP_STORE_URL", "")
    monkeypatch.setattr(config, "GOOGLE_SITE_VERIFICATION", "")
    monkeypatch.setattr(config, "GOOGLE_SITE_VERIFICATION_FILE", "")
    monkeypatch.setattr(config, "INDEXNOW_KEY", "")
    monkeypatch.setattr(seo_static, "OG_CACHE_DIR", str(tmp_path / "og"))
    seo_static._og_memory.clear()


async def _get_many(paths, headers=None):
    transport = httpx.ASGITransport(app=mcp_server.build_app())
    async with httpx.AsyncClient(transport=transport, base_url=HOST) as client:
        return {p: await client.get(p, headers=headers or {}) for p in paths}


async def _get(path, headers=None):
    return (await _get_many([path], headers))[path]


def _all_urls():
    return [seo_pages.url_for(p, lang) for p in seo_pages.pages() for lang in seo_pages.LANGS]


def _ld_blocks(html):
    return [json.loads(raw) for raw in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


def _ld_types(html):
    return {block["@type"] for block in _ld_blocks(html)}


# ---------- страницы, языки, sitemap ----------


async def test_every_page_is_200_in_every_language_and_only_russian_has_cyrillic():
    responses = await _get_many(_all_urls())
    for page in seo_pages.pages():
        for lang in seo_pages.LANGS:
            url = seo_pages.url_for(page, lang)
            resp = responses[url]
            assert resp.status_code == 200, url
            assert resp.headers["content-type"].startswith("text/html")
            assert resp.headers["content-language"] == seo_pages.hreflang(lang)
            assert f"<html lang={seo_pages.hreflang(lang)}>" in resp.text
            # Ключ каталога вместо текста — значит, строки нет ни в одном языке.
            assert not re.search(r"\bseo\.[a-z]", resp.text.replace("og:", "")), url
            if lang != "ru":
                assert not _cyr(resp.text), (url, _CYRILLIC.findall(resp.text.replace("Русский", ""))[:10])
            else:
                assert _cyr(resp.text), url


async def test_hreflang_canonical_og_and_icons_on_every_page():
    responses = await _get_many(_all_urls())
    for page in seo_pages.pages():
        for lang in seo_pages.LANGS:
            html = responses[seo_pages.url_for(page, lang)].text
            assert f'<link rel=canonical href="{HOST}{seo_pages.url_for(page, lang)}">' in html
            for code in seo_pages.LANGS:
                assert (f'<link rel=alternate hreflang={seo_pages.hreflang(code)} '
                        f'href="{HOST}{seo_pages.url_for(page, code)}">') in html
            assert f'hreflang=x-default href="{HOST}{seo_pages.url_for(page, "en")}"' in html
            # Ровно восемь языков и x-default — без лишних и без повторов.
            assert len(re.findall(r"<link rel=alternate hreflang=", html)) == len(seo_pages.LANGS) + 1
            assert '<meta name=description content="' in html
            assert f'<meta property="og:image" content="{HOST}/og/{seo_pages.lang_segment(lang)}/{page.key}.png">' in html
            assert f'<meta property="og:locale" content="{seo_pages.OG_LOCALES[lang]}">' in html
            assert '<link rel=icon href="/favicon.ico"' in html and "apple-touch-icon" in html and "site.webmanifest" in html


async def test_sitemap_lists_every_page_in_every_language_and_no_embeds():
    resp = await _get("/sitemap.xml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(resp.text)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9", "x": "http://www.w3.org/1999/xhtml"}
    locs = [u.find("s:loc", ns).text for u in root.findall("s:url", ns)]
    assert sorted(locs) == sorted(HOST + u for u in _all_urls())
    assert not [loc for loc in locs if "/embed/" in loc]
    for url in root.findall("s:url", ns):
        langs = {link.get("hreflang") for link in url.findall("x:link", ns)}
        assert langs == {"ru", "en", "es", "pt-BR", "pt-PT", "de", "fr", "tr", "x-default"}


async def test_robots_allows_search_and_ai_crawlers_and_points_to_sitemap():
    responses = await _get_many(["/robots.txt", "/privacy", "/v1/health", "/.well-known/oauth-authorization-server"])
    robots = responses["/robots.txt"]
    assert robots.status_code == 200
    assert f"Sitemap: {HOST}/sitemap.xml" in robots.text
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(robots.text.splitlines())
    for agent in ("Googlebot", "bingbot", "GPTBot", "OAI-SearchBot", "PerplexityBot", "ClaudeBot", "SomeNewBot"):
        assert parser.can_fetch(agent, f"{HOST}/en/tools/1rm"), agent
        assert parser.can_fetch(agent, f"{HOST}/llms.txt"), agent
        assert not parser.can_fetch(agent, f"{HOST}/go/bot?f=home"), agent
    assert responses["/privacy"].status_code == 200
    assert responses["/v1/health"].status_code == 200
    assert responses["/.well-known/oauth-authorization-server"].status_code == 200


async def test_llms_txt_tells_what_it_is_in_both_languages(monkeypatch):
    monkeypatch.setattr(config, "APP_STORE_URL", STORE)
    resp = await _get("/llms.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    text = resp.text
    assert text.startswith("# Athlete Diary")
    assert "## Дневник атлета" in text
    assert f"{HOST}/en/tools/1rm" in text and f"{HOST}/ru/import/hevy" in text
    assert "ct=site_llms" in text and "pt=129483617" in text
    assert f"t.me/{config.BOT_USERNAME}?start=src_seo_llms" in text
    # Числа потолков — из config, не из головы.
    assert f"{config.AI_QUESTION_DAILY_LIMIT} questions" in text
    assert f"{config.AI_VIDEO_DAILY_LIMIT} video reviews" in text
    assert not re.search(r"<[a-z/]", text)


async def test_root_redirects_by_browser_language():
    ru = await _get("/", headers={"Accept-Language": "ru-RU,ru;q=0.9"})
    en = await _get("/")
    assert ru.status_code == 302 and ru.headers["location"] == "/ru"
    assert en.status_code == 302 and en.headers["location"] == "/en"


async def test_unknown_page_is_a_real_404_with_the_coach():
    responses = await _get_many(["/ru/nope", "/en/tools/nope", "/en", "/pt-pt/nope", "/de/tools/nope"])
    for path in ("/ru/nope", "/en/tools/nope", "/pt-pt/nope", "/de/tools/nope"):
        resp = responses[path]
        assert resp.status_code == 404
        assert "<meta name=robots content=noindex>" in resp.text
        assert "rel=canonical" not in resp.text
        assert "coach_idle.png" in resp.text
    assert _cyr(responses["/ru/nope"].text)
    assert not _cyr(responses["/en/tools/nope"].text)
    assert not _cyr(responses["/de/tools/nope"].text)
    assert '<html lang=pt-PT>' in responses["/pt-pt/nope"].text
    assert responses["/en"].status_code == 200


# ---------- кнопки, метки, устройство ----------


def _go_links(html):
    return re.findall(r'href="(/go/(bot|appstore|diary)\?f=([a-z0-9_]+))"', html)


async def test_every_page_links_to_the_bot_through_go_with_a_known_block():
    responses = await _get_many(_all_urls())
    blocks = set()
    for url, resp in responses.items():
        links = _go_links(resp.text)
        assert any(target == "bot" for _href, target, _b in links), url
        for _href, _target, block in links:
            assert block in seo_pages._KNOWN_BLOCKS, (url, block)
            blocks.add(block)
        assert 'href="https://t.me/' not in resp.text, url
    # Каждая метка раздела где-то стоит — иначе её строка в /growth вечно пуста.
    assert blocks == set(seo_pages._BLOCKS.values())


async def test_go_bot_redirects_to_a_short_tagged_link():
    paths = [f"/go/bot?f={b}" for b in seo_pages._KNOWN_BLOCKS] + ["/go/bot?f=../evil", "/go/bot"]
    responses = await _get_many(paths)
    for path, resp in responses.items():
        assert resp.status_code == 302, path
        link = resp.headers["location"]
        match = re.fullmatch(r"https://t\.me/(\w+)\?start=(src_seo_[a-z0-9_]+)", link)
        assert match, link
        payload = match.group(2)
        # Метка доживает до users.source как есть — acquisition её не режет.
        assert acquisition.parse_start_payload(payload).source == payload
        assert len(payload) - len(acquisition.CHANNEL_PREFIX) <= acquisition.MAX_SLUG_LEN
        # Ссылка без схемы — в 48 символов, как у каталогов ботов.
        assert len(link.removeprefix("https://")) <= 48, link
    assert responses["/go/bot?f=../evil"].headers["location"].endswith("src_seo_home")


def test_app_store_link_keeps_provider_token_and_adds_campaign(monkeypatch):
    monkeypatch.setattr(config, "APP_STORE_URL", STORE)
    for block in seo_pages._KNOWN_BLOCKS:
        link = seo_pages.app_store_link(block)
        query = parse_qs(urlsplit(link).query)
        assert query["pt"] == ["129483617"] and query["mt"] == ["8"]
        assert query["ct"] == [f"site_{block}"] and len(query["ct"][0]) <= 40
        assert urlsplit(link).path == "/app/apple-store/id6815211995"
    monkeypatch.setattr(config, "APP_STORE_URL", STORE + "&ct=old")
    assert parse_qs(urlsplit(seo_pages.app_store_link("rm1")).query)["ct"] == ["site_rm1"]


async def test_app_store_button_banner_and_redirect_follow_the_setting(monkeypatch):
    plain = await _get_many(["/en", "/go/appstore?f=rm1"])
    assert "apple-itunes-app" not in plain["/en"].text and "/go/appstore" not in plain["/en"].text
    assert plain["/go/appstore?f=rm1"].headers["location"] == "/go/bot?f=rm1"

    monkeypatch.setattr(config, "APP_STORE_URL", STORE)
    responses = await _get_many(["/ru/import/hevy", "/go/appstore?f=imp_hevy"])
    assert '<meta name="apple-itunes-app" content="app-id=6815211995">' in responses["/ru/import/hevy"].text
    assert 'href="/go/appstore?f=imp_hevy"' in responses["/ru/import/hevy"].text
    location = responses["/go/appstore?f=imp_hevy"].headers["location"]
    assert location == seo_pages.app_store_link("imp_hevy")
    assert "ct=site_imp_hevy" in location and "pt=129483617" in location


def _primary_target(html):
    """Куда ведёт первая главная кнопка в теле страницы (в шапке своя, маленькая)."""
    main = html.split("<main", 1)[1]
    match = re.search(r'<a class="btn primary" href="/go/(bot|appstore)\?', main)
    return match.group(1)


async def _get_as(path, ua):
    return await _get(path, headers={"User-Agent": ua})


def _head_target(html):
    head = re.search(r"<header class=site-head>(.*?)</header>", html, re.S).group(1)
    return re.search(r'head-cta[^"]*" href="/go/(bot|appstore)\?', head).group(1)


def _primary_targets(html):
    """Все главные кнопки в теле страницы (без «запиши в дневник» калькулятора)."""
    main = html.split("<main", 1)[1].split("<footer", 1)[0]
    return set(re.findall(r'<a class="btn primary" href="/go/(bot|appstore)\?', main))


def _alt_targets(html):
    main = html.split("<main", 1)[1].split("<footer", 1)[0]
    return set(re.findall(r'class=alt-link>[^<]*<a href="/go/(bot|appstore)\?', main))


# Политика кнопок: приложение для iPhone — главный продукт, App Store первым
# везде, кроме Android (там приложения нет — главный бот). Язык не влияет.
# Запасной путь — строка помельче под кнопкой, а не вторая такая же кнопка.
_POLICY = {"ios": "appstore", "android": "bot", "desktop": "appstore", "crawler": "appstore"}
_POLICY_PAGES = ("", "/ai-coach", "/import/hevy", "/tools/1rm", "/tools/goal", "/programs", "/exercises",
                 "/vs/hevy", "/alternatives/strong", "/guides", "/compare", "/about")


async def test_device_decides_the_primary_button(monkeypatch):
    monkeypatch.setattr(config, "APP_STORE_URL", STORE)
    uas = {"ios": UA_IPHONE, "android": UA_ANDROID, "desktop": UA_DESKTOP, "crawler": UA_GOOGLEBOT}
    for lang in ("ru", "en"):
        for device, ua in uas.items():
            pages = await _get_many([f"/{lang}{p}" for p in _POLICY_PAGES], {"user-agent": ua})
            want = _POLICY[device]
            other = "bot" if want == "appstore" else "appstore"
            for url, resp in pages.items():
                html = resp.text
                assert _primary_target(html) == want, (url, device)
                assert _primary_targets(html) == {want}, (url, device)
                assert _head_target(html) == want, (url, device)
                # Второй путь виден, но мельче: строкой, а не кнопкой того же размера.
                assert other in _alt_targets(html), (url, device)
                assert not re.search(rf'<a class="btn" href="/go/{other}\?', html), (url, device)
                # Робот видит обе ссылки в любом варианте.
                assert "/go/bot?f=" in html and "/go/appstore?f=" in html, url
                # Accept-Encoding дописывает gzip (seo_http) — страница всё равно зависит от устройства.
                assert "User-Agent" in [v.strip() for v in resp.headers["vary"].split(",")]
                # QR-код в App Store — только на компьютере.
                assert ("<div class=qr" in html) == (device == "desktop"), (url, device)
        desktop = await _get_as(f"/{lang}", UA_DESKTOP)
        iphone = await _get_as(f"/{lang}", UA_IPHONE)
        assert "<div class=qr" in desktop.text and "<div class=qr" not in iphone.text
        assert i18n.t_in(lang, "seo.cta.head_store") in desktop.text.split("</header>", 1)[0]


async def test_bot_page_keeps_the_bot_first_on_every_device(monkeypatch):
    """Страница про сам бот — единственное исключение: бот первой кнопкой,
    App Store строкой под ней; шапка — по общей политике."""
    monkeypatch.setattr(config, "APP_STORE_URL", STORE)
    for ua in (UA_IPHONE, UA_DESKTOP, UA_ANDROID):
        html = (await _get_as("/ru/telegram", ua)).text
        assert _primary_targets(html) == {"bot"}
        assert "appstore" in _alt_targets(html)


async def test_diary_button_follows_the_device_not_the_language(monkeypatch):
    monkeypatch.setattr(config, "APP_STORE_URL", STORE)
    for lang in ("ru", "en"):
        for ua, host in ((UA_DESKTOP, "apps.apple.com"), (UA_IPHONE, "apps.apple.com"), (UA_ANDROID, "t.me")):
            resp = await _get("/go/diary?f=" + seo_pages._BLOCKS["log:goal"],
                              headers={"user-agent": ua, "referer": f"{HOST}/{lang}/tools/goal"})
            assert urlsplit(resp.headers["location"]).hostname == host, (lang, ua)


async def test_desktop_qr_encodes_the_counted_app_store_link(monkeypatch):
    monkeypatch.setattr(config, "APP_STORE_URL", STORE)
    resp = await _get_as("/en/tools/1rm", UA_DESKTOP)
    svg = re.search(r"<div class=qr[^>]*>(<svg.*?</svg>)", resp.text).group(1)
    assert svg == qr_code.svg(f"{HOST}/go/appstore?f=rm1")


async def test_calculator_result_offers_to_log_the_set():
    responses = await _get_many([f"/{lang}/tools/{tool}" for lang in ("ru", "en") for tool in seo_pages.TOOLS])
    for tool in seo_pages.TOOLS:
        en = responses[f"/en/tools/{tool}"].text
        block = seo_pages._BLOCKS[f"log:{tool}"]
        assert f'class="log-cta btn-row"><a class="btn primary" href="/go/diary?f={block}"' in en
    assert "Запиши этот подход — дальше посчитаю сам" in responses["/ru/tools/1rm"].text
    assert "Log this set — I&#x27;ll do the math from here" in responses["/en/tools/1rm"].text


# ---------- встраивание и заголовки безопасности ----------


async def test_only_embeds_may_be_framed():
    responses = await _get_many(["/en/tools/1rm", "/embed/en/1rm", "/embed/ru/1rm", "/ru"])
    for path in ("/en/tools/1rm", "/ru"):
        assert responses[path].headers["content-security-policy"] == "frame-ancestors 'none'"
        assert responses[path].headers["x-frame-options"] == "DENY"
    for path in ("/embed/en/1rm", "/embed/ru/1rm"):
        resp = responses[path]
        assert resp.status_code == 200
        assert resp.headers["content-security-policy"] == "frame-ancestors *"
        assert "x-frame-options" not in resp.headers
        assert "<meta name=robots content=noindex>" in resp.text
        assert "/go/bot" not in resp.text
    assert f'href="{HOST}/en/tools/1rm" target=_blank' in responses["/embed/en/1rm"].text
    assert not _cyr(responses["/embed/en/1rm"].text)
    # На странице калькулятора — готовый код iframe.
    assert f"&lt;iframe src=&quot;{HOST}/embed/en/1rm&quot;" in responses["/en/tools/1rm"].text


# ---------- структурированные данные ----------


async def test_json_ld_is_valid_and_matches_the_page():
    responses = await _get_many(_all_urls())
    for page in seo_pages.pages():
        for lang in seo_pages.LANGS:
            html = responses[seo_pages.url_for(page, lang)].text
            blocks = _ld_blocks(html)
            assert blocks, page
            for block in blocks:
                assert block["@context"] == "https://schema.org"
                assert "aggregateRating" not in json.dumps(block)
            types = _ld_types(html)
            if page.kind != "home":
                crumbs = next(b for b in blocks if b["@type"] == "BreadcrumbList")
                items = crumbs["itemListElement"]
                assert items[0]["item"] == f"{HOST}/{seo_pages.lang_segment(lang)}"
                assert items[-1]["item"] == f"{HOST}{seo_pages.url_for(page, lang)}"
                assert [i["position"] for i in items] == list(range(1, len(items) + 1))
            if page.kind == "program":
                assert "ExercisePlan" in types
            if page.kind == "exercise":
                assert "Article" in types
            # FAQ — у страниц с заданными вопросами, у замен и у пар (вопросы
            # из данных страницы, ради AI-поиска); у остальных его нет.
            if seo_pages.faq_key(page) or page.kind in ("exercise_alt", "pair"):
                faq = next(b for b in blocks if b["@type"] == "FAQPage")
                for question in faq["mainEntity"]:
                    # Вопрос FAQPage обязан быть виден на странице.
                    assert f"<summary>{seo_pages._e(question['name'])}</summary>" in html
                    assert "<" not in question["acceptedAnswer"]["text"]
            else:
                assert "FAQPage" not in types, page


async def test_home_declares_a_free_app_with_real_screenshots(monkeypatch):
    monkeypatch.setattr(config, "APP_STORE_URL", STORE)
    resp = await _get("/en")
    app = next(b for b in _ld_blocks(resp.text) if b["@type"] == "SoftwareApplication")
    assert app["offers"] == {"@type": "Offer", "price": "0", "priceCurrency": "USD"}
    assert app["operatingSystem"] == "iOS, Telegram"
    assert STORE in app["sameAs"]
    assert all(seo_static.site_file(urlsplit(u).path.removeprefix("/site/")) for u in app["screenshot"])
    for name, _w, _h in seo_static.SCREENS["en"]:
        assert f"screens/en-{name}.jpg" in resp.text


async def test_faq_numbers_come_from_config():
    responses = await _get_many(["/ru", "/en"])
    assert f"{config.AI_QUESTION_DAILY_LIMIT} questions" in responses["/en"].text
    assert f"{config.AI_VIDEO_DAILY_LIMIT} video reviews" in responses["/en"].text
    assert f"{config.AI_QUESTION_DAILY_LIMIT} вопрос" in responses["/ru"].text


# ---------- статика ----------


async def test_icons_manifest_fonts_and_static_files():
    responses = await _get_many([
        "/favicon.ico", "/apple-touch-icon.png", "/site.webmanifest?lang=ru", "/site/fonts/pixel.woff",
        "/site/fonts/OFL.txt", "/site/screens/ru-workout.jpg", "/site/gym/scene-en.png",
        "/site/../config.py", "/site/screens/nope.jpg", "/site/fonts/../../db.py",
    ])
    assert responses["/favicon.ico"].status_code == 200
    assert responses["/favicon.ico"].headers["content-type"] == "image/x-icon"
    assert responses["/apple-touch-icon.png"].headers["content-type"] == "image/png"
    manifest = json.loads(responses["/site.webmanifest?lang=ru"].text)
    assert manifest["name"] == "Дневник атлета" and manifest["start_url"] == "/ru"
    assert {i["sizes"] for i in manifest["icons"]} == {"192x192", "512x512"}
    assert responses["/site/fonts/pixel.woff"].headers["content-type"] == "font/woff"
    assert "SIL Open Font License" in responses["/site/fonts/OFL.txt"].text
    assert responses["/site/screens/ru-workout.jpg"].status_code == 200
    assert responses["/site/gym/scene-en.png"].status_code == 200
    for bad in ("/site/../config.py", "/site/screens/nope.jpg", "/site/fonts/../../db.py"):
        assert responses[bad].status_code == 404, bad


def test_pixel_assets_stay_light_and_contain_no_mockups():
    import os

    from PIL import Image

    gym = os.path.join(seo_static.SITE_DIR, "gym")
    files = os.listdir(gym)
    assert not [f for f in files if "mockup" in f]
    # Пиксельный подвал на странице одного языка: сцена + спрайты < 150 КБ.
    for lang in ("ru", "en"):
        total = sum(os.path.getsize(os.path.join(gym, f)) for f in files
                    if not f.startswith("scene-") or f == f"scene-{lang}.png")
        assert total < 150_000, total
        # живая петля — у каждого языка своя доска; браузер качает одну из двух
        for ext in ("mp4", "webm"):
            assert os.path.getsize(os.path.join(gym, f"scene-{lang}.{ext}")) <= 400 * 1024, (lang, ext)
    for rel, w, h, frames, step_ms in seo_static.SPRITES.values():
        with Image.open(os.path.join(seo_static.SITE_DIR, rel)) as im:
            assert im.size == (w * frames, h), rel
        assert frames == 1 or step_ms > 0, rel


async def test_basement_loop_is_served_as_video_with_ranges():
    """Петля подвала отдаётся своим типом, с кэшем на год и по кусками
    (Range): Safari без 206 видео не играет."""
    responses = await _get_many(["/site/gym/scene-en.mp4", "/site/gym/scene-ru.webm"])
    mp4, webm = responses["/site/gym/scene-en.mp4"], responses["/site/gym/scene-ru.webm"]
    assert mp4.status_code == 200 and mp4.headers["content-type"] == "video/mp4"
    assert webm.status_code == 200 and webm.headers["content-type"] == "video/webm"
    assert "immutable" in mp4.headers["cache-control"] and mp4.headers["x-content-type-options"] == "nosniff"
    assert mp4.content[4:8] == b"ftyp" and webm.content[:4] == b"\x1aE\xdf\xa3"
    # faststart: индекс (moov) — до данных (mdat), видео стартует до конца загрузки
    assert mp4.content.index(b"moov") < mp4.content.index(b"mdat")
    part = await _get("/site/gym/scene-en.mp4", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100


async def test_gym_section_plays_the_loop_with_poster_and_reduced_motion():
    responses = await _get_many(["/ru", "/en"])
    for lang in ("ru", "en"):
        html = responses[f"/{lang}"].text
        scene = html[html.index("<figure class=scene>"):]
        scene = scene[:scene.index("</script>")]
        video = scene[:scene.index("</video>")]
        for attr in ("autoplay", "muted", "loop", "playsinline", "preload=metadata", "width=1110", "height=617",
                     f'poster="/site/gym/scene-{lang}.png?v=', "aria-label="):
            assert attr in video, (lang, attr)
        webm = video.index(f'src="/site/gym/scene-{lang}.webm?v=')
        mp4 = video.index(f'src="/site/gym/scene-{lang}.mp4?v=')
        assert webm < mp4 and "type=video/webm" in video and "type=video/mp4" in video
        # без движения — скриншот, а видео на паузе
        assert f'<img class=scene-still src="/site/gym/scene-{lang}.png?v=' in scene and 'alt="' in scene
        assert "prefers-reduced-motion: reduce" in scene and "pause()" in scene
        assert "scene-dog" not in html  # собака теперь бегает в самой петле
        for src in re.findall(r'src="(/site/gym/scene-[^"?]+)', scene):
            assert seo_static.site_file(src.removeprefix("/site/")), src
    import seo_design

    reduced = seo_design.CSS[seo_design.CSS.index("@media (prefers-reduced-motion: reduce) { .scene"):]
    assert ".scene-video { display: none; }" in reduced.split("\n")[0]
    assert ".scene-still { display: block; }" in reduced.split("\n")[0]


async def test_basement_band_walkers_stay_on_the_floor():
    """Полоса «неделя в подвале»: тренер и собака ходят по полу и разворачиваются
    у его края (пол — свой контейнер), а не уходят под доску; атлет с
    гантелями работает; кадры — из лент с шагом приложения."""
    import seo_design

    html = (await _get("/en")).text
    band = html[html.index('<section class="basement'):]
    band = band[:band.index("</section>")]
    floor = band[:band.index("</div>", band.index("basement-floor"))]
    for cls, name in (("lifter", "curl"), ("walker", "coach_walk"), ("walker-dog", "dog")):
        rel, w, h, frames, step_ms = seo_static.SPRITES[name]
        assert f"<span class={cls}>" in floor and rel in floor, cls
        assert f"animation-duration:{step_ms * frames}ms" in floor, cls
    css = seo_design.CSS
    rule = css[css.index(".basement-floor {"):]
    assert "container-type: inline-size" in rule[:rule.index("}")]
    pace = css[css.index("@keyframes pace"):]
    pace = pace[:pace.index("}\n  }")]
    assert "scaleX(-1)" in pace and "var(--path)" in pace


async def test_indexnow_key_file_only_when_set(monkeypatch):
    off = await _get("/0123456789abcdef.txt")
    assert off.status_code == 404
    monkeypatch.setattr(config, "INDEXNOW_KEY", "0123456789abcdef")
    on = await _get("/0123456789abcdef.txt")
    assert on.status_code == 200 and on.text == "0123456789abcdef"
    monkeypatch.setattr(config, "INDEXNOW_KEY", "bad key!")
    assert seo_static.indexnow_key() is None


@contextmanager
def _capture_drawn_text():
    from PIL import ImageDraw

    drawn = []
    original = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        drawn.append(text)
        return original(self, xy, text, *args, **kwargs)

    ImageDraw.ImageDraw.text = spy
    try:
        yield drawn
    finally:
        ImageDraw.ImageDraw.text = original


async def test_og_image_is_a_1200x630_png_with_raw_text():
    from PIL import Image

    with _capture_drawn_text() as drawn:
        responses = await _get_many(["/og/en/vs-hevy.png", "/og/ru/tools-1rm.png", "/og/en/nope.png", "/og/it/home.png",
                                     "/og/pt-pt/home.png", "/og/pt-PT/home.png"])
    resp = responses["/og/en/vs-hevy.png"]
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(resp.content)) as im:
        assert im.size == (1200, 630)
    assert responses["/og/en/nope.png"].status_code == 404
    # Язык — сегментом пути, как у страниц: /og/pt-pt/, не /og/pt-PT/; чужого языка нет.
    assert responses["/og/it/home.png"].status_code == 404
    assert responses["/og/pt-pt/home.png"].status_code == 200
    assert responses["/og/pt-PT/home.png"].status_code == 404
    # Текст в пикселях — сырой: ни HTML-сущностей, ни чужого языка.
    entity = re.compile(r"&#\d+;|&#x[0-9a-fA-F]+;|&amp;|&lt;|&gt;|&quot;")
    assert drawn and not [s for s in drawn if entity.search(s)]
    english = [s for s in drawn if "Hevy" in s]
    assert english and not [s for s in english if _cyr(s)]
    assert any(_cyr(s) for s in drawn)


def test_og_image_text_with_apostrophe_is_drawn_raw(monkeypatch):
    page = seo_pages.Page("tool", "warmup")
    monkeypatch.setattr(seo_pages, "page_h1", lambda p, lang: "Don't skip the warm-up")
    with _capture_drawn_text() as drawn:
        seo_pages.og_png(page, "en")
    assert "Don't" in " ".join(drawn)


def test_og_png_is_cached_in_memory_and_on_disk(monkeypatch):
    calls = []
    real = seo_static.render_og
    monkeypatch.setattr(seo_static, "render_og", lambda *a: calls.append(a) or real(*a))
    first = seo_pages.og_png(seo_pages.Page("home"), "ru")
    second = seo_pages.og_png(seo_pages.Page("home"), "ru")
    seo_static._og_memory.clear()
    third = seo_pages.og_png(seo_pages.Page("home"), "ru")
    assert first == second == third and len(calls) == 1


# ---------- содержимое ----------


async def test_google_verification_meta_and_file(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SITE_VERIFICATION", "abc-123_XYZ")
    monkeypatch.setattr(config, "GOOGLE_SITE_VERIFICATION_FILE", "google0123abcd.html")
    responses = await _get_many(["/en/tools/1rm", "/google0123abcd.html"])
    assert '<meta name="google-site-verification" content="abc-123_XYZ">' in responses["/en/tools/1rm"].text
    assert responses["/google0123abcd.html"].text == "google-site-verification: google0123abcd.html"


def test_verification_file_name_must_look_like_google_file(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SITE_VERIFICATION_FILE", "../etc/passwd")
    assert seo_pages.verification_file_name() is None


async def test_site_base_url_wins_for_absolute_urls_and_falls_back_to_server(monkeypatch):
    fallback = await _get_many(["/en/programs", "/robots.txt"])
    assert f'<link rel=canonical href="{HOST}/en/programs">' in fallback["/en/programs"].text

    monkeypatch.setattr(config, "SITE_BASE_URL", "https://athletediary.app")
    # С адреса сервера страницы теперь уезжают на свой домен (seo_http) — смотрим их там.
    moved = await _get("/en/programs")
    assert moved.status_code == 301 and moved.headers["location"] == "https://athletediary.app/en/programs"
    responses = await _get_many(["/en/programs", "/robots.txt", "/sitemap.xml"], headers={"Host": "athletediary.app"})
    page = responses["/en/programs"].text
    assert '<link rel=canonical href="https://athletediary.app/en/programs">' in page
    assert 'hreflang=ru href="https://athletediary.app/ru/programs"' in page
    assert "Sitemap: https://athletediary.app/sitemap.xml" in responses["/robots.txt"].text
    assert "<loc>https://athletediary.app/ru</loc>" in responses["/sitemap.xml"].text
    # Адрес сервера для Universal Links и OAuth эта настройка не трогает.
    assert config.public_base_url() == HOST


def test_site_base_url_ignores_non_https(monkeypatch):
    monkeypatch.setattr(config, "SITE_BASE_URL", "http://athletediary.app")
    assert config.site_base_url() == HOST


async def test_exercises_and_programs_link_each_other():
    responses = await _get_many(["/en/programs/fullbody3", "/en/exercises/back-squat", "/ru/import/strong",
                                 "/en/exercises/lying-leg-curl"])
    assert 'href="/en/exercises/back-squat"' in responses["/en/programs/fullbody3"].text
    assert 'href="/en/programs/fullbody3"' in responses["/en/exercises/back-squat"].text
    assert 'href="/ru"' in responses["/ru/import/strong"].text
    curl = responses["/en/exercises/lying-leg-curl"].text
    assert 'href="/en/programs/ppl"' in curl
    # На каждой странице упражнения — то, чего нет у других: наша пара кадров и разбор видео.
    assert "lying_leg_curls_r2_1.jpg" in curl and "lying_leg_curls_r2_2.jpg" in curl
    assert "/go/bot?f=ex" in curl


def test_exercise_pages_have_media_and_their_own_text_on_both_languages():
    # Своя страница — только у упражнения со своим текстом; вариации без него
    # показываются карточкой (seo_catalog), а не тонкой страницей.
    assert 120 <= len(seo_pages.EXERCISES) <= 140
    assert len({u for u, _ in seo_pages.EXERCISES}) == len(seo_pages.EXERCISES)
    assert len({s for _, s in seo_pages.EXERCISES}) == len(seo_pages.EXERCISES)
    seen: dict[str, set[str]] = {}
    for _url, slug in seo_pages.EXERCISES:
        identity = seo_pages._identity_for_media_slug(slug)
        assert exercise_media.get_images(identity), slug
        assert exercise_descriptions.EXERCISE_DESCRIPTIONS.get(identity), slug
        assert exercise_descriptions.EXERCISE_DESCRIPTIONS_EN.get(identity), slug
        assert seed_data.localized_exercise_name(identity, "en") != identity
        for lang in ("ru", "en"):
            for part in ("lead", "setup", "mistakes"):
                key = f"seo.ex.{slug}.{part}"
                text = i18n.t_in(lang, key)
                assert text != key
                # Без шаблонной одинаковости: у каждой страницы свой текст.
                assert text not in seen.setdefault(f"{lang}.{part}", set()), (slug, part)
                seen[f"{lang}.{part}"].add(text)
            assert len(i18n.t_in(lang, f"seo.ex.{slug}.mistakes").split("\n")) >= 3


def test_program_pages_cover_the_whole_catalog():
    keys = {p.arg for p in seo_pages.pages() if p.kind == "program"}
    assert keys == set(seed_data.PROGRAM_BY_KEY)


async def test_strength_pages_show_tables_and_lb_on_english_only():
    responses = await _get_many(["/en/strength/bench-press/men", "/ru/strength/deadlift/women", "/en/strength"])
    en = responses["/en/strength/bench-press/men"].text
    ru = responses["/ru/strength/deadlift/women"].text
    assert "In pounds" in en and "В фунтах" not in ru
    row = seo_tools.strength_table("bench", "m")[6]
    assert f"<tr><th scope=row>{row[0]:g}<span class=u>\u00a0kg</span></th>" in en
    assert f"<td class=num>{row[1][2]:g}<span class=u>\u00a0kg</span></td>" in en
    # Методика названа честно: своя шкала, DOTS, допущение о долях, источники.
    for html in (en, ru, responses["/en/strength"].text):
        assert seo_pages.STRENGTH_SOURCES[0] in html and seo_pages.STRENGTH_SOURCES[1] in html
    assert "This is my scale" in en


# ---------- калькуляторы ----------


@pytest.mark.parametrize("formula", ["epley", "brzycki"])
@pytest.mark.parametrize("rpe", [None, 6.0, 8.0, 9.5, 10.0])
def test_e1rm_is_the_bot_formula(formula, rpe):
    for reps in range(1, 16):
        for weight in (20.0, 62.5, 100.0, 142.5):
            assert seo_tools.e1rm(weight, reps, formula, rpe) == analytics.e1rm(weight, reps, formula, rpe)


async def test_removed_plate_calculator_redirects_permanently():
    responses = await _get_many(["/ru/tools/plates", "/en/tools/plates", "/embed/ru/plates", "/embed/en/plates"])
    expected = {"/ru/tools/plates": "/ru/tools", "/en/tools/plates": "/en/tools",
                "/embed/ru/plates": "/embed/ru/1rm", "/embed/en/plates": "/embed/en/1rm"}
    for path, target in expected.items():
        assert responses[path].status_code == 301, path
        assert responses[path].headers["location"] == target
    sitemap = (await _get("/sitemap.xml")).text
    assert "/tools/plates" not in sitemap and "plates" not in seo_pages.TOOLS


# Раскладка на сторону осталась в разминке (подписи «на сторону»), поэтому
# математика блинов и её JS-двойник проверяются по-прежнему.
def test_plates_finds_exact_set_with_fewest_plates():
    assert seo_tools.plates_per_side(80, 20, (25, 20, 15, 10)) == ([20.0, 10.0], 80.0)
    assert seo_tools.plates_per_side(100, 20, seo_tools.DEFAULT_PLATES["kg"]) == ([25.0, 15.0], 100.0)
    assert seo_tools.plates_per_side(101, 20, seo_tools.DEFAULT_PLATES["kg"]) == ([25.0, 15.0], 100.0)
    assert seo_tools.plates_per_side(102.5, 20, seo_tools.DEFAULT_PLATES["kg"]) == ([25.0, 15.0, 1.25], 102.5)
    assert seo_tools.plates_per_side(225, 45, seo_tools.DEFAULT_PLATES["lb"]) == ([45.0, 45.0], 225.0)
    assert seo_tools.plates_per_side(15, 20, (25,)) == ([], 20)


def test_wilks_and_dots_match_published_values():
    # 500кг сумма при весе тела 80кг: классический Wilks ≈ 341.3, DOTS ≈ 344.8.
    assert seo_tools.wilks(500, 80, "m") == pytest.approx(341.35, abs=0.01)
    assert seo_tools.dots(500, 80, "m") == pytest.approx(344.77, abs=0.01)
    # Вес тела за пределами таблицы зажимается.
    assert seo_tools.dots(500, 300, "m") == seo_tools.dots(500, 210, "m")
    assert seo_tools.wilks(300, 20, "f") == seo_tools.wilks(300, 26.51, "f")


def test_ipf_gl_matches_the_ipf_published_examples():
    # Примеры IPF (они же в тестах OpenPowerlifting, goodlift.rs): Инзаркин,
    # ЧМ 2019 в экипировке — 1035кг при 92.04кг; Тёрёнен, ЧМ 2019 по
    # классическому жиму — 122.5кг при 70.5кг.
    assert seo_tools.ipf_gl(1035, 92.04, "m", "sbd", "equipped") == pytest.approx(112.85, abs=0.01)
    assert seo_tools.ipf_gl(122.5, 70.5, "f", "b", "classic") == pytest.approx(96.78, abs=0.01)
    assert seo_tools.ipf_gl(500, 34.9, "m") == 0.0


def test_warmup_ladder_rounds_to_loadable_steps():
    assert seo_tools.warmup_sets(140, 20, 2.5, 5) == [(20, 10), (55.0, 5), (82.5, 3), (110.0, 2), (125.0, 1)]
    # Перед десяткой последней ступени нет.
    assert seo_tools.warmup_sets(140, 20, 2.5, 10) == [(20, 10), (55.0, 5), (82.5, 3), (110.0, 2)]
    # Лёгкий рабочий вес — лесенка короче; не тяжелее грифа — разминки нет.
    assert seo_tools.warmup_sets(30, 20, 2.5, 8) == [(20, 10), (22.5, 2)]
    assert seo_tools.warmup_sets(20, 20, 2.5, 5) == []
    assert seo_tools.warmup_sets(225, 45, 5, 3) == [(45, 10), (90.0, 5), (135.0, 3), (180.0, 2), (200.0, 1)]
    for weight, _reps in seo_tools.warmup_sets(187.5, 20, 2.5, 3):
        assert (weight / 2.5) == int(weight / 2.5)


def test_rpe_chart_is_the_bot_formula_backwards():
    for formula in ("epley", "brzycki"):
        for rpe, row in seo_tools.rpe_chart(formula):
            for reps, pct in zip(seo_tools.RPE_REPS, row, strict=True):
                assert pct == pytest.approx(100 / analytics.e1rm(1.0, reps, formula, rpe))
    assert seo_tools.rpe_percent(1, 10) == 100
    # Чем больше повторов при том же RPE, тем меньше процент.
    row = seo_tools.rpe_chart()[0][1]
    assert row == sorted(row, reverse=True)


def test_strength_levels_are_dots_split_by_lift_share():
    # Доли в сумме — 100% у обоих полов.
    for sex in ("m", "f"):
        assert sum(seo_tools.LIFT_SHARE[sex].values()) == pytest.approx(1.0)
    # Порог уровня, сложенный по трём упражнениям, — ровно этот DOTS суммы.
    for sex, bw in (("m", 82.5), ("f", 63.0)):
        for _name, threshold in seo_tools.LEVELS:
            total = sum(seo_tools.level_weight(threshold, lift, bw, sex) for lift in seo_tools.STRENGTH_LIFTS)
            assert seo_tools.dots(total, bw, sex) == pytest.approx(threshold)
    weight = seo_tools.level_weight(300, "bench", 80, "m")
    assert seo_tools.level_index(seo_tools.lift_score(weight, "bench", 80, "m")) == 2
    assert seo_tools.level_index(seo_tools.lift_score(weight - 1, "bench", 80, "m")) == 1
    assert seo_tools.level_index(10) == -1
    # Таблица растёт с весом тела и с уровнем; фунты — кратны 5.
    table = seo_tools.strength_table("squat", "m")
    assert [row[1][0] for row in table] == sorted(row[1][0] for row in table)
    assert all(row[1] == sorted(row[1]) for row in table)
    assert all(v % 5 == 0 for _bw, values in seo_tools.strength_table("deadlift", "f", "lb") for v in values)


@pytest.mark.skipif(shutil.which("node") is None, reason="node не установлен")
def test_browser_script_matches_python():
    cases_e1rm = [(w, r, f, rpe) for w in (60.0, 100.0) for r in (1, 5, 8, 10, 11, 12, 13)
                  for f in ("epley", "brzycki") for rpe in (None, 8.0, 9.5)]
    cases_plates = [(80, 20, [25, 20, 15, 10]), (101, 20, list(seo_tools.DEFAULT_PLATES["kg"])),
                    (102.5, 20, list(seo_tools.DEFAULT_PLATES["kg"])), (315, 45, list(seo_tools.DEFAULT_PLATES["lb"])),
                    (10, 20, [25])]
    cases_scores = [(500, 80, "m"), (300, 60, "f"), (700, 250, "m"), (200, 20, "f")]
    cases_gl = [(1035, 92.04, "m", "sbd", "equipped"), (122.5, 70.5, "f", "b", "classic"), (600, 90, "m", "sbd", "classic"),
                (350, 60, "f", "sbd", "classic"), (200, 100, "m", "b", "equipped"), (500, 30, "m", "sbd", "classic")]
    cases_warmup = [(140, 20, 2.5, 5), (140, 20, 2.5, 10), (30, 20, 2.5, 8), (20, 20, 2.5, 5), (225, 45, 5, 3), (187.5, 15, 2.5, 1)]
    cases_rpe = [(r, rpe, f) for r in (1, 3, 8, 12) for rpe in (6.0, 7.5, 9.0, 10.0) for f in ("epley", "brzycki")]
    cases_level = [(w, lift, bw, sex) for w in (60.0, 120.0, 210.0) for lift in seo_tools.STRENGTH_LIFTS
                   for bw, sex in ((80.0, "m"), (57.0, "f"), (130.0, "m"))]
    script = seo_tools.calc_js() + f"""
var out = {{
  e1rm: {json.dumps(cases_e1rm)}.map(function (c) {{ return e1rm(c[0], c[1], c[2], c[3]); }}),
  plates: {json.dumps(cases_plates)}.map(function (c) {{ return platesPerSide(c[0], c[1], c[2]); }}),
  scores: {json.dumps(cases_scores)}.map(function (c) {{ return [wilks(c[0], c[1], c[2]), dots(c[0], c[1], c[2])]; }}),
  gl: {json.dumps(cases_gl)}.map(function (c) {{ return ipfGl(c[0], c[1], c[2], c[3], c[4]); }}),
  warmup: {json.dumps(cases_warmup)}.map(function (c) {{ return warmupSets(c[0], c[1], c[2], c[3]); }}),
  rpe: {json.dumps(cases_rpe)}.map(function (c) {{ return rpePercent(c[0], c[1], c[2]); }}),
  level: {json.dumps(cases_level)}.map(function (c) {{ var s = liftScore(c[0], c[1], c[2], c[3]);
    return [s, levelIndex(s), levelWeight(C.levels[2], c[1], c[2], c[3])]; }})
}};
console.log(JSON.stringify(out));
"""
    result = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    for (w, r, f, rpe), got in zip(cases_e1rm, result["e1rm"], strict=True):
        assert got == pytest.approx(analytics.e1rm(w, r, f, rpe), abs=1e-9)
    for (target, bar, plates), got in zip(cases_plates, result["plates"], strict=True):
        expected_plates, expected_total = seo_tools.plates_per_side(target, bar, plates)
        assert got["plates"] == expected_plates
        assert got["total"] == pytest.approx(expected_total)
    for (total, bw, sex), (w, d) in zip(cases_scores, result["scores"], strict=True):
        assert w == pytest.approx(seo_tools.wilks(total, bw, sex))
        assert d == pytest.approx(seo_tools.dots(total, bw, sex))
    for case, got in zip(cases_gl, result["gl"], strict=True):
        assert got == pytest.approx(seo_tools.ipf_gl(*case))
    for case, got in zip(cases_warmup, result["warmup"], strict=True):
        assert [tuple(x) for x in got] == [(pytest.approx(w), r) for w, r in seo_tools.warmup_sets(*case)]
    for (r, rpe, f), got in zip(cases_rpe, result["rpe"], strict=True):
        assert got == pytest.approx(seo_tools.rpe_percent(r, rpe, f))
    for (w, lift, bw, sex), (score, index, level_w) in zip(cases_level, result["level"], strict=True):
        assert score == pytest.approx(seo_tools.lift_score(w, lift, bw, sex))
        assert index == seo_tools.level_index(seo_tools.lift_score(w, lift, bw, sex))
        assert level_w == pytest.approx(seo_tools.level_weight(seo_tools.LEVELS[2][1], lift, bw, sex))


# ---------- счётчик посещений ----------


@pytest.mark.parametrize("ua, expected", [
    (UA_IPHONE, ("ios", None)),
    ("Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X)", ("ios", None)),
    (UA_ANDROID, ("android", None)),
    (UA_DESKTOP, ("desktop", None)),
    (UA_GOOGLEBOT, ("bot", "Googlebot")),
    ("Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)", ("bot", "bingbot")),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; +https://openai.com/gptbot)", ("bot", "GPTBot")),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot)", ("bot", "OAI-SearchBot")),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)", ("bot", "PerplexityBot")),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)", ("bot", "ClaudeBot")),
    ("Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)", ("bot", "YandexBot")),
    ("Mozilla/5.0 (compatible; MistralAI-User/1.0; +https://docs.mistral.ai/robots)", ("bot", "MistralAI-User")),
    ("DuckAssistBot/1.2; (+http://duckduckgo.com/duckassistbot.html)", ("bot", "DuckAssistBot")),
    ("curl/8.4.0", ("bot", "other")),
    ("", ("bot", "unknown")),
])
def test_user_agent_classification(ua, expected):
    assert site_stats.classify_ua(ua) == expected


@pytest.mark.parametrize("referer, expected", [
    ("https://www.google.com/search?q=1rm+calculator&secret=1", "google"),
    ("https://www.google.ru/", "google"),
    ("https://yandex.ru/search/?text=x", "yandex"),
    ("https://chatgpt.com/c/abc", "chatgpt"),
    ("https://www.perplexity.ai/search?q=x", "perplexity"),
    ("https://gemini.google.com/app/abc", "gemini"),
    ("https://copilot.microsoft.com/chats/x", "copilot"),
    ("https://grok.com/c/x", "grok"),
    ("https://chat.deepseek.com/a/chat/s/x", "deepseek"),
    ("https://t.me/somechannel/12", "telegram"),
    ("https://athletediary.app/ru/tools", "internal"),
    ("https://news.ycombinator.com/item?id=1", "news.ycombinator.com"),
    (None, "direct"),
])
def test_referrer_is_reduced_to_a_domain(referer, expected):
    assert site_stats.referrer_name(referer, ("athletediary.app",)) == expected


async def test_visits_and_clicks_are_counted_without_ip_or_full_referrer(fresh_db, monkeypatch, caplog):
    monkeypatch.setattr(config, "APP_STORE_URL", STORE)
    caplog.set_level("INFO", logger="site_stats")
    transport = httpx.ASGITransport(app=mcp_server.build_app(), client=("203.0.113.7", 5555))
    async with httpx.AsyncClient(transport=transport, base_url=HOST) as client:
        await client.get("/ru/tools/1rm?utm_source=Habr!", headers={
            "User-Agent": UA_IPHONE, "Referer": "https://www.google.com/search?q=1rm+secret"})
        await client.get("/en", headers={"User-Agent": UA_GOOGLEBOT})
        await client.get("/embed/en/1rm", headers={"User-Agent": UA_DESKTOP, "Referer": "https://blog.example.org/post"})
        await client.get("/go/bot?f=rm1_log", headers={"User-Agent": UA_IPHONE, "Referer": f"{HOST}/ru/tools/1rm"})
        await client.get("/go/appstore?f=rm1", headers={"User-Agent": UA_DESKTOP})
    cur = await fresh_db.conn().execute("SELECT * FROM site_events ORDER BY id")
    rows = [dict(r) for r in await cur.fetchall()]
    columns = set(rows[0])
    assert columns == {"id", "kind", "page", "lang", "referrer", "device", "crawler", "utm", "target", "created_at"}
    dump = json.dumps(rows, ensure_ascii=False)
    assert "203.0.113.7" not in dump and "secret" not in dump and "Mozilla" not in dump
    view, crawl, embed, click_bot, click_store = rows
    assert (view["kind"], view["page"], view["lang"], view["referrer"], view["device"], view["utm"]) == (
        "view", "/ru/tools/1rm", "ru", "google", "ios", "habr")
    assert (crawl["device"], crawl["crawler"]) == ("bot", "Googlebot")
    assert (embed["kind"], embed["referrer"]) == ("embed", "blog.example.org")
    assert (click_bot["kind"], click_bot["target"], click_bot["page"]) == ("click", "bot", "/ru/tools/1rm")
    assert (click_store["target"], click_store["page"]) == ("appstore", "block:rm1")
    logged = "\n".join(r.getMessage() for r in caplog.records if r.name == "site_stats")
    assert "site view /ru/tools/1rm lang=ru ref=google device=ios" in logged
    assert "203.0.113.7" not in logged

    stats = await fresh_db.site_stats(30)
    assert stats["views"] == 1
    assert stats["crawlers"] == [("Googlebot", 1)]
    assert stats["clicks"] == {"bot": 1, "appstore": 1}
    assert stats["embeds"] == 1
    block = site_stats.format_growth_block(stats, "30 дн.", [{"source": "src_seo_rm1_log", "users": 2, "activated": 1}])
    assert block.startswith("🌐 <b>САЙТ за 30 дн.</b>")
    assert "Клики: в бота 1, в App Store 1 — 200.0% от просмотров" in block
    assert "Роботы: Googlebot 1" in block
    assert "Пришли в бота по ссылкам сайта: 2, из них первую тренировку закрыли 1 (rm1_log 2)" in block

    feed = [r for r in await fresh_db.site_visits_feed() if r["crawler"] is None]
    assert len(feed) == 1
    line = site_stats.format_feed_line(feed[0])
    assert re.fullmatch(r"🌐 \d\d\.\d\d \d\d:\d0 · /ru/tools/1rm · google · iOS", line)


async def test_growth_counts_visits_from_ai_assistants(fresh_db):
    """GEO: переход человека из ответа ассистента — по источнику или по
    utm_source, который ассистент сам дописал (ChatGPT — chatgpt.com); и
    отдельно — роботы, открывшие страницу по вопросу человека."""
    await fresh_db.record_site_event("view", "/en", "en", "chatgpt", "ios")
    await fresh_db.record_site_event("view", "/en", "en", "direct", "ios", None, "chatgpt.com")
    await fresh_db.record_site_event("view", "/ru", "ru", "gemini", "desktop")
    await fresh_db.record_site_event("view", "/ru", "ru", "google", "desktop", None, "habr")
    await fresh_db.record_site_event("view", "/en", "en", "direct", "bot", "ChatGPT-User")
    await fresh_db.record_site_event("view", "/en", "en", "direct", "bot", "GPTBot")
    stats = await fresh_db.site_stats(7)
    assert stats["ai_sources"] == [("chatgpt", 2), ("gemini", 1)]
    block = site_stats.format_growth_block(stats, "7 дн.", [])
    assert "Из AI-ассистентов: 3 (chatgpt 2, gemini 1)" in block
    assert "AI открыл страницу по вопросу человека: ChatGPT-User 1" in block
    assert site_stats.ai_source("google", "habr") is None


async def test_feed_folds_repeated_views_into_one_line(fresh_db):
    for _ in range(3):
        await fresh_db.record_site_event("view", "/en", "en", "direct", "desktop")
    await fresh_db.record_site_event("view", "/en", "en", "direct", "bot", "GPTBot")
    await fresh_db.record_site_event("view", "/en/guides", "en", "direct", "bot", "GPTBot")
    await fresh_db.record_site_event("view", "/ru", "ru", "direct", "bot", "ClaudeBot")
    feed = await fresh_db.site_visits_feed()
    assert await fresh_db.count_site_visit_buckets() == len(feed) == 3
    assert feed[0]["n"] == 3
    assert site_stats.format_feed_line(feed[0]).endswith("· компьютер ×3")
    bots = [site_stats.format_feed_line(r) for r in feed[1:]]
    assert any(re.fullmatch(r"🤖 \d\d\.\d\d \d\d:\d0 · GPTBot · 2 стр\.", b) for b in bots)
    assert any(b.endswith("· ClaudeBot · 1 стр.") for b in bots)
    assert "Пришли в бота по ссылкам сайта: 0" in site_stats.format_growth_block(await fresh_db.site_stats(7), "7 дн.", [])


async def test_old_site_events_are_pruned(fresh_db):
    await fresh_db.record_site_event("view", "/en", "en", "direct", "desktop")
    old = (dt.date.today() - dt.timedelta(days=config.ANALYTICS_RETENTION_DAYS + 2)).isoformat()
    await fresh_db.conn().execute("UPDATE site_events SET created_at = ?", (old,))
    assert await fresh_db.prune_old_site_events(config.ANALYTICS_RETENTION_DAYS) == 1


async def test_page_survives_when_the_counter_cannot_write():
    # Базы нет (тесты страниц выше гоняют без неё) — счётчик молчит, страница живёт.
    assert await site_stats.record("view", "/en", "en", user_agent=UA_DESKTOP, referer=None) == ("desktop", None, "direct")


def test_activity_screen_has_the_site_button():
    import keyboards

    with i18n.use_lang("ru"):
        kb = keyboards.admin_activity_users_keyboard([], 0, False)
        feed = keyboards.admin_site_feed_keyboard(0, True)
    assert "admin:acs:0" in [b.callback_data for row in kb.inline_keyboard for b in row]
    assert [b.callback_data for row in feed.inline_keyboard for b in row] == ["admin:acs:1", "admin:acb"]


@pytest.mark.parametrize("lang, needle, ip", [
    ("ru", "не ставит cookies", "IP-адрес"), ("en", "sets no cookies", "IP address"),
])
def test_privacy_policy_describes_the_site_counter(lang, needle, ip):
    import pathlib

    text = pathlib.Path(f"legal/privacy.{lang}.html").read_text(encoding="utf-8")
    assert needle in text and "athletediary.app" in text and ip in text


def test_indexnow_script_builds_payload_from_the_sitemap():
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location("indexnow_ping", pathlib.Path("scripts/indexnow_ping.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    urls = module.sitemap_urls(seo_pages.sitemap_xml())
    assert len(urls) == len(_all_urls())
    body = module.payload("https://athletediary.app", "0123456789abcdef", urls)
    assert body["host"] == "athletediary.app"
    assert body["keyLocation"] == "https://athletediary.app/0123456789abcdef.txt"
    assert body["urlList"] == urls
