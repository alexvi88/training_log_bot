"""Отправить все адреса сайта из sitemap в IndexNow (Bing, Яндекс и другие).

Зачем: поиск ChatGPT стоит на индексе Bing, а Bing по IndexNow узнаёт о
новых и изменённых страницах сразу, а не при следующем обходе. Запускать
руками после деплоя, который добавил или заметно поменял страницы:

    INDEXNOW_KEY=<ключ> python scripts/indexnow_ping.py            # отправить
    INDEXNOW_KEY=<ключ> python scripts/indexnow_ping.py --dry-run  # показать, что уйдёт

Ключ — тот же, что задан серверу в `INDEXNOW_KEY` (fly secrets): сервер
отдаёт его файлом `https://<сайт>/<ключ>.txt`, и по этому файлу IndexNow
проверяет, что адреса присылает владелец сайта. Адреса берутся из живого
`https://<сайт>/sitemap.xml` (`--site`, по умолчанию SITE_BASE_URL или
https://athletediary.app); за раз уходит до 10 000 адресов — у нас меньше.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

ENDPOINT = "https://api.indexnow.org/indexnow"
_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def sitemap_urls(xml_text: str) -> list[str]:
    root = ET.fromstring(xml_text)
    return [loc.text.strip() for loc in root.findall("s:url/s:loc", _NS) if loc.text]


def payload(site: str, key: str, urls: list[str]) -> dict:
    host = urlsplit(site).hostname
    return {"host": host, "key": key, "keyLocation": f"{site.rstrip('/')}/{key}.txt", "urlList": urls[:10000]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", default=os.getenv("SITE_BASE_URL") or "https://athletediary.app")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    key = os.getenv("INDEXNOW_KEY", "").strip()
    if not key:
        print("INDEXNOW_KEY не задан — ключ тот же, что у сервера.", file=sys.stderr)
        return 2
    site = args.site.rstrip("/")
    with urllib.request.urlopen(f"{site}/sitemap.xml", timeout=30) as resp:
        urls = sitemap_urls(resp.read().decode("utf-8"))
    body = payload(site, key, urls)
    if args.dry_run:
        print(json.dumps({**body, "urlList": body["urlList"][:5] + ["…"]}, ensure_ascii=False, indent=2))
        print(f"всего адресов: {len(body['urlList'])}")
        return 0
    request = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(request, timeout=30) as resp:
        print(f"IndexNow ответил {resp.status}: отправил {len(body['urlList'])} адресов")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
