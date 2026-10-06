"""Миниатюры первых кадров каталога для строк списков — media/exercises/thumbs/.

Строка списка в приложении рисует кадр 52x40 pt (ExerciseThumbnail), на 3x это
156x120 px, а полный кадр каталога весит ~65 КБ при 850x567. Миниатюра — тот же
кадр 240 px по ширине (с запасом над 156 px, чтобы на Retina не мылилось),
JPEG q80, около 8 КБ. Нужны только первые кадры (`*_1.jpg`): `thumb` в /v1 —
это именно первый кадр (exercise_media.thumb_url_for).

    python3 scripts/gen_exercise_thumbs.py          # дописать недостающие
    python3 scripts/gen_exercise_thumbs.py --force  # пересобрать все
    python3 scripts/gen_exercise_thumbs.py --check  # код 1, если чего-то нет

Файлы лежат в репозитории (бот ничего не режет на лету), имя в имя: полный кадр
media/exercises/<slug>_1.jpg -> media/exercises/thumbs/<slug>_1.jpg. Генерация
детерминирована по входу и параметрам: тот же кадр даёт тот же файл.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "media" / "exercises"
THUMBS_DIR = SOURCE_DIR / "thumbs"

THUMB_WIDTH = 240
JPEG_QUALITY = 80


def source_frames() -> list[Path]:
    return sorted(SOURCE_DIR.glob("*_1.jpg"))


def thumb_path(source: Path) -> Path:
    return THUMBS_DIR / source.name


def render(source: Path, target: Path) -> None:
    with Image.open(source) as img:
        img = img.convert("RGB")
        height = max(1, round(img.height * THUMB_WIDTH / img.width))
        small = img.resize((THUMB_WIDTH, height), Image.Resampling.LANCZOS)
    target.parent.mkdir(parents=True, exist_ok=True)
    small.save(target, "JPEG", quality=JPEG_QUALITY, optimize=True)


def missing() -> list[Path]:
    return [s for s in source_frames() if not thumb_path(s).exists()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="пересобрать и уже готовые")
    parser.add_argument("--check", action="store_true", help="только проверить, ничего не писать")
    args = parser.parse_args()

    if args.check:
        gaps = missing()
        for source in gaps:
            print(f"нет миниатюры: {source.name}")
        return 1 if gaps else 0

    done = 0
    for source in source_frames():
        target = thumb_path(source)
        if target.exists() and not args.force:
            continue
        render(source, target)
        done += 1
    print(f"готово: {done}, всего кадров: {len(source_frames())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
