"""Статика публичного сайта (seo_pages.py): иконки, шрифт, снимки приложения,
картинки для соцсетей и служебные файлы.

- `/site/<файл>` — только из `media/site/` и только известных типов: путь
  нормализуется, выйти из каталога нельзя. Там лежат:
  * снимки экрана настоящего приложения (`screens/<язык>-<экран>.jpg`) —
    из прогона `screenshots.yml` iOS-репозитория (ветка `screenshots`:
    `feature-store-screenshots/en-…` и `main/ru-…`), уменьшенные до 540 px;
  * тренер (`coach.jpg`, `coach-face.jpg`) — из `media/push/
    release_ai_programs_and_video.jpg`: кадр без надписи, годится обоим языкам;
  * пиксельный шрифт (`fonts/pixel.woff`) — Press Start 2P (SIL OFL 1.1,
    `fonts/OFL.txt`) из iOS-репозитория, урезанный до латиницы и кириллицы;
    урезанная копия — изменённая версия, поэтому по OFL она переименована
    (Reserved Font Name) в Basement Pixel, как и в приложении;
  * иконки (`icons/`) — из иконки приложения (AppIcon 1024);
  * кадры упражнений для сайта (`ex/<слаг>_<кадр>-<ширина>.avif|webp`) —
    из полноразмерных кадров бота, 4:3, резкость и тёплый тон
    (scripts/gen_site_exercise_images.py);
  * пиксельный подвал (`gym/`) — сцена «Меню» приложения и спрайты из
    iOS-репозитория (`docs/marketing/menu-gym`, наши собственные ассеты).
- `/favicon.ico`, `/apple-touch-icon.png`, `/site.webmanifest` — по
  привычным адресам, их браузеры спрашивают сами.
- `/og/<язык>/<ключ>.png` — картинка 1200×630 для превью ссылки: заголовок
  страницы и тренер на тёплом фоне. Рисуется Pillow при первом запросе и
  кэшируется в памяти и на диске (`OG_CACHE_DIR`); ключ — путь страницы без
  языка (`home`, `tools-1rm`, `exercises-back-squat`).
- `/<ключ>.txt` для IndexNow — только когда задан `INDEXNOW_KEY`.

Текст на картинке — сырой, не HTML: заголовки берутся из каталога как есть,
без escape (иначе апостроф английской строки нарисуется как `&#x27;`).
"""

from __future__ import annotations

import io
import os
import re
import tempfile
import threading
from collections import OrderedDict
from typing import Optional

from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response

import config

SITE_DIR = os.path.join(os.path.dirname(__file__), "media", "site")
COACH_SOURCE = os.path.join(os.path.dirname(__file__), "media", "push", "release_ai_programs_and_video.jpg")
OG_CACHE_DIR = os.path.join(tempfile.gettempdir(), "athletediary_og")
OG_SIZE = (1200, 630)
_OG_MEMORY_LIMIT = 48

_TYPES = {
    ".jpg": "image/jpeg", ".png": "image/png", ".ico": "image/x-icon", ".woff": "font/woff",
    ".txt": "text/plain; charset=utf-8", ".svg": "image/svg+xml", ".webp": "image/webp", ".avif": "image/avif",
}
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,120}$")

# Скриншоты на главной: (экран, ширина, высота) — у каждого языка свой прогон.
SCREENS = {
    "ru": (("workout", 540, 1174), ("summary", 540, 1174), ("progress", 540, 1174), ("coach", 540, 1174)),
    "en": (("workout", 540, 1173), ("progress", 540, 1173), ("coach", 540, 1173), ("history", 540, 1173)),
}

THEME_COLOR = "#120b09"

# Пиксельный подвал с вкладки «Меню» приложения (iOS-репозиторий,
# docs/marketing/menu-gym): настоящая сцена со скриншота симулятора и
# спрайты, извлечённые из кода (raw, 1:1), склеенные в ленты кадров. Макеты
# оттуда (папка mockups, с плашкой «макет») сюда не берутся.
SCENE_SIZE = (1110, 617)
# Лента кадров: (файл, ширина кадра, высота, кадров).
SPRITES = {
    "curl": ("gym/athlete_curl.png", 28, 77, 2),
    "coach": ("gym/coach_idle.png", 58, 100, 4),
    "dog": ("gym/dog_run.png", 33, 28, 4),
    "stand": ("gym/athlete_stand.png", 28, 77, 1),
}


def site_file(rel: str) -> Optional[str]:
    """Абсолютный путь к файлу из media/site или None — чужой путь, тип или нет файла."""
    if not _SAFE.match(rel) or ".." in rel or rel.endswith("/"):
        return None
    ext = os.path.splitext(rel)[1]
    if ext not in _TYPES:
        return None
    path = os.path.normpath(os.path.join(SITE_DIR, rel))
    if not path.startswith(SITE_DIR + os.sep) or not os.path.isfile(path):
        return None
    return path


def asset_url(rel: str) -> str:
    """Адрес файла из media/site с версией по времени изменения — кэш на год без риска старья."""
    path = os.path.join(SITE_DIR, rel)
    try:
        version = int(os.path.getmtime(path))
    except OSError:
        version = 0
    return f"/site/{rel}?v={version}"


def _file_response(path: str) -> Response:
    ext = os.path.splitext(path)[1]
    return FileResponse(path, media_type=_TYPES[ext], headers={
        "Cache-Control": "public, max-age=31536000, immutable" if ext in (".woff", ".jpg", ".png", ".svg", ".webp", ".avif") else "public, max-age=86400",
        "X-Content-Type-Options": "nosniff",
        "Access-Control-Allow-Origin": "*",
    })


async def site_route(request: Request) -> Response:
    path = site_file(request.path_params.get("path", ""))
    if path is None:
        return PlainTextResponse("Not found", status_code=404)
    return _file_response(path)


def root_file(rel: str):
    async def route(request: Request) -> Response:
        return _file_response(os.path.join(SITE_DIR, rel))

    return route


def webmanifest(lang: str, name: str, short_name: str, description: str) -> str:
    import json

    return json.dumps({
        "name": name,
        "short_name": short_name,
        "description": description,
        "lang": lang,
        "start_url": f"/{lang}",
        "display": "browser",
        "background_color": THEME_COLOR,
        "theme_color": THEME_COLOR,
        "icons": [
            {"src": "/site/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/site/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }, ensure_ascii=False)


# ---------- IndexNow ----------


def indexnow_key() -> Optional[str]:
    """Ключ IndexNow из окружения: 8–128 символов [a-zA-Z0-9-], иначе выключено."""
    key = (config.INDEXNOW_KEY or "").strip()
    return key if re.fullmatch(r"[A-Za-z0-9-]{8,128}", key) else None


# ---------- картинки для соцсетей ----------

_og_memory: "OrderedDict[str, bytes]" = OrderedDict()
_og_lock = threading.Lock()


def _font(size: int, *, pixel: bool = False):
    from PIL import ImageFont

    if pixel:
        return ImageFont.truetype(os.path.join(SITE_DIR, "fonts", "pixel.woff"), size)
    import matplotlib

    # DejaVu едет вместе с matplotlib — на сервере нет системных шрифтов.
    path = os.path.join(matplotlib.get_data_path(), "fonts", "ttf", "DejaVuSans-Bold.ttf")
    return ImageFont.truetype(path, size)


def _wrap(draw, text: str, font, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    line = ""
    for word in words:
        probe = f"{line} {word}".strip()
        if draw.textlength(probe, font=font) <= width or not line:
            line = probe
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def render_og(title: str, eyebrow: str, footer: str) -> bytes:
    """PNG 1200×630: тёплый градиент, тренер справа, заголовок слева.

    Текст рисуется сырым — экранировать его здесь нельзя (см. докстроку модуля)."""
    from PIL import Image, ImageDraw, ImageFilter

    w, h = OG_SIZE
    img = Image.new("RGB", (w, h), (18, 11, 9))
    # Свет лампы: мягкое оранжевое пятно сверху справа.
    glow = Image.new("RGB", (w, h), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((560, -260, 1360, 520), fill=(150, 62, 22))
    glow = glow.filter(ImageFilter.GaussianBlur(140))
    img = Image.blend(img, glow, 0.85)
    coach = Image.open(COACH_SOURCE).convert("RGB").resize((h, h), Image.LANCZOS)
    mask = Image.new("L", (h, h), 255)
    md = ImageDraw.Draw(mask)
    fade = 260
    for x in range(fade):
        md.line((x, 0, x, h), fill=int(255 * x / fade))
    img.paste(coach, (w - h, 0), mask)
    draw = ImageDraw.Draw(img)
    pad = 64
    text_w = w - h - pad + 120
    draw.text((pad, 70), eyebrow, font=_font(22, pixel=True), fill=(255, 179, 92))
    size = 66
    while True:
        font = _font(size)
        lines = _wrap(draw, title, font, text_w)
        if len(lines) <= 4 or size <= 40:
            break
        size -= 4
    lines = lines[:4]
    y = 140
    for line in lines:
        draw.text((pad, y), line, font=font, fill=(247, 237, 228))
        y += int(size * 1.18)
    draw.rounded_rectangle((pad, h - 112, pad + 64, h - 106), radius=3, fill=(255, 138, 61))
    draw.text((pad, h - 92), footer, font=_font(26), fill=(196, 168, 148))
    out = io.BytesIO()
    img.quantize(colors=256, method=Image.Quantize.MEDIANCUT).save(out, format="PNG", optimize=True)
    return out.getvalue()


def og_png(cache_key: str, title: str, eyebrow: str, footer: str) -> bytes:
    """Картинка из кэша памяти, с диска или свежая — в этом порядке."""
    with _og_lock:
        cached = _og_memory.get(cache_key)
        if cached is not None:
            _og_memory.move_to_end(cache_key)
            return cached
    safe = re.sub(r"[^a-z0-9_-]", "_", cache_key.lower())
    disk = os.path.join(OG_CACHE_DIR, f"{safe}.png")
    data: Optional[bytes] = None
    try:
        with open(disk, "rb") as fh:
            data = fh.read()
    except OSError:
        pass
    if data is None:
        data = render_og(title, eyebrow, footer)
        try:
            os.makedirs(OG_CACHE_DIR, exist_ok=True)
            tmp = f"{disk}.{os.getpid()}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, disk)
        except OSError:
            pass
    with _og_lock:
        _og_memory[cache_key] = data
        while len(_og_memory) > _OG_MEMORY_LIMIT:
            _og_memory.popitem(last=False)
    return data
