"""Копии кадров упражнений для сайта: `media/site/ex/<слаг>_<кадр>-<ширина>.{avif,webp}`.

Исходники — пары кадров free-exercise-db (`media/exercises/<слаг>_1.jpg` и
`_2.jpg`, 850×567, общественное достояние), те же, что у бота. На сайте
они показываются не шире своего размера и в нашей палитре:

- кадр 4:3 по центру (из 850×567 — 756×567, без растяжения);
- лёгкая резкость (UnsharpMask) — исходники мягкие;
- тёплый тон: чуть больше красного, чуть меньше синего, контраст +4% —
  чтобы фото не выпадали из тёмно-янтарной страницы;
- ширины 360 (карточка, 1×) и 720 (карточка 2× и страница техники);
  720 — меньше исходных 756, то есть картинка никогда не растягивается;
- AVIF и WebP, качество 82 (AVIF — 60: у него шкала другая, на глаз то же).

На странице списка — один кадр (конечное положение, `_2`), на странице
техники — оба. Упражнения без своей страницы, которые сайт показывает
карточкой (замены, «мышца × снаряд», дни шаблонов — seo_catalog.card_identities),
получают только конечный кадр шириной 360 — карточке хватает, а репозиторий
не раздувается. Готовые файлы не перезаписываются: AVIF
кодируется не побайтно одинаково, а пересохранённые кадры — шум в истории.
Запуск после добавления упражнения в seo_pages.EXERCISES или в каталог:

    python scripts/gen_site_exercise_images.py
"""

from __future__ import annotations

import os
import sys

from PIL import Image, ImageEnhance, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
SRC = os.path.join(ROOT, "media", "exercises")
OUT = os.path.join(ROOT, "media", "site", "ex")
WIDTHS = (360, 720)
QUALITY = {"webp": 82, "avif": 60}


def grade(img: Image.Image) -> Image.Image:
    img = img.convert("RGB")
    w, h = img.size
    target_w = min(w, round(h * 4 / 3))
    left = (w - target_w) // 2
    img = img.crop((left, 0, left + target_w, h))
    r, g, b = img.split()
    r = r.point(lambda v: min(255, round(v * 1.04)))
    b = b.point(lambda v: round(v * 0.95))
    img = Image.merge("RGB", (r, g, b))
    return ImageEnhance.Contrast(img).enhance(1.04)


def main() -> int:
    import db  # noqa: F401 — seed_data тянет db, а seo_pages — seed_data
    import exercise_media
    import seo_catalog
    import seo_pages

    os.makedirs(OUT, exist_ok=True)
    count = 0
    jobs = [(slug, (1, 2), WIDTHS) for _url, slug in seo_pages.EXERCISES]
    jobs += [(exercise_media.EXERCISE_IMAGE_SLUGS[name], (2,), WIDTHS[:1]) for name in seo_catalog.card_identities()]
    for slug, frames, widths in jobs:
        for frame in frames:
            src = os.path.join(SRC, f"{slug}_{frame}.jpg")
            base = grade(Image.open(src))
            for width in widths:
                if frame == 1 and width == 360:
                    continue  # стартовый кадр на карточках не показывается
                height = round(width * 3 / 4)
                img = base.resize((width, height), Image.LANCZOS)
                img = img.filter(ImageFilter.UnsharpMask(radius=1.1, percent=70, threshold=2))
                for fmt, q in QUALITY.items():
                    if os.path.exists(os.path.join(OUT, f"{slug}_{frame}-{width}.{fmt}")):
                        continue
                    img.save(os.path.join(OUT, f"{slug}_{frame}-{width}.{fmt}"), quality=q, method=6 if fmt == "webp" else None)
                    count += 1
    print(f"{count} files in {OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
