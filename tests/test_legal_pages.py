"""/privacy и /terms — публичные страницы для App Store (legal_pages.py).

Гоняются через то же приложение, что поднимает прод (`mcp_server.build_app`),
а не через голый модуль: страница, которая есть в коде, но не доехала до
маршрутов, ревьюеру Apple отдаст 404 — ровно это и проверяем. Без токена —
страницу открывают до входа.
"""

import httpx
import pytest

import config
import legal_pages
import mcp_server


@pytest.fixture(autouse=True)
def public_url(monkeypatch):
    # OAuth-часть приложения без HTTPS-адреса не собирается (см. mcp_oauth).
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "https://training-log.example.com")


async def _get(path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=mcp_server.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="https://training-log.example.com") as client:
        return await client.get(path, headers=headers or {})


@pytest.mark.parametrize("page", legal_pages.PAGES)
async def test_page_is_public_and_russian_by_default(page):
    resp = await _get(f"/{page}")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert '<html lang=ru>' in resp.text
    assert "Вступа" in resp.text  # «Вступает/Вступают в силу»
    assert "25 сентября 2026" in resp.text


@pytest.mark.parametrize("page", legal_pages.PAGES)
async def test_lang_query_switches_to_english(page):
    resp = await _get(f"/{page}?lang=en")

    assert resp.status_code == 200
    assert '<html lang=en>' in resp.text
    assert "Effective September 25, 2026" in resp.text
    # Англоязычный не видит русского текста — кроме ссылки-переключателя,
    # которая нарочно подписана автонимом «Русский».
    body = resp.text.replace("Русский", "")
    assert not any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in body)


@pytest.mark.parametrize("page", legal_pages.PAGES)
async def test_accept_language_picks_english_without_query(page):
    resp = await _get(f"/{page}", headers={"Accept-Language": "en-US,en;q=0.9"})

    assert '<html lang=en>' in resp.text
    assert resp.headers["content-language"] == "en"
    assert "Accept-Language" in resp.headers["vary"]


async def test_explicit_lang_beats_accept_language():
    """Приложение шлёт ?lang= языка аккаунта — он главнее языка телефона."""
    resp = await _get("/privacy?lang=ru", headers={"Accept-Language": "en-US"})

    assert '<html lang=ru>' in resp.text


async def test_unknown_lang_falls_back_to_header_then_russian():
    resp = await _get("/privacy?lang=de")

    assert '<html lang=ru>' in resp.text


def test_email_is_not_rendered_while_constant_is_empty():
    for page in legal_pages.PAGES:
        for lang in legal_pages.LANGS:
            assert "mailto:" not in legal_pages.render(page, lang)


def test_email_appears_on_every_page_once_set(monkeypatch):
    monkeypatch.setattr(legal_pages, "CONTACT_EMAIL", "coach@example.com")
    for page in legal_pages.PAGES:
        for lang in legal_pages.LANGS:
            assert 'href="mailto:coach@example.com"' in legal_pages.render(page, lang)


def test_every_page_links_to_the_other_and_the_other_language():
    for lang in legal_pages.LANGS:
        other = "en" if lang == "ru" else "ru"
        privacy = legal_pages.render("privacy", lang)
        terms = legal_pages.render("terms", lang)
        assert f'href="/terms?lang={lang}"' in privacy
        assert f'href="/privacy?lang={lang}"' in terms
        assert f'href="/privacy?lang={other}"' in privacy
        assert f'href="/terms?lang={other}"' in terms


async def test_support_page_is_public_in_both_languages():
    ru = await _get("/support")
    assert ru.status_code == 200
    assert '<html lang=ru>' in ru.text
    assert "«Поддержка и отзыв»" in ru.text
    assert "t.me" not in ru.text

    en = await _get("/support?lang=en")
    assert '<html lang=en>' in en.text
    assert "Support &amp; feedback" in en.text
    assert "t.me" not in en.text
    body = en.text.replace("Русский", "")
    assert not any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in body)


def test_support_page_shows_email_only_when_set(monkeypatch):
    for lang in legal_pages.LANGS:
        assert "mailto:" not in legal_pages.render(legal_pages.SUPPORT_PAGE, lang)
    monkeypatch.setattr(legal_pages, "CONTACT_EMAIL", "coach@example.com")
    for lang in legal_pages.LANGS:
        page = legal_pages.render(legal_pages.SUPPORT_PAGE, lang)
        assert '<li>E-mail: <a href="mailto:coach@example.com">' in page
