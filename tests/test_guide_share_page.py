"""Ссылка «поделиться гайдом» `/g/<slug>` (guide_share_page.py).

Гоняется через то же приложение, что поднимает прод (`mcp_server.build_app`):
iPhone без приложения → App Store с кампанией `ct=guide_<slug>`, остальные →
статья сайта на языке браузера, незнакомый адрес — 404. Переход — в счётчике
сайта одной строкой `/g`, без адреса гайда.
"""

from __future__ import annotations

import httpx
import pytest

import acquisition
import config
import db
import guide_share_page
import guide_stories
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
async def test_iphone_without_the_app_goes_to_the_app_store(fresh_db, ua):
    resp = await _get("/g/break-a-plateau", ua, "ru-RU,ru;q=0.9")

    assert resp.status_code == 302
    assert resp.headers["location"] == f"{STORE}&ct=guide_break-a-plateau"
    assert resp.headers["cache-control"] == "no-store"


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
async def test_iphone_falls_back_to_the_site_without_a_store_link(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "APP_STORE_URL", "")

    resp = await _get("/g/creatine", IPHONE, "fr-FR")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/fr/guides/creatine"


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
    assert rows == [("view", "/g", "ios", "appstore"), ("view", "/g", "desktop", "site")]


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
