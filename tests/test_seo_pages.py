"""Публичные страницы для поиска (seo_pages.py) и математика калькуляторов
(seo_tools.py).

Страницы гоняются через то же приложение, что поднимает прод
(`mcp_server.build_app`): страница, которая есть в коде, но не доехала до
маршрутов, поисковику отдаст 404. Калькуляторы сверяются с функциями бота, а
их браузерный скрипт — в node на тех же входах (нет node — пропуск).
"""

import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

import httpx
import pytest

import acquisition
import analytics
import config
import exercise_descriptions
import exercise_media
import mcp_server
import seed_data
import seo_pages
import seo_tools

HOST = "https://training-log.example.com"
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


@pytest.fixture(autouse=True)
def public_url(monkeypatch):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", HOST)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(config, "SITE_BASE_URL", "")
    monkeypatch.setattr(config, "APP_STORE_URL", "")
    monkeypatch.setattr(config, "GOOGLE_SITE_VERIFICATION", "")
    monkeypatch.setattr(config, "GOOGLE_SITE_VERIFICATION_FILE", "")


async def _get_many(paths, headers=None):
    transport = httpx.ASGITransport(app=mcp_server.build_app())
    async with httpx.AsyncClient(transport=transport, base_url=HOST) as client:
        return {p: await client.get(p, headers=headers or {}) for p in paths}


async def _get(path, headers=None):
    return (await _get_many([path], headers))[path]


def _all_urls():
    return [seo_pages.url_for(p, lang) for p in seo_pages.pages() for lang in seo_pages.LANGS]


async def test_every_page_is_200_in_both_languages_and_english_has_no_cyrillic():
    responses = await _get_many(_all_urls())
    for page in seo_pages.pages():
        for lang in seo_pages.LANGS:
            url = seo_pages.url_for(page, lang)
            resp = responses[url]
            assert resp.status_code == 200, url
            assert resp.headers["content-type"].startswith("text/html")
            assert resp.headers["content-language"] == lang
            assert f"<html lang={lang}>" in resp.text
            # Ключ каталога вместо текста — значит, строки нет ни в одном языке.
            assert not re.search(r"\bseo\.[a-z]", resp.text.replace("og:", "")), url
            if lang == "en":
                assert not _CYRILLIC.search(resp.text), (url, _CYRILLIC.findall(resp.text)[:10])
            else:
                assert _CYRILLIC.search(resp.text), url


async def test_hreflang_and_canonical_point_at_the_pair():
    responses = await _get_many(_all_urls())
    for page in seo_pages.pages():
        for lang in seo_pages.LANGS:
            html = responses[seo_pages.url_for(page, lang)].text
            assert f'<link rel=canonical href="{HOST}{seo_pages.url_for(page, lang)}">' in html
            for code in seo_pages.LANGS:
                assert f'<link rel=alternate hreflang={code} href="{HOST}{seo_pages.url_for(page, code)}">' in html
            assert f'hreflang=x-default href="{HOST}{seo_pages.url_for(page, "en")}"' in html
            assert '<meta name=description content="' in html
            assert 'property="og:title"' in html and 'property="og:image"' in html


async def test_sitemap_lists_every_page_in_both_languages():
    resp = await _get("/sitemap.xml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(resp.text)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9", "x": "http://www.w3.org/1999/xhtml"}
    locs = [u.find("s:loc", ns).text for u in root.findall("s:url", ns)]
    assert sorted(locs) == sorted(HOST + u for u in _all_urls())
    for url in root.findall("s:url", ns):
        langs = {link.get("hreflang") for link in url.findall("x:link", ns)}
        assert langs == {"ru", "en", "x-default"}


async def test_robots_allows_all_and_points_to_sitemap_without_breaking_neighbours():
    responses = await _get_many(["/robots.txt", "/privacy", "/v1/health", "/.well-known/oauth-authorization-server"])
    robots = responses["/robots.txt"]
    assert robots.status_code == 200
    assert "User-agent: *\nAllow: /" in robots.text
    assert f"Sitemap: {HOST}/sitemap.xml" in robots.text
    assert responses["/privacy"].status_code == 200
    assert responses["/v1/health"].status_code == 200
    assert responses["/.well-known/oauth-authorization-server"].status_code == 200


async def test_root_redirects_by_browser_language():
    ru = await _get("/", headers={"Accept-Language": "ru-RU,ru;q=0.9"})
    en = await _get("/")
    assert ru.status_code == 302 and ru.headers["location"] == "/ru"
    assert en.status_code == 302 and en.headers["location"] == "/en"


async def test_every_bot_link_carries_a_short_seo_tag():
    responses = await _get_many(_all_urls())
    blocks = set()
    for url, resp in responses.items():
        links = re.findall(r'href="(https://t\.me/[^"]+)"', resp.text)
        assert links, url
        for link in links:
            match = re.fullmatch(r"https://t\.me/(\w+)\?start=(src_seo_[a-z0-9_]+)", link)
            assert match, link
            payload = match.group(2)
            # Метка доживает до users.source как есть — acquisition её не режет.
            assert acquisition.parse_start_payload(payload).source == payload
            assert len(payload) - len(acquisition.CHANNEL_PREFIX) <= acquisition.MAX_SLUG_LEN
            # Ссылка без схемы — в 48 символов, как у каталогов ботов.
            assert len(link.removeprefix("https://")) <= 48, link
            blocks.add(payload)
    assert len(blocks) == len(set(seo_pages._BLOCKS.values()))


async def test_app_store_button_and_smart_banner_follow_the_setting(monkeypatch):
    plain = await _get("/en")
    assert "apple-itunes-app" not in plain.text and "apps.apple.com" not in plain.text

    monkeypatch.setattr(config, "APP_STORE_URL", "https://apps.apple.com/app/apple-store/id123456789?pt=1&mt=8")
    resp = await _get("/ru/import/hevy")
    assert '<meta name="apple-itunes-app" content="app-id=123456789">' in resp.text
    assert 'href="https://apps.apple.com/app/apple-store/id123456789?pt=1&amp;mt=8"' in resp.text


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
    responses = await _get_many(["/en/programs", "/robots.txt", "/sitemap.xml"])
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
    responses = await _get_many(["/en/programs/fullbody3", "/en/exercises/back-squat", "/ru/import/strong"])
    assert 'href="/en/exercises/back-squat"' in responses["/en/programs/fullbody3"].text
    assert 'href="/en/programs/fullbody3"' in responses["/en/exercises/back-squat"].text
    assert 'href="/ru"' in responses["/ru/import/strong"].text


def test_exercise_pages_have_media_and_text_on_both_languages():
    import i18n

    assert 30 <= len(seo_pages.EXERCISES) <= 40
    assert len({u for u, _ in seo_pages.EXERCISES}) == len(seo_pages.EXERCISES)
    for _url, slug in seo_pages.EXERCISES:
        identity = seo_pages._identity_for_media_slug(slug)
        assert exercise_media.get_images(identity), slug
        assert exercise_descriptions.EXERCISE_DESCRIPTIONS.get(identity), slug
        assert exercise_descriptions.EXERCISE_DESCRIPTIONS_EN.get(identity), slug
        for lang in ("ru", "en"):
            for part in ("lead", "setup", "mistakes"):
                key = f"seo.ex.{slug}.{part}"
                assert i18n.t_in(lang, key) != key
        assert seed_data.localized_exercise_name(identity, "en") != identity


def test_program_pages_cover_the_whole_catalog():
    keys = {p.arg for p in seo_pages.pages() if p.kind == "program"}
    assert keys == set(seed_data.PROGRAM_BY_KEY)


# ---------- калькуляторы ----------


@pytest.mark.parametrize("formula", ["epley", "brzycki"])
@pytest.mark.parametrize("rpe", [None, 6.0, 8.0, 9.5, 10.0])
def test_e1rm_is_the_bot_formula(formula, rpe):
    for reps in range(1, 16):
        for weight in (20.0, 62.5, 100.0, 142.5):
            assert seo_tools.e1rm(weight, reps, formula, rpe) == analytics.e1rm(weight, reps, formula, rpe)


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


@pytest.mark.skipif(shutil.which("node") is None, reason="node не установлен")
def test_browser_script_matches_python():
    cases_e1rm = [(w, r, f, rpe) for w in (60.0, 100.0) for r in (1, 5, 8, 10, 11, 12, 13)
                  for f in ("epley", "brzycki") for rpe in (None, 8.0, 9.5)]
    cases_plates = [(80, 20, [25, 20, 15, 10]), (101, 20, list(seo_tools.DEFAULT_PLATES["kg"])),
                    (102.5, 20, list(seo_tools.DEFAULT_PLATES["kg"])), (315, 45, list(seo_tools.DEFAULT_PLATES["lb"])),
                    (10, 20, [25])]
    cases_scores = [(500, 80, "m"), (300, 60, "f"), (700, 250, "m"), (200, 20, "f")]
    script = seo_tools.calc_js() + f"""
var out = {{
  e1rm: {json.dumps(cases_e1rm)}.map(function (c) {{ return e1rm(c[0], c[1], c[2], c[3]); }}),
  plates: {json.dumps(cases_plates)}.map(function (c) {{ return platesPerSide(c[0], c[1], c[2]); }}),
  scores: {json.dumps(cases_scores)}.map(function (c) {{ return [wilks(c[0], c[1], c[2]), dots(c[0], c[1], c[2])]; }})
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
