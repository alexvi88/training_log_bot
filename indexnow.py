"""IndexNow сам после деплоя: Bing и Яндекс узнают о страницах сайта сразу, а
не при следующем обходе (поиск ChatGPT стоит на индексе Bing).

Раньше это был ручной `scripts/indexnow_ping.py`, и его не запускали — после
смены набора страниц (языки, noindex) Bing неделями видел старый sitemap.
Теперь сервер на старте сравнивает sitemap с тем, что уже отправлял (отпечаток
лежит рядом с базой), и шлёт адреса, только если набор изменился. Без ключа
(`INDEXNOW_KEY`) или своего https-домена — ничего не делает.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

import config

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.indexnow.org/indexnow"
_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
# Дать серверу подняться и не спорить за CPU со стартом бота.
START_DELAY_SECONDS = 60


def sitemap_urls(xml_text: str) -> list[str]:
    root = ET.fromstring(xml_text)
    return [loc.text.strip() for loc in root.findall("s:url/s:loc", _NS) if loc.text]


def payload(site: str, key: str, urls: list[str]) -> dict:
    return {"host": urlsplit(site).hostname, "key": key,
            "keyLocation": f"{site.rstrip('/')}/{key}.txt", "urlList": urls[:10000]}


def _stamp_path() -> str:
    return os.path.join(os.path.dirname(config.DB_PATH) or ".", "indexnow_sitemap.sha256")


def _post(body: dict) -> int:
    request = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(request, timeout=30) as resp:
        return resp.status


async def ping_if_changed() -> bool:
    """Отправить sitemap в IndexNow, если он изменился с прошлой отправки.
    True — отправили. Любая ошибка — в лог, старт сервера не страдает."""
    key = (config.INDEXNOW_KEY or "").strip()
    site = config.site_base_url() or ""
    if not key or not site.startswith("https://"):
        return False
    import seo_pages

    try:
        xml_text = seo_pages.sitemap_xml(None)
        digest = hashlib.sha256(xml_text.encode()).hexdigest()
        path = _stamp_path()
        try:
            with open(path) as f:
                if f.read().strip() == digest:
                    return False
        except FileNotFoundError:
            pass
        body = payload(site, key, sitemap_urls(xml_text))
        status = await asyncio.to_thread(_post, body)
        logger.info("indexnow: sent %d urls, status %s", len(body["urlList"]), status)
        if 200 <= status < 300:
            with open(path, "w") as f:
                f.write(digest)
            return True
    except Exception:  # noqa: BLE001 — пинг поисковика не должен ронять сервер
        logger.warning("indexnow ping failed", exc_info=True)
    return False


async def run_on_startup() -> None:
    await asyncio.sleep(START_DELAY_SECONDS)
    await ping_if_changed()
