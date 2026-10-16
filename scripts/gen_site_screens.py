"""Снимки приложения для сайта в 720px (AVIF и WebP): `media/site/screens/<язык>-<экран>-720.*`.

Источник — PNG прогона `screenshots.yml` iOS-репозитория (ветка
`screenshots`), те же кадры, из которых сделаны 540px JPEG рядом. 720px —
чтобы телефон на главной (252px шириной) был чётким на экране 3×.
Запуск: `python scripts/gen_site_screens.py <язык>-<экран>=<путь к PNG> ...`.
Экран без такой копии остаётся на JPEG 540 — страница берёт копию, только
если файл есть (seo_pages.screen_picture).
"""

from __future__ import annotations

import os
import sys

from PIL import Image

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media", "site", "screens")


def main(args: list[str]) -> int:
    for arg in args:
        name, path = arg.split("=", 1)
        img = Image.open(path).convert("RGB")
        w, h = img.size
        img = img.resize((720, round(h * 720 / w)), Image.LANCZOS)
        img.save(os.path.join(OUT, f"{name}-720.webp"), quality=84, method=6)
        img.save(os.path.join(OUT, f"{name}-720.avif"), quality=62)
        print(name, img.size, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
