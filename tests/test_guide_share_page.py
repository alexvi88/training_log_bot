"""Ссылка «поделиться гайдом» `/g/<slug>` (guide_share_page.py).

Гоняется через то же приложение, что поднимает прод (`mcp_server.build_app`):
iPhone → страница-развилка (схема приложения, App Store с кампанией
`ct=guide_<slug>`, статья сайта) — дошедший сюда iPhone не значит «приложения
нет»; остальные → 302 на статью сайта на языке браузера, незнакомый адрес —
404. Переход — в счётчике сайта одной строкой `/g`, без адреса гайда.
"""

from __future__ import annotations

import re
from html import escape

import httpx
import pytest

import acquisition
import config
import db
import guide_share_page
import guide_stories
import i18n
import product_metrics

BASE = "https://training-log.example.com"
STORE = "https://apps.apple.com/app/id6738000000?pt=12345"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1"
IPAD = "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1"
DESKTOP = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15"
ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/128.0 Mobile Safari/537.36"


@pytest.fixture(autouse=True)
def public_url(monkeypatch):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", BASE)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(config, "APP_STORE_URL", STORE)


async def _get(path: str, user_agent: str, lang: str | None = None) -> httpx.Response:
    import mcp_server

    headers = {"user-agent": user_agent}
    if lang:
        headers["accept-language"] = lang
    transport = httpx.ASGITransport(app=mcp_server.build_app())
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        return await client.get(path, headers=headers)


@pytest.mark.asyncio
@pytest.mark.parametrize("ua", [IPHONE, IPAD])
async def test_iphone_gets_a_landing_not_a_store_redirect(fresh_db, ua):
    # Universal Link из встроенного браузера Telegram не срабатывает: iPhone с
    # приложением тоже доходит сюда — слепой 302 в App Store уводил его в магазин.
    resp = await _get("/g/break-a-plateau", ua, "ru-RU,ru;q=0.9")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "no-store"
    assert "location" not in resp.headers
    html = resp.text
    assert '<html lang=ru>' in html
    assert escape(i18n.t_in("ru", "seo.guide.break-a-plateau.h1")) in html
    assert escape(i18n.t_in("ru", "guide_share_page.lead")) in html
    assert 'href="trainingdiary://g/break-a-plateau"' in html
    assert escape(i18n.t_in("ru", "referral_page.btn.open_app")) in html
    assert f'href="{STORE}&amp;ct=guide_break-a-plateau"' in html
    assert escape(i18n.t_in("ru", "referral_page.btn.app_store")) in html
    assert 'href="/ru/guides/break-a-plateau"' in html
    assert escape(i18n.t_in("ru", "guide_share_page.btn.site")) in html
    assert f'src="{BASE}/v1/media/guides/break-a-plateau.jpg"' in html
    assert "/go/seen" in html


@pytest.mark.asyncio
async def test_landing_speaks_the_browser_language(fresh_db):
    resp = await _get("/g/creatine", IPHONE, "en-US,en;q=0.9")

    html = resp.text
    assert "<html lang=en>" in html
    assert escape(i18n.t_in("en", "seo.guide.creatine.h1")) in html
    assert escape(i18n.t_in("en", "guide_share_page.lead")) in html
    assert 'href="/en/guides/creatine"' in html
    assert not re.search(r"[а-яА-ЯёЁ]", html)


@pytest.mark.asyncio
async def test_landing_cover_is_served(fresh_db):
    resp = await _get("/g/creatine", IPHONE, "en")
    src = re.search(r'<img src="([^"]+)"', resp.text).group(1)

    cover = await _get(src.removeprefix(BASE), IPHONE)
    assert cover.status_code == 200
    assert cover.headers["content-type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_landing_without_a_cover_file_has_no_image(fresh_db, monkeypatch):
    monkeypatch.setattr(guide_stories, "cover_path", lambda slug: None)

    resp = await _get("/g/creatine", IPHONE, "en")

    assert resp.status_code == 200
    assert "<img" not in resp.text
    assert 'href="trainingdiary://g/creatine"' in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("accept", "segment"),
    [("en-US,en;q=0.9", "en"), ("ru-RU,ru;q=0.9", "ru"), ("de-DE", "de"), (None, "en"), ("ja-JP", "en")],
)
async def test_everyone_else_gets_the_site_article_in_their_language(fresh_db, accept, segment):
    resp = await _get("/g/creatine", DESKTOP, accept)

    assert resp.status_code == 302
    assert resp.headers["location"] == f"/{segment}/guides/creatine"


@pytest.mark.asyncio
async def test_android_and_link_previews_get_the_site(fresh_db):
    android = await _get("/g/creatine", ANDROID, "es-ES")
    preview = await _get("/g/creatine", "TelegramBot (like TwitterBot)", "en")

    assert android.headers["location"] == "/es/guides/creatine"
    assert preview.headers["location"] == "/en/guides/creatine"


@pytest.mark.asyncio
async def test_iphone_landing_without_a_store_link_has_no_store_button(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "APP_STORE_URL", "")

    resp = await _get("/g/creatine", IPHONE, "fr-FR")

    assert resp.status_code == 200
    html = resp.text
    assert "apps.apple.com" not in html
    assert escape(i18n.t_in("fr", "referral_page.btn.app_store")) not in html
    assert 'href="trainingdiary://g/creatine"' in html
    assert 'href="/fr/guides/creatine"' in html


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/g/no-such-guide", "/g/..%2Fprivacy", "/g/CREATINE"])
async def test_unknown_slug_is_404(fresh_db, path):
    resp = await _get(path, IPHONE, "en")

    assert resp.status_code == 404
    assert "apps.apple.com" not in resp.headers.get("location", "")


@pytest.mark.asyncio
async def test_every_guide_resolves_to_its_site_page(fresh_db):
    for slug in guide_stories.ORDER:
        resp = await _get(f"/g/{slug}", DESKTOP, "en")
        assert resp.status_code == 302, slug
        page = await _get(resp.headers["location"], DESKTOP, "en")
        assert page.status_code == 200, slug


@pytest.mark.asyncio
async def test_follow_is_counted_under_one_key(fresh_db):
    await _get("/g/creatine", IPHONE, "en")
    await _get("/g/break-a-plateau", DESKTOP, "ru")
    await _get("/g/nope", DESKTOP, "ru")

    cur = await db.conn().execute("SELECT kind, page, device, target FROM site_events ORDER BY id")
    rows = [tuple(r) for r in await cur.fetchall()]
    assert rows == [("view", "/g", "ios", "page"), ("view", "/g", "desktop", "site")]


def test_campaign_fits_the_app_store_limit():
    for slug in guide_stories.ORDER:
        token = guide_share_page.campaign(slug)
        assert token == f"guide_{slug}"
        assert len(token) <= acquisition.APP_STORE_CAMPAIGN_MAX_LEN
    assert acquisition.with_campaign_token("https://apps.apple.com/app/id1", "x" * 50).endswith("ct=" + "x" * 40)


def test_share_url_has_no_language_and_points_at_the_route():
    assert guide_stories.share_url("creatine") == f"{BASE}/g/creatine"


def test_share_url_falls_back_to_the_default_site(monkeypatch):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "")
    monkeypatch.setattr(config, "SITE_BASE_URL", "")
    assert guide_stories.share_url("creatine") == f"{guide_stories.DEFAULT_SITE}/g/creatine"


def test_guide_share_event_is_accepted_without_user_text():
    clean = product_metrics.clean_props
    assert clean("guide_share", {"slug": "creatine", "source": "story", "to": "whatsapp"}) == {
        "slug": "creatine", "source": "story"}
    assert clean("guide_share", {"slug": "creatine", "source": "elsewhere"}) == {"slug": "creatine"}
    assert clean("guide_share", {"slug": "creatine"}) == {"slug": "creatine"}


def test_guide_open_by_a_friends_link_keeps_its_source():
    clean = product_metrics.clean_props
    for source in ("menu", "link"):
        assert clean("guide_open", {"slug": "creatine", "source": source}) == {"slug": "creatine", "source": source}
    assert clean("guide_open", {"slug": "creatine", "source": "push"}) == {"slug": "creatine"}


@pytest.mark.asyncio
async def test_landing_beacon_is_accepted(fresh_db):
    import mcp_server

    transport = httpx.ASGITransport(app=mcp_server.build_app())
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        resp = await client.post("/go/seen?p=/g&l=en", headers={"user-agent": IPHONE, "sec-fetch-site": "same-origin"})
    assert resp.status_code == 204

    cur = await db.conn().execute("SELECT kind, page FROM site_events")
    assert [tuple(r) for r in await cur.fetchall()] == [("seen", "/g")]
