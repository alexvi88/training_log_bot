"""Сайт на всех языках продукта (seo_pages.LANGS): адрес с сегментом языка
(`/pt-pt/` — строчными), hreflang у каждой страницы на все восемь языков и
x-default, переключатель языка в шапке и подвале, sitemap, служебные адреса
(превью, карточки, манифест, встраиваемый калькулятор, файлы шаблона) и
301 на свой домен (seo_http). Каталог `seo.*` для новых языков может ещё
не быть переведён — тогда `i18n` берёт английский, и кириллицы на странице
нет нигде, кроме автонима «Русский» в переключателе.
"""

import re
import xml.etree.ElementTree as ET

import httpx
import pytest

import config
import i18n
import mcp_server
import seo_http
import seo_pages
import seo_static

HOST = "https://training-log.example.com"
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
NEW_LANGS = ("es", "pt", "pt-PT", "de", "fr", "tr")


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


def _one_page_per_kind() -> list[seo_pages.Page]:
    seen, out = set(), []
    for page in seo_pages.pages():
        if page.kind not in seen:
            seen.add(page.kind)
            out.append(page)
    return out


# ---------- языки и адреса ----------


def test_site_speaks_every_product_language_with_its_own_codes():
    assert seo_pages.LANGS == i18n.SUPPORTED
    assert set(seo_pages.OG_LOCALES) == set(seo_pages.AUTONYMS) == set(seo_pages.LANGS)
    assert [seo_pages.lang_segment(lang) for lang in seo_pages.LANGS] == ["ru", "en", "es", "pt", "pt-pt", "de", "fr", "tr"]
    assert [seo_pages.hreflang(lang) for lang in seo_pages.LANGS] == ["ru", "en", "es", "pt-BR", "pt-PT", "de", "fr", "tr"]
    for lang in seo_pages.LANGS:
        assert seo_pages.lang_from_segment(seo_pages.lang_segment(lang)) == lang
        assert seo_pages.lang_from_code(seo_pages.hreflang(lang)) == lang
    # Сегмент — только строчными; чужой язык и код вместо сегмента — не язык сайта.
    for bad in ("pt-PT", "pt-br", "it", "", "RU"):
        assert seo_pages.lang_from_segment(bad) is None, bad
    assert seo_pages.lang_of_path("/pt-pt/tools/1rm") == "pt-PT"
    assert seo_pages.lang_of_path("/tr") == "tr"
    assert seo_pages.lang_of_path("/trx/tools") is None
    assert seo_pages.url_for(seo_pages.Page("tool", "1rm"), "pt-PT") == "/pt-pt/tools/1rm"
    assert seo_pages.url_for(seo_pages.Page("home"), "pt") == "/pt"


async def test_every_page_kind_renders_in_every_language_with_full_hreflang_set():
    pages = _one_page_per_kind()
    responses = await _get_many([seo_pages.url_for(p, lang) for p in pages for lang in seo_pages.LANGS])
    for page in pages:
        for lang in seo_pages.LANGS:
            url = seo_pages.url_for(page, lang)
            resp = responses[url]
            html = resp.text
            assert resp.status_code == 200, url
            assert resp.headers["content-language"] == seo_pages.hreflang(lang)
            assert f"<html lang={seo_pages.hreflang(lang)}>" in html, url
            assert f'<link rel=canonical href="{HOST}{url}">' in html, url
            alternates = re.findall(r'<link rel=alternate hreflang=([^ ]+) href="([^"]+)">', html)
            expected = [(seo_pages.hreflang(c), HOST + seo_pages.url_for(page, c)) for c in seo_pages.LANGS]
            expected.append(("x-default", HOST + seo_pages.url_for(page, "en")))
            assert alternates == expected, url
            assert f'<meta property="og:locale" content="{seo_pages.OG_LOCALES[lang]}">' in html
            assert f'href="/site.webmanifest?lang={seo_pages.lang_segment(lang)}"' in html
            if lang != "ru":
                rest = html.replace("Русский", "")
                assert not _CYRILLIC.search(rest), (url, _CYRILLIC.findall(rest)[:10])


async def test_switcher_lists_all_languages_in_header_and_footer():
    page = seo_pages.Page("tool", "1rm")
    responses = await _get_many([seo_pages.url_for(page, lang) for lang in seo_pages.LANGS])
    for lang in seo_pages.LANGS:
        html = responses[seo_pages.url_for(page, lang)].text
        head = re.search(r"<header class=site-head>(.*?)</header>", html, re.S).group(1)
        foot = re.search(r"<footer class=site-foot>(.*?)</footer>", html, re.S).group(1)
        switcher = re.search(r"<details class=langs><summary class=lang>(.*?)</summary>(.*?)</details>", head, re.S)
        assert switcher, lang
        label = i18n.t_in(lang, "screen.language.button")
        assert f"<span class=vh>{label}: {seo_pages.AUTONYMS[lang]}</span>" in switcher.group(1)
        assert f'aria-label="{label}"' in switcher.group(2)
        footer_langs = re.search(r"<nav class=foot-langs[^>]*>(.*?)</nav>", foot, re.S)
        assert footer_langs, lang
        for block in (switcher.group(2), footer_langs.group(1)):
            links = re.findall(r'<a href="([^"]+)" hreflang=([^ ]+) lang=([^ >]+)( aria-current=true)?>([^<]+)</a>', block)
            assert [(href, code, name) for href, code, _l, _c, name in links] == [
                (seo_pages.url_for(page, c), seo_pages.hreflang(c), seo_pages.AUTONYMS[c]) for c in seo_pages.LANGS]
            assert all(code == lng for _h, code, lng, _c, _n in links)
            # Текущий язык отмечен, и только он.
            assert [name for _h, _code, _l, current, name in links if current] == [seo_pages.AUTONYMS[lang]]
        assert "seo.footer.other_lang" not in html


async def test_pt_pt_routes_and_unknown_prefixes_are_404():
    responses = await _get_many(["/pt-pt", "/pt-pt/tools/1rm", "/pt/tools/1rm", "/pt-pt/nope", "/de/tools/nope",
                                 "/pt-PT/tools/1rm", "/it/tools/1rm", "/xx", "/pt-br/tools/1rm"])
    for path in ("/pt-pt", "/pt-pt/tools/1rm", "/pt/tools/1rm"):
        assert responses[path].status_code == 200, path
    assert "<html lang=pt-PT>" in responses["/pt-pt/tools/1rm"].text
    assert "<html lang=pt-BR>" in responses["/pt/tools/1rm"].text
    # Неизвестная страница под языком сайта — наша 404 на этом языке.
    for path, code in (("/pt-pt/nope", "pt-PT"), ("/de/tools/nope", "de")):
        resp = responses[path]
        assert resp.status_code == 404 and "<meta name=robots content=noindex>" in resp.text, path
        assert f"<html lang={code}>" in resp.text and "rel=canonical" not in resp.text
    # Не язык сайта — не наш адрес вовсе.
    for path in ("/pt-PT/tools/1rm", "/it/tools/1rm", "/xx", "/pt-br/tools/1rm"):
        assert responses[path].status_code == 404, path
        assert "coach_idle.png" not in responses[path].text, path


async def test_sitemap_lists_every_language_with_alternates():
    resp = (await _get_many(["/sitemap.xml"]))["/sitemap.xml"]
    root = ET.fromstring(resp.text)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9", "x": "http://www.w3.org/1999/xhtml"}
    urls = root.findall("s:url", ns)
    assert len(urls) == len(seo_pages.pages()) * len(seo_pages.LANGS)
    by_lang = {lang: 0 for lang in seo_pages.LANGS}
    for url in urls:
        loc = url.find("s:loc", ns).text
        by_lang[seo_pages.lang_of_path(loc.removeprefix(HOST))] += 1
        links = {link.get("hreflang"): link.get("href") for link in url.findall("x:link", ns)}
        assert set(links) == {seo_pages.hreflang(c) for c in seo_pages.LANGS} | {"x-default"}
        assert loc in links.values()
    assert set(by_lang.values()) == {len(seo_pages.pages())}


async def test_service_addresses_work_for_new_languages():
    import seo_template

    name = next(iter(seo_template.FILES))
    paths = ["/site.webmanifest?lang=pt-pt", "/site.webmanifest?lang=de", "/site.webmanifest?lang=xx",
             "/embed/pt-pt/1rm", "/og/tr/tools-1rm.png", "/og/card/pt-BR/dots.png?v=345", "/og/card/es/dots.png?v=345",
             "/og/card/it/dots.png?v=345", seo_pages.template_file_url(name, "pt-PT")]
    responses = await _get_many(paths)
    manifest = responses["/site.webmanifest?lang=pt-pt"].json()
    assert manifest["lang"] == "pt-PT" and manifest["start_url"] == "/pt-pt"
    assert responses["/site.webmanifest?lang=de"].json()["start_url"] == "/de"
    assert responses["/site.webmanifest?lang=xx"].json()["start_url"] == "/en"
    embed = responses["/embed/pt-pt/1rm"]
    assert embed.status_code == 200 and "<html lang=pt-PT>" in embed.text
    assert f'href="{HOST}/pt-pt/tools/1rm"' in embed.text
    for path in ("/og/tr/tools-1rm.png", "/og/card/pt-BR/dots.png?v=345", "/og/card/es/dots.png?v=345"):
        assert responses[path].status_code == 200 and responses[path].headers["content-type"] == "image/png", path
    assert responses["/og/card/it/dots.png?v=345"].status_code == 404
    assert responses[seo_pages.template_file_url(name, "pt-PT")].status_code == 200


async def test_diary_button_reads_language_of_any_site_page():
    for lang in seo_pages.LANGS:
        referer = HOST + seo_pages.url_for(seo_pages.Page("tool", "1rm"), lang)
        resp = (await _get_many(["/go/diary?f=rm1_log"], {"Referer": referer}))["/go/diary?f=rm1_log"]
        assert resp.status_code == 302, lang


def test_home_uses_english_screens_for_languages_without_their_own():
    for lang in NEW_LANGS:
        html = seo_pages.render(seo_pages.Page("home"), lang)
        assert "screens/en-workout" in html and "screens/ru-" not in html, lang
        assert "gym/scene-en" in html, lang
        assert seo_pages.page_images(seo_pages.Page("home"), lang) == seo_pages.page_images(seo_pages.Page("home"), "en")
    assert "screens/ru-workout" in seo_pages.render(seo_pages.Page("home"), "ru")


def test_tonnage_plural_samples_cover_new_languages():
    import seo_calcs

    for lang in NEW_LANGS:
        objects = seo_calcs._tonnage_objects(lang)
        assert objects and all(set(o["f"]) == {"one", "other"} for o in objects), lang
        assert not _CYRILLIC.search(seo_calcs.tonnage_prelude(lang)), lang


def test_own_domain_redirect_covers_every_language(monkeypatch):
    monkeypatch.setattr(config, "SITE_BASE_URL", "https://athletediary.app")
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "https://training-log-bot.fly.dev")
    for lang in seo_pages.LANGS:
        seg = seo_pages.lang_segment(lang)
        assert seo_http.is_site_path(f"/{seg}") and seo_http.is_site_path(f"/{seg}/tools/1rm"), lang
        assert seo_http.redirect_target("training-log-bot.fly.dev", f"/{seg}/tools", "", "GET") == \
            f"https://athletediary.app/{seg}/tools"
    for path in ("/pt-PT/tools", "/it/tools", "/trx", "/v1/health", "/embed/de/1rm", "/de-x/tools"):
        assert not seo_http.is_site_path(path), path
