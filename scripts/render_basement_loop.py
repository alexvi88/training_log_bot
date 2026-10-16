#!/usr/bin/env python3
"""Живой подвал с вкладки «Меню» для сайта: бесшовная петля MP4/WebM.

На главной сайта (seo_pages._gym) стоит секция «Подвал»: «живая качалка на
главном экране — атлеты ходят и делают подходы, тренер кивает и хлопает,
доска показывает неделю». Скриншот это не показывал, поэтому сцена здесь
рисуется заново — тем же кодом, что в приложении, только портированным в
Python. Скрипт офлайновый, руками: в бот не импортируется, бот отдаёт готовые
файлы из media/site/gym/.

Что откуда (iOS-репозиторий, путь — `--ios`):

* спрайты, палитры, точки хвата, причёски — разбираются из Swift-литералов
  `TrainingLog/Features/Home/BasementGymSprites*.swift` программно (не
  переписаны руками): поменялись пиксели в приложении — перезапусти скрипт;
* цвета людей (кожа, футболки, волосы) — из `BasementGym.swift`;
* рисование — порт `BasementGymRenderer.swift` один в один: холст
  1080×600 (8 точек на единицу сцены 135×75), кирпичи с засевом 11, пол,
  доска, рамки плакатов, снаряды, штанга и гантели, тени, атлеты двух рядов
  с причёсками слоями, собака, тренер, пылинки в луче и таблица света;
* векторный слой карточки (`BasementGymCard.swift`, `GymBoardText.swift`) —
  плакаты из `Assets.xcassets/BasementPoster*` с тёплым налётом 8%, белые
  галочки дней и надпись доски пиксельным шрифтом
  `Resources/Fonts/BasementPixel-Regular.ttf`; строки доски — из
  `Localizable.strings`/`.stringsdict` обоих языков;
* шаг атлетов — порт `BasementGymSim.stepPerson`: скорость шага 0.018 ед/мс,
  кадр ходьбы `walkCycle` (+0.012/мс, смена каждые 1.1), повтор
  `repPhase` (+0.0016/мс — 625 мс на повтор, два кадра на снаряд), ряды
  (переход в задний ряд в 8 единицах до снаряда), запрыгивание на турник
  (200 + 550 мс), падение с турника с тем же ускорением, кадры отдыха; кадр
  тренера — `coachFrame` (хлопки по 130 мс 900 мс после подхода, кивок —
  пиксель вниз 500 мс, глоток из бутылки 1.3 с, дыхание); собака — рысь
  0.036 ед/мс по 90 мс на кадр, остановка у атлета, сидит и виляет хвостом.
  Частота — 30 кадров в секунду, как `TimelineView` карточки.

Чего нет и почему. Настоящий автомат зала выбирает всё генератором и не
повторяется, поэтому из него не выйдет петли. Здесь те же люди и те же
движения разложены сценарием (`CAST`): решения, которые в приложении
бросает генератор (куда пойти, сколько повторов, хлопнуть или кивнуть,
сколько отдыхать), записаны числами из тех же диапазонов, а длина
каждого цикла подогнана к длине петли. Атлет, который ушёл за край,
возвращается с другого — как новый посетитель из `spawn()`. Реплик в
облачках нет (тексты фраз меняются, на сайте они бы читались как обещание),
пасхалок тоже: это обычный будний зал. Состояние сцены — неделя с одной
тренировкой, как на скриншоте `docs/marketing/menu-gym/screens/*_1_week_scene.png`;
кадр 0 повторяет английский скриншот (кто где стоит).

Пылинкам в луче нужно 20–30 секунд на путь, петля короче, поэтому каждая
пылинка в конце петли плавно переходит в своё же положение из начала
(перекрёстное затухание по альфе) — шва нет.

Запуск (iOS-чекаут — свежий origin/main; без --ref читается рабочее дерево):

    python scripts/render_basement_loop.py --ios ../training_log_bot_ios --ref origin/main
    python scripts/render_basement_loop.py --ios ../training_log_bot_ios --ref origin/main \\
        --preview /tmp/anim          # плюс GIF-превью, кадры и сверка со скриншотом

Выход — media/site/gym/scene-<язык>.mp4 (H.264, yuv420p, без звука,
faststart) и scene-<язык>.webm (VP9); постер — прежние scene-<язык>.png.
ffmpeg — системный или из imageio_ffmpeg (`--ffmpeg` — свой путь).
"""

from __future__ import annotations

import argparse
import math
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "media", "site", "gym")
HOME = "TrainingLog/Features/Home/"
SCENE_SIZE = (1110, 617)  # seo_static.SCENE_SIZE — как скриншот симулятора
FPS = 30
DT = 1000.0 / FPS


# ---------------------------------------------------------------- iOS-исходники


class IOSSource:
    """Файлы iOS-репозитория: из рабочего дерева или `git show <ref>:<путь>`."""

    def __init__(self, root: str, ref: Optional[str]):
        self.root = root
        self.ref = ref

    def read_bytes(self, path: str) -> bytes:
        if self.ref:
            return subprocess.run(["git", "-C", self.root, "show", f"{self.ref}:{path}"],
                                  check=True, capture_output=True).stdout
        with open(os.path.join(self.root, path), "rb") as f:
            return f.read()

    def read(self, path: str) -> str:
        return self.read_bytes(path).decode("utf-8")


# ------------------------------------------------- разбор Swift-литералов

_TOKEN = re.compile(r'''
    (?P<ws>\s+)
  | (?P<lc>//[^\n]*)
  | (?P<bc>/\*.*?\*/)
  | (?P<str>"(?:\\.|[^"\\])*")
  | (?P<num>-?0x[0-9a-fA-F_]+|-?\d+(?:\.\d+)?)
  | (?P<enum>\.[A-Za-z_]\w*)
  | (?P<id>[A-Za-z_]\w*)
  | (?P<p>[\[\]():,])
  | (?P<other>.)
''', re.S | re.X)


def _tokens(text: str, pos: int):
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise ValueError(f"не разобрать Swift у {pos}: {text[pos:pos + 40]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind in ("ws", "lc", "bc"):
            continue
        yield kind, m.group(kind), pos


class _Parser:
    def __init__(self, text: str, pos: int):
        self.it = _tokens(text, pos)
        self.tok = next(self.it)

    def take(self, value=None):
        tok = self.tok
        if value is not None and tok[1] != value:
            raise ValueError(f"ждал {value!r}, а там {tok[1]!r}")
        self.tok = next(self.it, ("eof", "", 0))
        return tok

    def value(self):
        kind, text, _ = self.tok
        if text == "[":
            return self.collection()
        if kind == "str":
            self.take()
            return bytes(text[1:-1], "utf-8").decode("unicode_escape") if "\\" in text else text[1:-1]
        if kind == "num":
            self.take()
            t = text.replace("_", "")
            if "0x" in t:
                return int(t, 16)
            return float(t) if "." in t else int(t)
        if kind == "enum":
            self.take()
            return text[1:]
        if kind == "id":
            self.take()
            if self.tok[1] == "(":  # GymAnchor(x: 1, y: 2) → {"x": 1, "y": 2}
                self.take("(")
                args = {}
                while self.tok[1] != ")":
                    label = self.take()[1]
                    self.take(":")
                    args[label] = self.value()
                    if self.tok[1] == ",":
                        self.take()
                self.take(")")
                return args
            return text
        raise ValueError(f"неизвестный литерал {text!r}")

    def collection(self):
        self.take("[")
        if self.tok[1] == "]":
            self.take()
            return []
        if self.tok[1] == ":":
            self.take(":")
            self.take("]")
            return {}
        first = self.value()
        if self.tok[1] == ":":
            out = {}
            self.take(":")
            out[first] = self.value()
            while self.tok[1] == ",":
                self.take()
                if self.tok[1] == "]":
                    break
                key = self.value()
                self.take(":")
                out[key] = self.value()
            self.take("]")
            return out
        out = [first]
        while self.tok[1] == ",":
            self.take()
            if self.tok[1] == "]":
                break
            out.append(self.value())
        self.take("]")
        return out


def swift_static(text: str, name: str):
    """Значение `static let <name>[: Тип] = <литерал>` из Swift-файла."""
    m = re.search(r"static let " + re.escape(name) + r"\b[^=\n]*=\s*", text)
    if not m:
        raise KeyError(name)
    return _Parser(text, m.end()).value()


# --------------------------------------------------------------- данные сцены


@dataclass
class Art:
    frames: dict
    female_frames: dict
    idle_frames: dict
    female_idle_frames: dict
    hair_front: dict
    hair_side: dict
    head_anchors: dict
    hair_centre: dict
    hair_side_centre: dict
    ponytail: list
    ponytail_side: list
    ponytail_root: dict
    ponytail_offset: dict
    ponytail_side_root: dict
    ponytail_side_offset: dict
    anchors: dict
    palette: dict
    coach_palette: dict
    dog_palette: dict
    skin_tones: list
    shirt_colors: list
    hair_colors: list
    posters: dict
    font: bytes
    board: dict


def load_art(src: IOSSource) -> Art:
    sprites = src.read(HOME + "BasementGymSprites.swift")
    female = src.read(HOME + "BasementGymSpritesFemale.swift")
    idle = src.read(HOME + "BasementGymSpritesIdle.swift")
    hair = src.read(HOME + "BasementGymSpritesHair.swift")
    gym = src.read(HOME + "BasementGym.swift")
    posters = {}
    for name in ("BasementPoster", "BasementPosterLeft"):
        data = src.read_bytes(f"TrainingLog/Resources/Assets.xcassets/{name}.imageset/{name}.png")
        with tempfile.SpooledTemporaryFile() as f:
            f.write(data)
            f.seek(0)
            posters[name] = Image.open(f).convert("RGBA").copy()
    board = {}
    for lang in ("en", "ru"):
        strings = src.read(f"TrainingLog/Resources/{lang}.lproj/Localizable.strings")
        title = re.search(r'^"menu\.gym\.week_title"\s*=\s*"([^"]*)";', strings, re.M).group(1)
        plural = plistlib.loads(src.read_bytes(f"TrainingLog/Resources/{lang}.lproj/Localizable.stringsdict"))
        count = plural["menu.gym.week_count"]["n"]["one"].replace("%d", "1")
        board[lang] = (title, count)
    return Art(
        frames=swift_static(sprites, "frames"),
        female_frames=swift_static(female, "femaleFrames"),
        idle_frames=swift_static(idle, "idleFrames"),
        female_idle_frames=swift_static(idle, "femaleIdleFrames"),
        hair_front=swift_static(hair, "hairFront"),
        hair_side=swift_static(hair, "hairSide"),
        head_anchors=swift_static(hair, "headAnchors"),
        hair_centre=swift_static(hair, "hairCentre"),
        hair_side_centre=swift_static(hair, "hairSideCentre"),
        ponytail=swift_static(hair, "ponytail"),
        ponytail_side=swift_static(hair, "ponytailSide"),
        ponytail_root=swift_static(hair, "ponytailRoot"),
        ponytail_offset=swift_static(hair, "ponytailOffset"),
        ponytail_side_root=swift_static(hair, "ponytailSideRoot"),
        ponytail_side_offset=swift_static(hair, "ponytailSideOffset"),
        anchors=swift_static(sprites, "anchors"),
        palette=swift_static(sprites, "palette"),
        coach_palette=swift_static(sprites, "coachPalette"),
        dog_palette=swift_static(sprites, "dogPalette"),
        skin_tones=swift_static(gym, "skinTones"),
        shirt_colors=swift_static(gym, "shirtColors"),
        hair_colors=swift_static(gym, "hairColors"),
        posters=posters,
        font=src.read_bytes("TrainingLog/Resources/Fonts/BasementPixel-Regular.ttf"),
        board=board,
    )


# ------------------------------------------------------- константы сцены (BasementGymSim)

WIDTH, HEIGHT = 135.0, 75.0
FEET = 68.0
FIGURE_SCALE = 1.2
BACK_ROW_SCALE = 0.92
BACK_FEET = FEET - 3
BACK_ROW_DIM = 0.1
STATION_SCALE = FIGURE_SCALE * BACK_ROW_SCALE
STATION_FEET = BACK_FEET
PULL_BAR_Y = STATION_FEET - 24.75 * STATION_SCALE
PULL_HANG_LIFT = 4.5 * STATION_SCALE
MOUNT_CROUCH, MOUNT_RISE = 200.0, 550.0
DROP_GRAVITY = 2 * PULL_HANG_LIFT / (790 * 790)
RACK_BAR_Y = STATION_FEET - 13.75 * STATION_SCALE
COACH_X = 126.0
COACH_FEET = FEET + 2.5
DOG_WIDTH = 8.25 * FIGURE_SCALE
DOG_FEET = FEET + 1
DOG_SPEED = 0.036
DOG_OFFSTAGE = DOG_WIDTH / 2 + 1
STATIONS = {"pull": 12.0, "rack": 36.0, "bench": 82.0, "db": 104.0}
STATION_ORDER = ("pull", "rack", "bench", "db")
WALK_SPEED = 0.018
WALK_CYCLE_RATE = 0.012
REP_RATE = 0.0016
ROW_CHANGE_TIME = 400.0
ARRIVE_ZONE = 8.0

SCALE = 4       # PixelCanvas.scale
DENSITY = 2     # PixelCanvas.density
UNIT = SCALE * DENSITY
CW, CH = int(135 * UNIT), int(75 * UNIT)
Q = float(UNIT)
STATION_K = DENSITY * STATION_SCALE


def js_round(v: float) -> int:
    return int(math.floor(v + 0.5))


def rnd(v: float) -> int:
    """Swift `.rounded()` — половина от нуля."""
    return int(math.floor(v + 0.5)) if v >= 0 else -int(math.floor(-v + 0.5))


def rgb_tuple(c: int):
    return ((c >> 16) & 0xff, (c >> 8) & 0xff, c & 0xff)


def shade(c: int) -> int:
    r, g, b = rgb_tuple(c)
    return (rnd(r * 0.72) << 16) | (rnd(g * 0.72) << 8) | rnd(b * 0.72)


def lighten(c: int, k: float) -> int:
    return sum(min(255, rnd(v + (255 - v) * k)) << s for v, s in zip(rgb_tuple(c), (16, 8, 0), strict=True))


def dim(c: int, tone: float) -> int:
    return sum(rnd(v * tone) << s for v, s in zip(rgb_tuple(c), (16, 8, 0), strict=True))


# ----------------------------------------------------------------- холст


class Canvas:
    def __init__(self, width: int = 0, height: int = 0, background: int = 0xffffff):
        self.a = np.empty((height or CH, width or CW, 3), np.uint8)
        self.a[:] = rgb_tuple(background)

    @property
    def w(self):
        return self.a.shape[1]

    @property
    def h(self):
        return self.a.shape[0]

    def copy(self) -> "Canvas":
        c = Canvas.__new__(Canvas)
        c.a = self.a.copy()
        return c

    def fill(self, x, y, w, h, color: int, alpha: float = 1.0):
        x0, y0, x1, y1 = x, y, x + w, y + h
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(self.w, x1), min(self.h, y1)
        if x0 >= x1 or y0 >= y1:
            return
        src = np.array(rgb_tuple(color), np.float64)
        if alpha >= 1:
            self.a[y0:y1, x0:x1] = src.astype(np.uint8)
            return
        dst = self.a[y0:y1, x0:x1].astype(np.float64)
        v = dst * (1 - alpha) + src * alpha
        self.a[y0:y1, x0:x1] = np.clip(np.floor(v + 0.5), 0, 255).astype(np.uint8)

    def fill_rgb(self, x, y, w, h, color: int):
        x0, y0, x1, y1 = max(0, x), max(0, y), min(self.w, x + w), min(self.h, y + h)
        if x0 < x1 and y0 < y1:
            self.a[y0:y1, x0:x1] = rgb_tuple(color)

    def P(self, x, y, w, h, color, alpha=1.0):
        self.fill(js_round(x * Q), js_round(y * Q), js_round(w * Q), js_round(h * Q), color, alpha)

    def F(self, x, y, w, h, color, alpha=1.0):
        self.fill(js_round(x * Q), js_round(y * Q), w * DENSITY, h * DENSITY, color, alpha)

    def apply_light(self, mul: np.ndarray, add: np.ndarray):
        b = self.a.astype(np.int32)
        k = mul.astype(np.int32)
        w = add.astype(np.int32)
        r = ((b[..., 0] * k) >> 8) + w
        g = ((b[..., 1] * k) >> 8) + ((w * 3) >> 2)
        bl = ((b[..., 2] * k) >> 8) + (w >> 1)
        self.a = np.minimum(255, np.stack([r, g, bl], -1)).astype(np.uint8)


@dataclass
class Pen:
    left: int
    top: int
    k: float
    tone: float = 1.0

    @staticmethod
    def placed(cx, bottom, width, height, scale, tone=1.0) -> "Pen":
        k = DENSITY * scale
        left = js_round(cx * Q - width * k / 2)
        top = js_round(bottom * Q) - js_round(height * k)
        return Pen(left, top, k, tone)

    def x(self, i):
        return self.left + js_round(i * self.k)

    def y(self, j):
        return self.top + js_round(j * self.k)

    def at(self, i, j) -> "Pen":
        return Pen(self.x(i), self.y(j), self.k)

    def shifted(self, dy) -> "Pen":
        return Pen(self.left, self.top + js_round(dy * self.k), self.k, self.tone)

    def set(self, o: Canvas, i, j, color):
        x0, y0 = self.x(i), self.y(j)
        o.fill_rgb(x0, y0, self.x(i + 1) - x0, self.y(j + 1) - y0, dim(color, self.tone) if self.tone < 1 else color)


@dataclass
class Sprite:
    width: int
    height: int
    rgb: np.ndarray    # H×W×3
    mask: np.ndarray   # H×W bool


def blit(o: Canvas, s: Sprite, pen: Pen, i0=0, j0=0):
    """`draw` рисовалки: пиксель (x, y) спрайта — прямоугольник пера (i0+x, j0+y)."""
    xs = np.array([pen.x(i0 + i) for i in range(s.width + 1)])
    ys = np.array([pen.y(j0 + j) for j in range(s.height + 1)])
    rgb, mask = s.rgb, s.mask
    if pen.tone < 1:
        rgb = np.floor(rgb.astype(np.float64) * pen.tone + 0.5).astype(np.uint8)
    cx0, cx1 = max(0, xs[0]), min(o.w, xs[-1])
    cy0, cy1 = max(0, ys[0]), min(o.h, ys[-1])
    if cx0 >= cx1 or cy0 >= cy1:
        return
    cols = np.searchsorted(xs, np.arange(cx0, cx1), side="right") - 1
    rows = np.searchsorted(ys, np.arange(cy0, cy1), side="right") - 1
    m = mask[rows][:, cols]
    region = o.a[cy0:cy1, cx0:cx1]
    region[m] = rgb[rows][:, cols][m]


# ----------------------------------------------------------- рисовалка


@dataclass
class Person:
    name: str
    skin: int
    shirt: int
    hair: int
    female: bool
    hair_style: str
    x: float = 0.0
    phase: str = "walk"
    target: Optional[float] = None
    station: Optional[str] = None
    reps: int = 0
    rep_phase: float = 0.0
    dir: float = 1.0
    walk_cycle: float = 0.0
    mode: int = 0
    rest: float = 0.0
    mount: float = 0.0
    drop_offset: float = 0.0
    drop_velocity: float = 0.0
    landed: float = 0.0
    depth: float = 0.0
    idle: str = "stand"
    rest_age: float = 0.0
    gone: bool = False
    leaving: bool = False

    @property
    def rep_progress(self):
        return (1 - math.cos(self.rep_phase * math.pi * 2)) / 2


class Renderer:
    def __init__(self, art: Art):
        self.art = art
        self.cache = {}
        self.bricks = self._bricks(FEET - 6)
        self.footprints = self._footprints()
        self.mul, self.add = self._lighting()
        self.base = self._base()

    # --- спрайты
    def rows(self, name: str, female: bool):
        a = self.art
        if name == "ponytail":
            return a.ponytail
        if name == "ponytail_side":
            return a.ponytail_side
        if name.startswith("hair."):
            rest = name[5:]
            if rest.startswith("side."):
                return a.hair_side.get(rest[5:])
            return a.hair_front.get(rest)
        if female:
            return a.female_frames.get(name) or a.female_idle_frames.get(name)
        return a.frames.get(name) or a.idle_frames.get(name)

    def sprite(self, name, palette: dict, key: str, flip: bool, female=False) -> Optional[Sprite]:
        ck = (name, key, flip, female)
        if ck in self.cache:
            return self.cache[ck]
        rows = self.rows(name, female)
        if rows is None:
            return None
        h, w = len(rows), max(len(r) for r in rows)
        rgb = np.zeros((h, w, 3), np.uint8)
        mask = np.zeros((h, w), bool)
        for y, row in enumerate(rows):
            for x, ch in enumerate(row):
                c = palette.get(ch)
                if c is None:
                    continue
                xx = w - 1 - x if flip else x
                rgb[y, xx] = rgb_tuple(c)
                mask[y, xx] = True
        s = Sprite(w, h, rgb, mask)
        self.cache[ck] = s
        return s

    def person_palette(self, p: Person) -> dict:
        a = self.art
        pal = dict(a.palette)
        shirt, skin, hair = a.shirt_colors[p.shirt], a.skin_tones[p.skin], a.hair_colors[p.hair]
        pal.update({"t": shirt, "T": shade(shirt), "u": lighten(shirt, 0.22),
                    "s": skin, "S": shade(skin), "l": lighten(skin, 0.28),
                    "h": hair, "H": lighten(hair, 0.18), "j": shade(hair)})
        return pal

    def person_sprite(self, name, p: Person, flip=False):
        return self.sprite(name, self.person_palette(p), f"{p.skin}-{p.shirt}-{p.hair}", flip, p.female)

    # --- неподвижное
    @staticmethod
    def _bricks(limit):
        state = 11
        colors = [0x7a3324, 0x8a3d2b, 0x6c2c1f, 0x93452f]
        out = []
        y = 0
        while y < limit:
            x = -((y // 4) % 2) * 4
            while x < WIDTH:
                state = (state * 16807) % 2147483647
                out.append((float(x), float(y), colors[int(state / 2147483647 * 4)]))
                x += 8
            y += 4
        return out

    def _footprints(self):
        out = {}
        for group in (self.art.frames, self.art.idle_frames):
            for name, rows in group.items():
                xs = [x for line in rows[-4:] for x, ch in enumerate(line) if ch != "."]
                if xs:
                    out[name] = (min(xs), max(xs))
        return out

    @staticmethod
    def _light_at(x, y, floor=FEET, cone_fade=40.0):
        dx = x - 68.5
        spread = 9 + np.maximum(0, y - 2.75) * 0.55
        cone = np.exp(-(dx * dx) / (2 * spread * spread)) * np.exp(-np.power(np.maximum(0, y - 34) / cone_fade, 2))
        pool = np.exp(-np.power(dx / 26, 2) - np.power((y - floor) / 6, 2))
        return 24 * cone + 12 * pool

    def _lighting(self):
        py, px = np.mgrid[0:CH, 0:CW].astype(np.float64)
        y, x = (py + 0.5) / Q, (px + 0.5) / Q
        light = self._light_at(x, y)
        cx, cy = WIDTH / 2, HEIGHT / 2
        nx, ny = (x - cx) / cx, (y - cy) / cy
        r = np.minimum(1, np.sqrt(nx * nx + ny * ny) / math.sqrt(2))
        t = np.clip((r - 0.4) / 0.6, 0, 1)
        dark = 0.25 * t * t * (3 - 2 * t)
        mul = np.clip(np.floor((1 - dark) * 256 + 0.5), 0, 256)
        add = np.clip(np.floor(light + 0.5), 0, 255)
        return mul.astype(np.uint16), add.astype(np.uint8)

    def _base(self) -> Canvas:
        o = Canvas()
        floor = FEET - 6
        o.P(0, 0, WIDTH, floor, 0x4a221a)
        for x, y, col in self.bricks:
            o.P(x, y, 7.5, 3.5, col)
            o.F(x, y, 30, 1, 0xffdcbe, 0.16)
            o.F(x, y + 3.25, 30, 1, 0x000000, 0.25)
            if int(x * 7 + y * 3) % 5 == 0:
                o.F(x + 2, y + 1, 4, 1, 0x000000, 0.3)
        o.fill(0, 0, CW, 4 * UNIT, 0x000000, 0.35)
        o.P(0, floor, WIDTH, HEIGHT - floor, 0x2b2725)
        floor_rows = js_round((HEIGHT - floor) * SCALE)
        fx = 0.0
        while fx < WIDTH:
            o.F(fx, floor, 1, floor_rows, 0x1f1b19)
            o.F(fx + 0.25, floor, 1, floor_rows, 0x36312e)
            fx += 9
        o.P(0, floor, WIDTH, 0.5, 0x141211)
        # доска недели: рамка, блик, поле; неделя — четверг
        o.P(43, 3, 51, 24, 0x3a2a20)
        o.F(43, 3, int(51 * SCALE), 1, 0x5a4434)
        o.P(43.75, 3.75, 49.5, 22.5, 0x1f2a24)
        for i, trained in enumerate(WEEK):
            x = 48 + i * 6
            o.P(x, 19, 5, 5, 0xd9a441 if trained else 0x3a4a42)
            o.F(x, 19, 5 * SCALE, 1, 0xf0c060 if trained else 0x4a5a52)
        o.P(12, 32, 0.5, 0.5, 0x000000)  # гвоздь
        for poster in (POSTER_LEFT, POSTER_RIGHT):
            px, py, pw, ph = poster
            ox, oy, ow, oh = px - 1.5, py - 1.5, pw + 3, ph + 3
            o.P(ox, oy, ow, oh, 0x3a2414)
            o.P(ox + 0.75, oy + 0.75, ow - 1.5, oh - 1.5, 0x6b4424)
            o.P(px, py, pw, ph, 0x2a1a10)
        return o

    # --- тени
    def shadow(self, o, pen: Pen, lo, hi, floor_y, lift=0.0):
        strength = max(0, 1 - max(0, lift) / 10)
        if strength <= 0.05 or hi < lo:
            return
        shrink = max(0.5, 1 - max(0, lift) / 14)
        centre = (lo + hi) / 2
        half = ((hi - lo) / 2 + 1) * shrink
        a, b = js_round(centre - half), js_round(centre + half)
        rh = max(1, js_round(pen.k))
        core, edge = 0.35 * strength, 0.35 * 0.5 * strength
        self._shadow_row(o, pen, a, b, floor_y - rh, rh, core, edge)
        self._shadow_row(o, pen, a + 2, b - 2, floor_y, rh, core, edge)

    @staticmethod
    def _shadow_row(o, pen, a, b, y, height, core, edge):
        if b < a:
            return
        x0, x1 = pen.x(a), pen.x(b + 1)
        end = max(1, js_round(2 * pen.k))
        if x1 - x0 <= 2 * end:
            o.fill(x0, y, x1 - x0, height, 0, edge)
            return
        o.fill(x0, y, end, height, 0, edge)
        o.fill(x0 + end, y, x1 - x0 - 2 * end, height, 0, core)
        o.fill(x1 - end, y, end, height, 0, edge)

    def figure_shadow(self, o, name, pen, width, flip, floor, lift, fallback="stand"):
        foot = self.footprints.get(name) or self.footprints.get(fallback)
        if not foot:
            return
        lo = width - 1 - foot[1] if flip else foot[0]
        hi = width - 1 - foot[0] if flip else foot[1]
        self.shadow(o, pen, lo, hi, js_round(floor * Q), lift)

    def station_shadow(self, o, sx, dx0, dx1):
        pen = Pen(js_round((sx + dx0 * STATION_SCALE) * Q), 0, STATION_K)
        pixels = max(1, js_round((dx1 - dx0) * SCALE))
        self.shadow(o, pen, 0, pixels - 1, js_round(STATION_FEET * Q))

    # --- штанга и гантели
    STEEL, STEEL_DARK, STEEL_LIGHT = 0xb8bcc0, 0x7a7e84, 0xe4e6e8
    PLATE, PLATE_DARK, PLATE_LIGHT, PLATE_RIM = 0xa8432c, 0x762c1c, 0xc8644a, 0x3a160c
    DB, DB_LIGHT, DB_DARK = 0x2a2c2f, 0x4a4d52, 0x161718

    def barbell_front(self, o, pen):
        half, collar, plate_half, thick, plates = 48, 28, 10, 4, 2
        sleeve_from = collar + plates * thick + 1
        for x in range(-half, half):
            ax = abs(x + 0.5)
            if ax < collar:
                pen.set(o, x, -1, self.STEEL_LIGHT)
                pen.set(o, x, 0, self.STEEL_DARK)
            elif ax > sleeve_from:
                pen.set(o, x, -2, self.STEEL_LIGHT)
                pen.set(o, x, -1, self.STEEL)
                pen.set(o, x, 0, self.STEEL)
                pen.set(o, x, 1, self.STEEL_DARK)
        for side in (-1, 1):
            cx = collar if side > 0 else -collar - 1
            for y in range(-3, 3):
                pen.set(o, cx, y, self.STEEL_DARK)
            for i in range(plates):
                x0 = collar + 1 + thick * i if side > 0 else -collar - 1 - thick * (i + 1)
                outer = thick - 1 if side > 0 else 0
                for xx in range(thick):
                    for y in range(-plate_half, plate_half):
                        edge_row = y == -plate_half or y == plate_half - 1
                        if edge_row and (xx == 0 or xx == thick - 1):
                            continue
                        c = self.PLATE
                        if xx == outer:
                            c = self.PLATE_RIM
                        elif y == -plate_half or (y == -plate_half + 1 and 0 < xx < thick - 1):
                            c = self.PLATE_LIGHT
                        elif y >= plate_half - 2:
                            c = self.PLATE_DARK
                        pen.set(o, x0 + xx, y, c)

    def barbell_end(self, o, pen):
        r = 10.0
        for y in range(-10, 10):
            for x in range(-10, 10):
                d = math.hypot(x + 0.5, y + 0.5)
                if d > r:
                    continue
                c = self.PLATE
                if d > r - 1:
                    c = self.PLATE_RIM
                elif d > r - 2:
                    c = self.PLATE_LIGHT if x + y < -2 else self.PLATE_DARK
                elif d < 1.2:
                    c = self.STEEL_LIGHT
                elif d < 2.2:
                    c = self.STEEL
                elif d < 3.4:
                    c = self.STEEL_DARK
                pen.set(o, x, y, c)

    def dumbbell(self, o, pen, held):
        for x in range(-4, 4):
            if held and -2 <= x < 2:
                continue
            pen.set(o, x, -1, self.STEEL_LIGHT)
            pen.set(o, x, 0, self.STEEL_DARK)
        for side in (-1, 1):
            cx = 4 if side > 0 else -5
            for y in range(-2, 2):
                pen.set(o, cx, y, self.DB_LIGHT)
            x0 = 5 if side > 0 else -8
            for xx in range(3):
                for y in range(-4, 5):
                    if abs(y) == 4 and (xx == 0 or xx == 2):
                        continue
                    c = self.DB
                    if y == -4 or (xx == 0 and (side < 0 or y < 0)):
                        c = self.DB_LIGHT
                    if y == 4:
                        c = self.DB_DARK
                    pen.set(o, x0 + xx, y, c)

    # --- снаряды
    @staticmethod
    def srect(o, sx, dx, up, w, h, c):
        s = STATION_SCALE
        o.P(sx + dx * s, STATION_FEET - up * s, w * s, h * s, c)

    def bench_pen(self, height, tone=1.0):
        left = js_round((STATIONS["bench"] + BENCH_SPRITE_DX * STATION_SCALE) * Q)
        top = js_round(STATION_FEET * Q) - js_round(height * STATION_K)
        return Pen(left, top, STATION_K, tone)

    def pull_bolt_point(self):
        crouch_h = len(self.art.frames["coach_clap1"]) - 12  # coachCrouchRows 60..<72
        pen = Pen.placed(BAR_FIX_COACH_X, COACH_FEET, 58, crouch_h, FIGURE_SCALE)
        return js_round((STATIONS["pull"] + 5.5 * STATION_SCALE) * Q), pen.y(31)

    def equipment(self, o, rack_bar, bench_bar, dumbbells_in_use):
        pull, rack, bench, shelf = (STATIONS[k] for k in STATION_ORDER)
        for dx in (-6.75, 4.5):
            self.station_shadow(o, pull, dx, dx + 2.25)
        for dx in (-6.5, 5.5):
            self.station_shadow(o, rack, dx - 2, dx + 3)
        self.station_shadow(o, bench, -14.25, 2)
        self.station_shadow(o, shelf, -9.5, 9.5)
        bar_up = 24.75 + 1
        for dx in (-6.0, 5.25):
            self.srect(o, pull, dx, bar_up, 0.75, bar_up, 0x1c1c1e)
            self.srect(o, pull, dx, bar_up, 0.25, bar_up, 0x3a3a3e)
            self.srect(o, pull, dx - 0.75, 0.5, 2.25, 0.5, 0x3a3a3e)
        bx, by = self.pull_bolt_point()
        bolt = Pen(bx, by, STATION_K)
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                bolt.set(o, i, j, 0x2a2a2e)
        bolt.set(o, 0, -1, 0x6a6a70)
        bar = Pen(js_round((pull - 6.75 * STATION_SCALE) * Q),
                  js_round((PULL_BAR_Y - 0.25 * STATION_SCALE) * Q), STATION_K)
        for i in range(js_round(13.5 * SCALE)):
            bar.set(o, i, 0, 0xd0d0d0)
            bar.set(o, i, 1, 0x9a9a9a)
        hook_up = 13.75
        for dx in (-6.5, 5.5):
            self.srect(o, rack, dx, 15.5, 1, 15.5, 0x5a5e63)
            self.srect(o, rack, dx, 15.5, 0.25, 15.5, 0x8f949a)
            up = 15.0
            while up > 1:
                self.srect(o, rack, dx + 0.5, up, 0.25, 0.25, 0x26282b)
                up -= 2
            inner = dx < 0
            self.srect(o, rack, dx + (1 if inner else -0.75), hook_up - 0.25, 0.75, 0.5, 0x8f949a)
            self.srect(o, rack, dx + (1.5 if inner else -0.75), hook_up + 0.5, 0.25, 0.75, 0x8f949a)
            self.srect(o, rack, dx - 1.5, 7, 4, 0.5, 0x55595e)
            self.srect(o, rack, dx - 2, 0.5, 5, 0.5, 0x45494e)
        if rack_bar:
            self.barbell_front(o, Pen(js_round(rack * Q), js_round(RACK_BAR_Y * Q), STATION_K))
        pad_up, pad_dx = 4.75, BENCH_SPRITE_DX + 1.5
        self.srect(o, bench, pad_dx, pad_up, 13, 1.25, 0x7a3528)
        self.srect(o, bench, pad_dx, pad_up, 13, 0.25, 0x9a4a3a)
        self.srect(o, bench, pad_dx, pad_up - 1.25, 13, 0.25, 0x2a1410)
        for leg in (pad_dx + 1, pad_dx + 11.25):
            self.srect(o, bench, leg, pad_up - 1.5, 0.75, pad_up - 1.5, 0x55595e)
        self.srect(o, bench, pad_dx + 0.5, 0.5, 12, 0.5, 0x45494e)
        bench_rows = self.art.frames["bench_up"]
        ay = self.art.anchors["bench_up"][0]["y"]
        rack_up = (len(bench_rows) - ay) / SCALE
        self.srect(o, bench, -13.5, rack_up + 0.25, 0.75, rack_up + 0.25, 0x5a5e63)
        self.srect(o, bench, -13.5, rack_up + 0.25, 0.25, rack_up + 0.25, 0x8f949a)
        self.srect(o, bench, -12.75, rack_up - 0.5, 1.75, 0.5, 0x8f949a)
        self.srect(o, bench, -14.25, 0.5, 2.5, 0.5, 0x45494e)
        if bench_bar:
            pen = self.bench_pen(len(bench_rows))
            p = Pen(js_round((bench + BENCH_RACK_DX * STATION_SCALE) * Q), pen.y(ay), STATION_K)
            self.barbell_end(o, p)
        self.srect(o, shelf, -9.5, 8, 19, 0.75, 0x1c1c1e)
        self.srect(o, shelf, -9.5, 8, 19, 0.25, 0x45484d)
        for leg in (-9.0, 8.25):
            self.srect(o, shelf, leg, 8 - 0.75, 0.75, 8 - 0.75, 0x26282b)
        self.srect(o, shelf, -9.5, 3, 19, 0.5, 0x26282b)
        for index, dx in enumerate((-7.25, -2.75, 1.75, 6.25)):
            if dumbbells_in_use and index in (1, 2):
                continue
            p = Pen(js_round((shelf + dx * STATION_SCALE) * Q), js_round((STATION_FEET - 9.25 * STATION_SCALE) * Q),
                    STATION_K)
            self.dumbbell(o, p, held=False)

    # --- люди
    def layer_corner(self, name, width, pin, dx, dy, frame_width, flip):
        head = self.art.head_anchors.get(name)
        if head is None:
            return None
        local = head["x"] + dx - pin["x"]
        x = frame_width - local - width if flip else local
        return x, head["y"] + dy - pin["y"]

    def draw_body(self, o, s: Sprite, name, p: Person, pen, flip=False):
        a = self.art
        side = name.startswith("bench")
        if p.hair_style == "ponytail":
            layer = "ponytail_side" if side else "ponytail"
            root = a.ponytail_side_root if side else a.ponytail_root
            off = a.ponytail_side_offset if side else a.ponytail_offset
            swing = 0 if side else (-1 if name == "walk1" else 0)
            width = len(self.rows(layer, True)[0])
            c = self.layer_corner(name, width, root, off["x"] + swing, off["y"], s.width, flip)
            tail = self.person_sprite(layer, p, flip)
            if c and tail:
                blit(o, tail, pen, c[0], c[1])
        blit(o, s, pen)
        style = {"side_part": "sidePart"}.get(p.hair_style, p.hair_style)
        layer = "hair." + ("side." if side else "") + style
        rows = self.rows(layer, False)
        if rows:
            pin = a.hair_side_centre if side else a.hair_centre
            c = self.layer_corner(name, len(rows[0]), pin, 0, 0, s.width, flip)
            hair = self.person_sprite(layer, p, flip)
            if c and hair:
                blit(o, hair, pen, c[0], c[1])

    @staticmethod
    def row_scale(depth):
        if depth >= 1:
            return STATION_SCALE
        if depth <= 0:
            return FIGURE_SCALE
        return FIGURE_SCALE * (1 + (BACK_ROW_SCALE - 1) * depth)

    @staticmethod
    def row_feet(depth):
        return FEET + (BACK_FEET - FEET) * depth

    def figure(self, o, name, p, cx, floor, lift=0.0, depth=0.0, flip=False):
        s = self.person_sprite(name, p, flip)
        if s is None:
            raise KeyError(name)
        pen = Pen.placed(cx, floor - lift, s.width, s.height, self.row_scale(depth), 1 - BACK_ROW_DIM * depth)
        self.figure_shadow(o, name, pen, s.width, flip, floor, lift)
        self.draw_body(o, s, name, p, pen, flip)
        return pen

    def hang(self, o, s, name, p, x, top):
        pen = Pen(js_round(x * Q) - js_round(s.width * STATION_K / 2), top, STATION_K, 1 - BACK_ROW_DIM)
        floor_row = js_round(STATION_FEET * Q)
        lift = (floor_row - pen.y(s.height)) / Q
        self.figure_shadow(o, name, pen, s.width, False, STATION_FEET, lift)
        self.draw_body(o, s, name, p, pen)

    @staticmethod
    def depth_of(p: Person):
        return 1.0 if p.phase in ("work", "mount", "drop") else p.depth

    def rest_frame(self, p: Person):
        turned = p.dir < 0
        age, left = p.rest_age, p.rest
        if p.idle == "drink":
            return ("rest_drink1" if age < 250 or left < 250 else "rest_drink2"), turned
        if p.idle == "stretch":
            return ("press_down" if age < 200 or left < 200 else "press_up"), False
        if p.idle == "phone":
            return "rest_phone", turned
        return "rest_turn", turned

    def draw_person(self, o, p: Person):
        depth = self.depth_of(p)
        floor = self.row_feet(depth)
        flip = p.dir < 0
        if p.phase in ("walk", "leave"):
            frames = ["walk1", "walk_pass", "walk2", "walk_pass"]
            name = "stand" if p.walk_cycle == 0 else frames[int(p.walk_cycle / 1.1) % 4]
            self.figure(o, name, p, p.x, floor, 0, depth, flip)
        elif p.phase == "mount":
            x = p.target if p.target is not None else p.x
            if p.mount <= MOUNT_CROUCH:
                self.figure(o, "pull_crouch", p, x, STATION_FEET, 0, 1)
                return
            s = self.person_sprite("pull_hang", p)
            grip = self.art.anchors["pull_hang"][0]
            lift = mount_lift(p.mount)
            rest = js_round((PULL_HANG_LIFT - lift) * Q)
            top = js_round(PULL_BAR_Y * Q) - js_round(grip["y"] * STATION_K) + rest
            self.hang(o, s, "pull_hang", p, x, top)
        elif p.phase == "drop":
            self.figure(o, "stand", p, p.x, floor, -p.drop_offset, depth, flip)
        elif p.phase == "rest":
            name, f = self.rest_frame(p)
            self.figure(o, name, p, p.x, floor, 0, depth, f)
        elif p.phase == "work":
            self.work(o, p, p.target if p.target is not None else p.x, p.rep_progress)

    def work(self, o, p, x, prog):
        hi = prog > 0.5
        floor = STATION_FEET
        a = self.art.anchors
        if p.station == "rack":
            name = "squat_bottom" if hi else "squat_top"
            s = self.person_sprite(name, p)
            pen = Pen.placed(x, floor, s.width, s.height, STATION_SCALE, 1 - BACK_ROW_DIM)
            self.figure_shadow(o, name, pen, s.width, False, floor, 0)
            self.barbell_front(o, pen.at(a[name][0]["x"], a[name][0]["y"]))
            self.draw_body(o, s, name, p, pen)
        elif p.station == "bench":
            name = "bench_up" if hi else "bench_down"
            s = self.person_sprite(name, p)
            pen = self.bench_pen(s.height, 1 - BACK_ROW_DIM)
            self.figure_shadow(o, name, pen, s.width, False, floor, 0)
            self.draw_body(o, s, name, p, pen)
            self.barbell_end(o, pen.at(a[name][0]["x"], a[name][0]["y"]))
        elif p.station == "pull":
            name = "pull_top" if hi else "pull_hang"
            s = self.person_sprite(name, p)
            top = js_round(PULL_BAR_Y * Q) - js_round(a[name][0]["y"] * STATION_K)
            self.hang(o, s, name, p, x, top)
        else:
            press = p.station == "db" and p.mode == 1
            name = ("press_up" if hi else "press_down") if press else ("curl_up" if hi else "curl_down")
            pen = self.figure(o, name, p, x, floor, 0, 1)
            for an in a.get(name, []):
                self.dumbbell(o, pen.at(an["x"], an["y"]), held=True)

    # --- собака и тренер
    def draw_dog(self, o, dog: "Dog"):
        name = dog.frame()
        flip = dog.facing < 0
        s = self.sprite(name, self.art.dog_palette, "dog", flip)
        pen = Pen.placed(dog.x, DOG_FEET, s.width, s.height, FIGURE_SCALE)
        self.figure_shadow(o, name, pen, s.width, flip, DOG_FEET, 0, "dog_run1")
        blit(o, s, pen)

    def draw_coach(self, o, name, dy):
        s = self.sprite(name, self.art.coach_palette, "coach", False)
        pen = Pen.placed(COACH_X, COACH_FEET, s.width, s.height, FIGURE_SCALE).shifted(dy)
        lift = max(0, -dy) / SCALE * FIGURE_SCALE
        self.figure_shadow(o, name, pen, s.width, False, COACH_FEET, lift, "coach")
        blit(o, s, pen)

    def dust(self, o, t_ms, loop_ms):
        # перекрёстное затухание: в конце петли пылинка — в своём же положении из её начала
        w = t_ms / loop_ms
        for spec in DUST:
            for when, weight in ((t_ms, 1 - w), (t_ms - loop_ms, w)):
                if weight <= 0:
                    continue
                x, y, alpha = dust_point(spec, when)
                o.fill(js_round(x * Q), js_round(y * Q), DENSITY, DENSITY, 0xffe6bf, alpha * weight)


# Геометрия из рисовалки (единицы прототипа от точки снаряда).
BENCH_SPRITE_DX = -12.5
BENCH_RACK_DX = -11.25
BAR_FIX_COACH_X = 25.0  # BasementGymSim.barFixCoachX: болт турника — на высоте ладоней присевшего тренера
POSTER_RIGHT = (115.0, 7.0, 13.0, 17.0)
POSTER_LEFT = (WIDTH - 128.0, 7.0, 13.0, 17.0)
WEEK = [False, False, False, True, False, False, False]
DUST = [(-0.55, 3, 0.0021, 1.2, 0.0), (0.35, 17, 0.0017, 0.9, 1.7), (-0.15, 29, 0.0024, 1.5, 3.1),
        (0.6, 41, 0.0019, 1.0, 4.4), (-0.4, 47, 0.0015, 1.3, 5.6), (0.1, 9, 0.0022, 0.8, 2.3),
        (0.8, 24, 0.0018, 1.1, 0.9), (-0.8, 36, 0.0020, 0.7, 3.8)]


def dust_point(spec, t):
    sx, sy, speed, sway, phase = spec
    y = 6 + math.fmod(sy + t * speed, 50.0)
    if y < 6:
        y += 50
    half = 3 + (y - 2.75) * 0.42
    x = 68.5 + sx * half + math.sin(t * 0.0006 + phase) * sway
    alpha = 0.3 + 0.15 * math.sin(t * 0.0021 + phase * 3)
    return x, y, alpha


def mount_lift(elapsed):
    if elapsed <= MOUNT_CROUCH:
        return 0.0
    t = min(1, (elapsed - MOUNT_CROUCH) / MOUNT_RISE)
    return PULL_HANG_LIFT * (1 - (1 - t) ** 3)


# ----------------------------------------------------------- шаг (BasementGymSim)


@dataclass
class Dog:
    x: float
    dir: float = 1.0
    facing: float = 1.0
    stop_x: Optional[float] = None
    pause_left: float = 0.0
    paused: float = 0.0
    run_cycle: float = 0.0
    pausing: bool = False

    def frame(self):
        if not self.pausing:
            return f"dog_run{int(self.run_cycle / 90) % 4 + 1}"
        if self.paused < 250:
            return "dog_sit"
        return "dog_wag1" if int((self.paused - 250) / 160) % 2 == 0 else "dog_wag2"


class Actor:
    """Атлет со сценарием: решения генератора — из списка, по кругу."""

    def __init__(self, person: Person, script: list, spawn_x: Optional[float] = None):
        self.p = person
        self.script = script
        self.i = 0
        self.spawn_x = spawn_x
        self.gone_frames = 0
        self.finished = []  # моменты конца подходов — тренеру
        self.world = [self]
        self.last_station = None
        self.lonely = []    # подходы, кончившиеся, когда больше никто не работал

    def next(self, kind):
        while True:
            item = self.script[self.i % len(self.script)]
            self.i += 1
            if item[0] == kind:
                return item
            raise ValueError(f"{self.p.name}: ждал {kind}, а в сценарии {item}")

    def step(self, dt, frame):
        p = self.p
        if p.gone:
            self.gone_frames -= 1
            if self.gone_frames > 0:
                return
            # снова заходит из-за края — тот же человек, как новый посетитель
            p.gone = False
            p.x = self.spawn_x
            p.depth = 0.0
            p.walk_cycle = 0.0
            p.phase = "walk"
            p.target = None
            p.station = None
            return
        if p.phase in ("walk", "leave"):
            if p.target is None:
                item = self.next("go")
                if item[1] == "leave":
                    p.phase = "leave"
                    p.target = item[2]
                else:
                    p.station = item[1]
                    p.target = STATIONS[item[1]]
                    p.phase = "walk"
            d = p.target - p.x
            if d != 0:
                p.dir = 1.0 if d > 0 else -1.0
            if abs(d) < 0.6:
                if p.phase == "leave":
                    p.gone = True
                    self.gone_frames = self.next("gone")[1]
                    return
                p.depth = 1.0
                if p.station == "pull":
                    p.phase = "mount"
                    p.mount = 0.0
                    return
                self.start_work()
                return
            step = min(abs(d), dt * WALK_SPEED)
            p.x += (1 if d > 0 else -1) * step
            p.walk_cycle += dt * WALK_CYCLE_RATE
            remaining = abs(d) - step
            to_station = p.phase == "walk" and p.station is not None and remaining <= ARRIVE_ZONE
            goal = 1.0 if to_station else 0.0
            gap = goal - p.depth
            if gap != 0:
                change = dt / ROW_CHANGE_TIME
                if goal == 1 and step > 0:
                    change = max(change, abs(gap) * step / max(step, remaining))
                p.depth += (1 if gap > 0 else -1) * min(abs(gap), change)
        elif p.phase == "mount":
            p.mount += dt
            if p.mount >= MOUNT_CROUCH + MOUNT_RISE:
                p.mount = MOUNT_CROUCH + MOUNT_RISE
                self.start_work()
        elif p.phase == "drop":
            p.drop_velocity += dt * DROP_GRAVITY
            p.drop_offset = min(0.0, p.drop_offset + p.drop_velocity * dt)
            if p.drop_offset >= 0:
                p.landed += dt
                if p.landed > 220:
                    p.station = None
                    p.drop_velocity = 0.0
                    p.landed = 0.0
                    self.start_rest(self.pending_rest)
        elif p.phase == "rest":
            p.rest -= dt
            p.rest_age += dt
            if p.rest <= 0:
                p.phase = "walk"
                p.target = None
        elif p.phase == "work":
            p.rep_phase += dt * REP_RATE
            if p.rep_phase < p.reps:
                return
            item = self.next("done")  # ("done", "clap"|"nod", rest_frames, idle)
            self.finished.append((frame, item[1]))
            self.last_station = p.station
            # в приложении (`keepsGymBusy`) подход тянется, пока никто другой не в подходе
            if not any(a is not self and a.p.phase == "work" and not a.p.gone for a in self.world):
                self.lonely.append(frame)
            if p.station == "pull":
                p.phase = "drop"
                p.drop_offset = -PULL_HANG_LIFT
                p.drop_velocity = 0.0
                p.landed = 0.0
                p.x = p.target
                self.pending_rest = item[2:]
                return
            p.station = None
            self.start_rest(item[2:])

    def start_work(self):
        p = self.p
        item = self.next("work")  # ("work", reps, mode)
        p.phase = "work"
        p.depth = 1.0
        p.mode = item[2]
        p.reps = item[1]
        p.rep_phase = 0.0

    def start_rest(self, rest):
        frames, idle = rest
        p = self.p
        p.phase = "rest"
        p.rest_age = 0.0
        p.rest = frames * DT - DT / 2  # ровно `frames` кадров отдыха
        p.idle = idle
        # вполоборота к ближайшему свободному снаряду, кроме того, с которого ушёл
        busy = {a.p.station for a in self.world if a is not self and not a.p.gone}
        free = [st for st in STATION_ORDER if st not in busy and st != self.last_station]
        goal = min((STATIONS[st] for st in free), key=lambda x: abs(x - p.x), default=WIDTH / 2)
        if goal != p.x:
            p.dir = 1.0 if goal > p.x else -1.0


# ----------------------------------------------------------- сценарий петли

# Кто в зале — как на английском скриншоте (кадр 0): атлет спрыгнул с
# турника, двое идут перед стойками, атлетка жмёт лёжа, тренер в углу.
# Внешность — индексы палитр BasementGymSim (кожа, футболка, волосы).
LOOK = {
    "A": dict(skin=1, shirt=4, hair=2, female=False, hair_style="crop"),
    "B": dict(skin=1, shirt=0, hair=1, female=True, hair_style="bun"),
    "C": dict(skin=2, shirt=5, hair=3, female=False, hair_style="side_part"),
    "D": dict(skin=2, shirt=4, hair=4, female=False, hair_style="crop"),
}

LOOP_FRAMES = 450  # 15 с при 30 к/с
LEFT_IN, RIGHT_IN = -6.0, WIDTH + 6     # откуда заходят (`spawn`)
LEFT_OUT, RIGHT_OUT = -8.0, WIDTH + 8   # куда уходят (`pick`, `.leave`)


def cast():
    """Сценарии по кругу — что выпало бы генератору зала. Каждый снаряд
    достаётся одному, так что выбор всегда из свободных и не тот, с
    которого только что ушёл; уход из зала — как у `pick` (15% или нет
    свободных). ("work", повторов, гантели: 0 — бицепс, 1 — жим),
    ("done", реакция тренера, кадров отдыха, чем занят на отдыхе),
    ("gone", кадров за краем — добивается до длины петли)."""
    A = Actor(Person("A", x=LEFT_IN, **LOOK["A"]), [
        ("go", "pull"), ("work", 10, 0), ("done", "nod", 40, "drink"),
        ("go", "leave", LEFT_OUT), ("gone", 0),
    ], spawn_x=LEFT_IN)
    B = Actor(Person("B", x=RIGHT_IN, **LOOK["B"]), [
        ("go", "bench"), ("work", 9, 0), ("done", "nod", 42, "phone"),
        ("go", "leave", RIGHT_OUT), ("gone", 0),
    ], spawn_x=RIGHT_IN)
    C = Actor(Person("C", x=RIGHT_IN, **LOOK["C"]), [
        ("go", "rack"), ("work", 8, 0), ("done", "clap", 38, "stretch"),
        ("go", "leave", LEFT_OUT), ("gone", 0),
    ], spawn_x=RIGHT_IN)
    D = Actor(Person("D", x=RIGHT_IN, **LOOK["D"]), [
        ("go", "db"), ("work", 8, 1), ("done", "clap", 49, "drink"),
        ("go", "leave", LEFT_OUT), ("gone", 0),
    ], spawn_x=RIGHT_IN)
    return [A, B, C, D]


def cycle_frames(actor: Actor) -> int:
    """Сколько кадров один круг сценария (от начала до возврата в начало)."""
    n = 0
    start_i = actor.i
    while True:
        actor.step(DT, n)
        n += 1
        if actor.i - start_i >= len(actor.script) and actor.p.phase in ("walk", "leave") \
                and actor.p.target is None and not actor.p.gone:
            return n
        if n > 10000:
            raise RuntimeError("сценарий не замыкается")


def fit_cast(loop_frames: int):
    """Добивает каждый сценарий до длины петли: свободные кадры уходят в
    отдых (кто не уходит из зала) или в паузу за краем (кто уходит)."""
    actors = cast()
    for a in actors:
        probe = cast()[[x.p.name for x in actors].index(a.p.name)]
        n = cycle_frames(probe)
        slack = loop_frames - n
        if slack < 0:
            raise RuntimeError(f"{a.p.name}: круг {n} кадров длиннее петли {loop_frames}")
        # слот добивки: последний отдых у тех, кто в зале, и пауза за краем у
        # ушедших; ноль и единица там — один кадр, поэтому сверяем замером
        slot = next(k for k in range(len(a.script) - 1, -1, -1)
                    if a.script[k][0] == "gone" or (a.script[k][0] == "done" and a.script[k][2] == 0))
        value = slack
        for _ in range(3):
            item = a.script[slot]
            a.script[slot] = (item[0], value) if item[0] == "gone" else item[:2] + (value,) + item[3:]
            check = Actor(Person(a.p.name, **LOOK[a.p.name], x=a.p.x), a.script, a.spawn_x)
            got = cycle_frames(check)
            if got == loop_frames:
                break
            value += loop_frames - got
        else:
            raise RuntimeError(f"{a.p.name}: не подогнать к петле")
    return actors


# Сдвиг каждого по его кругу: в кадре 0 все стоят как на скриншоте.
OFFSETS = {"A": None, "B": None, "C": None, "D": None}
DOG_START = 20       # кадр петли, где собака выбегает слева (атлетка ещё на скамье)
DOG_STOP_FOR = 1750  # мс сидит у атлетки на скамье и виляет хвостом
SIP_START = 340      # кадр петли, где тренер пьёт (1.3 с), — между хлопками


def find_offsets(loop_frames: int):
    """Где на своём круге каждый в кадре 0 — ближайшее к английскому
    скриншоту: A падает с турника (ступни на 2.75 ед. над полом), B жмёт
    лёжа (гриф внизу), C идёт влево к стойкам (x 42, уже на треть в заднем
    ряду), D уходит влево (x 47)."""
    want = {
        "A": lambda p: abs(-p.drop_offset - 2.75) if p.phase == "drop" else None,
        "B": lambda p: abs(p.rep_phase - 2.1) if p.phase == "work" else None,
        "C": lambda p: abs(p.x - 42.075) if p.phase == "walk" and p.dir < 0 and p.station == "rack" else None,
        "D": lambda p: abs(p.x - 46.95) if p.phase == "leave" and p.dir < 0 else None,
    }
    out = {}
    for a in fit_cast(loop_frames):
        best = None
        for n in range(loop_frames):
            a.step(DT, n)
            score = want[a.p.name](a.p)
            if score is not None and (best is None or score < best[0]):
                best = (score, n + 1)
        if best is None:
            raise RuntimeError(f"{a.p.name}: нет кадра как на скриншоте")
        out[a.p.name] = best[1]
    return out


class Scene:
    def __init__(self, art: Art, loop_frames: int = LOOP_FRAMES):
        self.r = Renderer(art)
        self.loop = loop_frames
        self.loop_ms = loop_frames * DT
        offsets = find_offsets(loop_frames)
        self.actors = fit_cast(loop_frames)
        # каждый — на своё место круга; потом зал живёт целую петлю, и
        # записывается вторая: в ней всё, что тянется из прошлой (хлопки
        # тренера), уже то же, что перейдёт в следующую
        for a in self.actors:
            for _ in range(offsets[a.p.name]):
                a.step(DT, -1)
            a.finished.clear()
            a.lonely.clear()
        for a in self.actors:
            a.world = self.actors
        self.reactions = []
        self.states = []
        for f in range(2 * loop_frames):
            for a in self.actors:
                a.step(DT, f)
            if f >= loop_frames:
                self.states.append([_snapshot(a.p) for a in self.actors])
        for a in self.actors:
            for f, kind in a.finished:
                self.reactions.append((f - loop_frames, kind))
            for f in (f for f in a.lonely if loop_frames <= f < 2 * loop_frames):
                print(f"внимание: подход {a.p.name} кончился на кадре {f - loop_frames}, когда больше никто"
                      " не работал — в приложении он бы продолжился", file=sys.stderr)
        # петля замкнута: состояние после неё — то же, что в её начале
        for a in self.actors:
            a.step(DT, 2 * loop_frames)
        if not _close(self.states[0], [_snapshot(a.p) for a in self.actors]):
            raise RuntimeError("петля не замкнулась")

    def coach(self, frame):
        """Кадр и сдвиг тренера (`coachFrame`, `drawCoach`): хлопки 900 мс,
        кивок 500 мс после подхода, глоток раз за петлю, дыхание — два вдоха
        за петлю (в приложении период 6 с; здесь подогнан к длине петли)."""
        t = frame * DT
        clap = nod = 0.0
        for f, kind in self.reactions:
            for shift in (-self.loop, 0, self.loop):
                age = (frame - (f + shift)) * DT
                if age >= 0:
                    if kind == "clap" and age < 900 - DT / 2:
                        clap = 900 - age
                    if kind == "nod" and age < 500 - DT / 2:
                        nod = 500 - age
        if clap > 0:
            name = "coach_clap1" if int(t / 130) % 2 == 1 else "coach_clap2"
        else:
            sip = ((frame - SIP_START) % self.loop) * DT
            name = "coach_sip" if sip < 1300 else "coach"
        breaths = max(1, round(self.loop_ms / (2 * math.pi * 950)))
        breath = 1 if math.sin(2 * math.pi * breaths * t / self.loop_ms) > 0.55 else 0
        return name, -breath + (1 if nod > 0 else 0)

    def dog(self, frame) -> Optional[Dog]:
        """Пробежка собаки слева направо с остановкой у атлетки на скамье (`stepDog`)."""
        n = (frame - DOG_START) % self.loop
        dog = Dog(x=-DOG_OFFSTAGE, stop_x=STATIONS["bench"] - (DOG_WIDTH / 2 + 3))
        for _ in range(n + 1):
            if dog.pausing:
                dog.paused += DT
                dog.pause_left -= DT
                if dog.pause_left <= 0:
                    dog.pausing = False
                    dog.stop_x = None
            else:
                step = DT * DOG_SPEED
                dog.run_cycle += DT
                if dog.stop_x is not None and abs(dog.stop_x - dog.x) <= step:
                    dog.x = dog.stop_x
                    dog.pausing = True
                    dog.pause_left = DOG_STOP_FOR
                    dog.paused = 0.0
                else:
                    dog.x += step
            if dog.x > WIDTH + DOG_OFFSTAGE:
                return None
        return dog

    def frame(self, f) -> Canvas:
        o = self.r.base.copy()
        people = [_restore(s) for s in self.states[f]]
        def lifting(st):
            return any(p.phase == "work" and p.station == st for p in people)

        self.r.equipment(o, rack_bar=not lifting("rack"), bench_bar=not lifting("bench"),
                         dumbbells_in_use=any(p.station == "db" and p.phase == "work" for p in people))
        for back in (True, False):
            for p in people:
                if p.gone or (Renderer.depth_of(p) >= 0.5) != back:
                    continue
                self.r.draw_person(o, p)
        dog = self.dog(f)
        if dog:
            self.r.draw_dog(o, dog)
        name, dy = self.coach(f)
        self.r.draw_coach(o, name, dy)
        self.r.dust(o, f * DT, self.loop_ms)
        o.apply_light(self.r.mul, self.r.add)
        return o


def _close(a, b) -> bool:
    for x, y in zip(a, b, strict=True):
        for k in x:
            vx, vy = x[k], y[k]
            if isinstance(vx, float) and isinstance(vy, float):
                if abs(vx - vy) > 1e-6:
                    return False
            elif vx != vy:
                return False
    return True


def _snapshot(p: Person) -> dict:
    return dict(p.__dict__)


def _restore(d: dict) -> Person:
    p = Person.__new__(Person)
    p.__dict__.update(d)
    return p


# ----------------------------------------------------------- векторный слой карточки


class Overlay:
    """Что карточка рисует поверх растра: плакаты, галочки, надпись доски.
    Размер — как у скриншота: 370 pt @3x, единица сцены — 8.22 точки."""

    def __init__(self, art: Art, lang: str):
        w, h = SCENE_SIZE
        self.u = w / WIDTH                  # пикселей на единицу
        pt = self.u / 3                     # точек на единицу (scale в GymBoardText)
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        for asset, (fx, fy, fw, fh) in (("BasementPosterLeft", POSTER_LEFT), ("BasementPoster", POSTER_RIGHT)):
            # ближайший пиксель в дробный прямоугольник поля, как Canvas с .interpolation(.none)
            src = np.asarray(art.posters[asset].convert("RGB")).astype(np.float64)
            sh, sw = src.shape[:2]
            x0, y0, x1, y1 = fx * self.u, fy * self.u, (fx + fw) * self.u, (fy + fh) * self.u
            xs = np.arange(int(x0), int(math.ceil(x1)))
            ys = np.arange(int(y0), int(math.ceil(y1)))
            xs = xs[(xs + 0.5 >= x0) & (xs + 0.5 < x1)]
            ys = ys[(ys + 0.5 >= y0) & (ys + 0.5 < y1)]
            ci = np.clip(np.floor((xs + 0.5 - x0) / (x1 - x0) * sw).astype(int), 0, sw - 1)
            ri = np.clip(np.floor((ys + 0.5 - y0) / (y1 - y0) * sh).astype(int), 0, sh - 1)
            pix = src[ri][:, ci] * (1 - 0.08) + np.array([255, 190, 120]) * 0.08  # тёплый налёт лампы
            tile = Image.fromarray(np.floor(pix + 0.5).astype(np.uint8), "RGB").convert("RGBA")
            layer.alpha_composite(tile, (int(xs[0]), int(ys[0])))
        # галочки — векторной линией 0.75 ед. с круглыми концами (сглаживание — ×4)
        ss = 4
        big = Image.new("L", (w * ss, h * ss), 0)
        d = ImageDraw.Draw(big)
        lw = 0.75 * self.u * ss
        for i, trained in enumerate(WEEK):
            if not trained:
                continue
            x, y = (48 + i * 6) * self.u * ss, 19 * self.u * ss
            pts = [(x + 1.125 * self.u * ss, y + 2.625 * self.u * ss), (x + 2.125 * self.u * ss, y + 3.625 * self.u * ss),
                   (x + 3.875 * self.u * ss, y + 1.375 * self.u * ss)]
            d.line(pts, fill=255, width=round(lw), joint="curve")
            for px, py in (pts[0], pts[-1]):
                d.ellipse((px - lw / 2, py - lw / 2, px + lw / 2, py + lw / 2), fill=255)
        mask = big.resize((w, h), Image.LANCZOS)
        white = Image.new("RGBA", (w, h), (255, 255, 255, 255))
        layer = _over(layer, white, mask)
        # надпись доски: Press Start 2P (BasementPixel), кегль 3.5 ед. целыми точками
        size_pt = max(1, rnd(3.5 * pt))
        font = ImageFont.truetype(_font_file(art.font), size_pt * 3)
        title, count = art.board[lang]
        line_h = 5.25 * pt * 3
        top = 6 * self.u
        text = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        td = ImageDraw.Draw(text)
        ascent, descent = font.getmetrics()
        shadow = 0.25 * self.u
        for k, (s, color) in enumerate(((title, (0xef, 0xe9, 0xdc)), (count, (0xd9, 0xa4, 0x41)))):
            width = font.getlength(s)
            cx = (43 + 51 / 2) * self.u
            x = cx - width / 2
            y = top + k * line_h + (line_h - (ascent + descent)) / 2
            td.text((x + shadow, y + shadow), s, font=font, fill=(0, 0, 0, round(0.6 * 255)))
            td.text((x, y), s, font=font, fill=color + (255,))
        layer.alpha_composite(text)
        self.layer = layer

    def compose(self, canvas: Canvas) -> Image.Image:
        # растр 1080×600 растягивается на карточку без сглаживания
        w, h = SCENE_SIZE
        cols = np.minimum(CW - 1, np.floor((np.arange(w) + 0.5) * CW / w).astype(int))
        rows = np.minimum(CH - 1, np.floor((np.arange(h) + 0.5) * CH / h).astype(int))
        img = Image.fromarray(canvas.a[rows][:, cols], "RGB").convert("RGBA")
        img.alpha_composite(self.layer)
        return img.convert("RGB")


def _over(base: Image.Image, top: Image.Image, mask: Image.Image) -> Image.Image:
    t = top.copy()
    t.putalpha(mask)
    out = base.copy()
    out.alpha_composite(t)
    return out


_FONT_PATH = None


def _font_file(data: bytes) -> str:
    global _FONT_PATH
    if _FONT_PATH is None:
        fd, _FONT_PATH = tempfile.mkstemp(suffix=".ttf")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    return _FONT_PATH


# ----------------------------------------------------------- полоса «неделя в подвале»

KEY = 0xff00fe  # фон ленты кадров — в палитрах его нет, на выходе прозрачный


def coach_walk_rows(rows: list, columns: range) -> list:
    """`GymSprites.liftLeg`: нога в столбцах `columns` ниже колена (строка 74)
    поднята на 2 пикселя — шаг тренера (`coachWalkFrame`)."""
    grid = [list(r) for r in rows]
    for y in range(74, len(grid)):
        for x in columns:
            if x < len(grid[y]):
                src = y + 2
                grid[y][x] = grid[src][x] if src < len(grid) else "."
    return ["".join(r) for r in grid]


def band_sprites(art: Art, out_dir: str):
    """Ленты кадров для полосы на главной (seo_static.SPRITES) — из тех же
    спрайтов, пиксель в пиксель (без масштаба): тренер на ходу — четыре
    кадра шага (`coach_walk1`, `coach`, `coach_walk2`, `coach`, по 160 мс),
    атлет с гантелями — бицепс вниз и вверх (по полповтора, 312 мс)."""
    r = Renderer.__new__(Renderer)
    r.art, r.cache = art, {}
    coach = art.frames["coach"]
    width = len(coach[0])
    walk = {"coach_walk1": coach_walk_rows(coach, range(0, 29)),
            "coach_walk2": coach_walk_rows(coach, range(29, width))}
    frames = []
    for name in ("coach_walk1", "coach", "coach_walk2", "coach"):
        rows = walk.get(name, coach)
        o = Canvas(width, len(rows), KEY)
        s = r.sprite(name, art.coach_palette, "coach", False) if name == "coach" else None
        if s is None:
            art.frames.setdefault(name, rows)
            s = r.sprite(name, art.coach_palette, "coach", False)
        blit(o, s, Pen(0, 0, 1.0))
        frames.append(o)
    _save_strip(frames, os.path.join(out_dir, "coach_walk.png"))
    p = Person("band", skin=1, shirt=0, hair=0, female=False, hair_style="crop")
    pad = 10  # гантель выходит за кадр атлета на 8 пикселей
    frames = []
    for name in ("curl_down", "curl_up"):
        s = r.person_sprite(name, p)
        o = Canvas(s.width + 2 * pad, s.height, KEY)
        pen = Pen(pad, 0, 1.0)
        r.draw_body(o, s, name, p, pen)
        for an in art.anchors[name]:
            r.dumbbell(o, pen.at(an["x"], an["y"]), held=True)
        frames.append(o)
    _save_strip(frames, os.path.join(out_dir, "athlete_curl.png"))


def _save_strip(frames: list, path: str):
    """Кадры подряд, обрезанные по общему краю непрозрачного; фон — прозрачный."""
    h = max(f.h for f in frames)
    w = max(f.w for f in frames)
    stack = np.zeros((len(frames), h, w, 4), np.uint8)
    for i, f in enumerate(frames):
        rgba = np.concatenate([f.a, np.full(f.a.shape[:2] + (1,), 255, np.uint8)], 2)
        key = (f.a == np.array(rgb_tuple(KEY), np.uint8)).all(2)
        rgba[key] = 0
        stack[i, :f.h, :f.w] = rgba
    solid = stack[..., 3].max(0) > 0
    cols = np.where(solid.any(0))[0]
    rows = np.where(solid.any(1))[0]
    crop = stack[:, rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
    strip = np.concatenate(list(crop), 1)
    Image.fromarray(strip, "RGBA").save(path, optimize=True)
    print(f"{path}: {crop.shape[2]}×{crop.shape[1]} × {len(frames)} кадров")


# ----------------------------------------------------------- видео


def video_frame(img: Image.Image) -> Image.Image:
    """Кадр для yuv420p: высота чётная — нижняя строка повторена (617 → 618);
    на странице видео кадрируется в те же 1110×617 (`object-fit: cover`)."""
    w, h = img.size
    if h % 2 == 0:
        return img
    out = Image.new("RGB", (w, h + 1))
    out.paste(img, (0, 0))
    out.paste(img.crop((0, h - 1, w, h)), (0, h))
    return out


def find_ffmpeg(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit("нет ffmpeg: поставь его или imageio-ffmpeg, или передай --ffmpeg")


def encode(ffmpeg: str, frames_dir: str, out_base: str, crf_mp4: int, crf_webm: int):
    pattern = os.path.join(frames_dir, "%04d.png")
    common = [ffmpeg, "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", pattern, "-an"]
    subprocess.run(common + ["-c:v", "libx264", "-preset", "veryslow", "-tune", "animation", "-crf", str(crf_mp4),
                             "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_base + ".mp4"], check=True)
    subprocess.run(common + ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(crf_webm), "-row-mt", "1",
                             "-deadline", "good", "-cpu-used", "1", "-pix_fmt", "yuv420p", out_base + ".webm"],
                   check=True)


# ----------------------------------------------------------- точка входа


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ios", required=True, help="чекаут training_log_bot_ios")
    ap.add_argument("--ref", help="git-ревизия iOS-репо (например origin/main); без неё — рабочее дерево")
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--ffmpeg")
    ap.add_argument("--crf-mp4", type=int, default=28)
    ap.add_argument("--crf-webm", type=int, default=47)
    ap.add_argument("--preview", help="папка для GIF-превью, кадров и сверки со скриншотом")
    ap.add_argument("--langs", default="en,ru")
    ap.add_argument("--band-only", action="store_true", help="только ленты кадров полосы на главной")
    args = ap.parse_args(argv)

    art = load_art(IOSSource(args.ios, args.ref))
    band_sprites(art, args.out)
    if args.band_only:
        return
    scene = Scene(art)
    canvases = [scene.frame(f) for f in range(scene.loop)]
    ffmpeg = find_ffmpeg(args.ffmpeg)
    for lang in args.langs.split(","):
        overlay = Overlay(art, lang)
        with tempfile.TemporaryDirectory() as tmp:
            images = []
            for f, c in enumerate(canvases):
                img = overlay.compose(c)
                video_frame(img).save(os.path.join(tmp, f"{f:04d}.png"))
                if args.preview:
                    images.append(img)
            base = os.path.join(args.out, f"scene-{lang}")
            encode(ffmpeg, tmp, base, args.crf_mp4, args.crf_webm)
            for ext in ("mp4", "webm"):
                print(f"{base}.{ext}: {os.path.getsize(base + '.' + ext) // 1024} KB")
        if args.preview:
            ref = None
            try:
                data = IOSSource(args.ios, args.ref).read_bytes(
                    f"docs/marketing/menu-gym/screens/{lang}_1_week_scene.png")
                ref = Image.open(__import__("io").BytesIO(data))
            except (OSError, subprocess.CalledProcessError):
                pass
            write_preview(args.preview, lang, images, scene, ref)


def fidelity(img: Image.Image, ref: Image.Image) -> str:
    """Сверка кадра со скриншотом: доля точек, где канал разошёлся больше
    чем на 8 и на 32 из 255, — по всему кадру и без людей (неподвижная часть:
    стена, доска, плакаты, пол, снаряды)."""
    a = np.asarray(img.convert("RGB")).astype(int)
    b = np.asarray(ref.convert("RGB").resize(img.size)).astype(int)
    d = np.abs(a - b).max(2)
    still = np.ones(d.shape, bool)
    for x0, y0, x1, y1 in PEOPLE_BOXES:
        still[y0:y1, x0:x1] = False
    return (f"весь кадр: >8 — {(d > 8).mean():.1%}, >32 — {(d > 32).mean():.1%}; "
            f"без людей: >8 — {(d[still] > 8).mean():.1%}, >32 — {(d[still] > 32).mean():.1%}")


# Где на скриншотах стоят люди и тренер (точки 1110×617) — их нет в «неподвижной» сверке.
PEOPLE_BOXES = [(40, 300, 170, 545), (180, 330, 460, 570), (540, 330, 720, 545), (790, 340, 920, 545),
                (480, 320, 700, 590), (950, 320, 1110, 600)]


def write_preview(folder, lang, images, scene, ref=None):
    os.makedirs(folder, exist_ok=True)
    images[0].save(os.path.join(folder, f"frame0-{lang}.png"))
    if ref is not None:
        print(f"кадр 0 против скриншота ({lang}): {fidelity(images[0], ref)}")
    small = [im.resize((555, 308), Image.NEAREST) for im in images[::2]]
    small[0].save(os.path.join(folder, f"loop-{lang}.gif"), save_all=True, append_images=small[1:],
                  duration=int(2 * DT), loop=0, optimize=True)
    for f in (0, scene.loop // 3, 2 * scene.loop // 3):
        images[f].save(os.path.join(folder, f"frame{f:03d}-{lang}.png"))


if __name__ == "__main__":
    main()
