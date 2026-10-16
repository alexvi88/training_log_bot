"""Сайт на телефоне: дешёвые инварианты вёрстки, которые держат мобильную
шапку и формы без браузера (seo_design.CSS + разметка seo_pages._layout).

Глазами сайт проверяется Playwright-проходом вне CI; здесь — то, что
ломалось на живом iPhone в браузере Telegram: шапка в две строки с лентой
разделов, которая обрезала первую пилюлю; полупрозрачная шапка, сквозь
которую и над которой виден текст; поля ввода мельче 16px (iOS зумит
страницу на фокусе).
"""

import re

import httpx
import pytest

import config
import i18n
import mcp_server
import seo_design
import seo_pages
import seo_static

HOST = "https://training-log.example.com"
STORE = "https://apps.apple.com/app/apple-store/id6815211995?pt=129483617&mt=8"
UA_ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36"


@pytest.fixture(autouse=True)
def public_url(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", HOST)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(config, "SITE_BASE_URL", "")
    monkeypatch.setattr(config, "APP_STORE_URL", STORE)
    monkeypatch.setattr(config, "GOOGLE_SITE_VERIFICATION", "")
    monkeypatch.setattr(config, "GOOGLE_SITE_VERIFICATION_FILE", "")
    monkeypatch.setattr(config, "INDEXNOW_KEY", "")
    monkeypatch.setattr(seo_static, "OG_CACHE_DIR", str(tmp_path / "og"))
    seo_static._og_memory.clear()


async def _get_many(paths, headers=None):
    transport = httpx.ASGITransport(app=mcp_server.build_app())
    async with httpx.AsyncClient(transport=transport, base_url=HOST) as client:
        return {p: await client.get(p, headers=headers or {}) for p in paths}


def _one_page_per_kind() -> list[str]:
    seen, urls = set(), []
    for page in seo_pages.pages():
        if page.kind not in seen:
            seen.add(page.kind)
            urls += [seo_pages.url_for(page, lang) for lang in seo_pages.LANGS]
    return urls


def _rules(css: str) -> list[tuple[str, str, str]]:
    """(медиа-условие или '', селектор, тело) для каждого правила таблицы —
    без полноценного парсера: вложенность в стилях сайта одна (@media/@supports)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, i, media = [], 0, [""]
    depth_media: list[int] = []
    depth = 0
    token = ""
    while i < len(css):
        ch = css[i]
        if ch == "{":
            head = token.strip()
            token = ""
            depth += 1
            if head.startswith("@"):
                media.append(head)
                depth_media.append(depth)
            else:
                end = css.index("}", i)
                out.append((media[-1], head, css[i + 1:end]))
                i = end
                depth -= 1
        elif ch == "}":
            if depth_media and depth_media[-1] == depth:
                depth_media.pop()
                media.pop()
            depth -= 1
            token = ""
        else:
            token += ch
        i += 1
    return out


CSS = seo_design.CSS


def _base(selector: str) -> str:
    """Тело правила с ровно этим селектором вне @media."""
    bodies = [body for media, sel, body in _rules(CSS) if not media and sel == selector]
    assert bodies, f"нет правила {selector!r}"
    return " ".join(bodies)


async def test_every_page_kind_has_mobile_viewport_and_one_row_header():
    urls = _one_page_per_kind() + ["/en/nope-xyz", "/ru/nope-xyz"]
    pages = await _get_many(urls)
    for url, resp in pages.items():
        html = resp.text
        assert '<meta name=viewport content="width=device-width, initial-scale=1, viewport-fit=cover">' in html, url
        head = re.search(r"<header class=site-head>(.*?)</header>", html, re.S)
        assert head, url
        head = head.group(1)
        # Лента разделов второй строкой ушла в меню: разделы на телефоне — за кнопкой.
        assert "sub-nav" not in html, url
        menu = re.search(r"<details class=menu><summary class=menu-btn>(.*?)</summary>(.*?)</details>", head, re.S)
        assert menu, url
        lang = "ru" if "/ru" in url else "en"
        assert f'<span class=vh>{i18n.t_in(lang, "seo.nav.menu")}</span>' in menu.group(1), url
        links = re.findall(r'href="(/(?:ru|en)[^"]*)"', menu.group(2))
        assert links == [f"/{lang}/{s}" for s in ("guides", "exercises", "muscles", "programs", "compare", "tools")], url
        # Язык и главная кнопка — в той же строке, что логотип.
        assert "class=lang" in head and "head-cta" in head, url


async def test_header_cta_is_marked_when_it_is_the_long_bot_button():
    resp = (await _get_many(["/ru/tools/tonnage"], {"user-agent": UA_ANDROID}))["/ru/tools/tonnage"]
    head = re.search(r"<header class=site-head>(.*?)</header>", resp.text, re.S).group(1)
    assert 'class="btn small primary head-cta bot"' in head
    # CSS прячет подпись логотипа под длинную кнопку — без неё строка не влезает в 360px.
    assert ".head-row:has(.head-cta.bot) .brand span" in CSS


def test_header_is_opaque_sticky_and_clears_the_safe_area():
    head = _base(".site-head")
    assert "position: sticky" in head and "top: 0" in head
    assert "env(safe-area-inset-top" in head
    background = re.search(r"background:\s*([^;]+);", head).group(1)
    assert background.startswith("#"), "на телефоне шапка сплошная — сквозь неё не видно текст"
    assert "backdrop-filter" not in head
    # Над шапкой (под чужой панелью встроенного браузера) — та же заливка.
    cover = _base(".site-head::before")
    assert "bottom: 100%" in cover and "#" in cover
    assert "height: 56px" in _base(".head-row")
    # Шапка не прокручивается вбок ни одним своим куском.
    for _media, sel, body in _rules(CSS):
        if any(part in sel for part in (".site-head", ".head-row", ".head-end", ".menu")):
            assert "overflow-x: auto" not in body and "overflow-x: scroll" not in body, sel


def test_menu_is_reachable_and_closes_without_script():
    for sel in (".menu-btn", ".lang"):
        body = _base(sel)
        assert re.search(r"(min-)?height: 44px", body), sel
    assert any("min-height: 44px" in body for media, sel, body in _rules(CSS)
               if media == "@media (max-width: 959px)" and sel == ".site-head .btn.head-cta")
    assert "position: absolute" in _base(".menu-panel")  # поверх страницы, без сдвига вёрстки
    assert "min-height: 52px" in _base(".menu-panel a")
    assert ".menu[open] > .menu-btn::before" in CSS  # тап по затемнению — тап по summary
    assert "Escape" in seo_pages._PAGE_JS
    desktop = [body for media, sel, body in _rules(CSS) if media == "@media (min-width: 960px)" and ".menu" in sel]
    assert any("display: none" in body for body in desktop)


def test_form_fields_are_16px_or_more():
    """iOS увеличивает страницу, когда фокус попадает в поле мельче 16px."""
    for media, sel, body in _rules(CSS):
        if not re.search(r"(^|[\s,>])(input|select|textarea)\b", sel):
            continue
        for size in re.findall(r"(?:font-size:\s*|font:[^;]*?)(\d+(?:\.\d+)?)px", body):
            assert float(size) >= 16, f"{media} {sel}: {size}px"


def test_small_buttons_and_chips_grow_to_44px_on_phones():
    phone = " ".join(body for media, sel, body in _rules(CSS)
                     if media == "@media (max-width: 959px)" and (".btn.small" in sel or ".tabs a" in sel))
    assert phone.count("min-height: 44px") >= 2
    assert "padding: 10px 0" in _base(".foot-grid li a")
