"""Схема тела спереди и сзади — та же, что у приложения (`BodyMap.swift`,
`WorkoutBodyMap.swift` в `training_log_bot_ios`): разбор контуров
`body_map_data`, какие мышцы красит тренировка и как фигура ложится в кадр.

Здесь только геометрия и логика: какие зоны закрасить, где их многоугольники
в точках карточки. Цвета и холст — у того, кто рисует (`finish_slides.py`).

Правила — как у приложения:

- Упражнение каталога красит своей разметкой (`exercise_muscles`): основные
  мышцы — основным тоном, помогающие — светлее; зона, основная хоть у одного
  упражнения, — основная.
- Без разметки (своё упражнение) — зона всей его группы основным тоном
  (`REGIONS`, `MuscleGroupRegions` приложения). Своя группа и «Другое» зоны
  не имеют — такое упражнение фигуру не красит.
- Красить нечего — фигуры нет (`None`), и слайда с ней тоже.
"""

from __future__ import annotations

import dataclasses
import functools
import re
from typing import Iterable, Mapping, Optional, Sequence

import body_map_data

Point = tuple[float, float]
Polygon = list[Point]
Rect = tuple[float, float, float, float]  # x, y, ширина, высота

FRONT, BACK = "front", "back"

# Зоны, которые знает схема (`ExerciseMuscleText.knownSlugs`): разметка
# сервера могла дорасти до новой — её зона тогда просто не красится.
KNOWN_SLUGS = frozenset({
    "chest", "deltoids", "biceps", "triceps", "forearm", "abs", "obliques",
    "trapezius", "upper-back", "lower-back", "neck", "gluteal", "quadriceps",
    "hamstring", "adductors", "calves",
})

# Часть поля 0 0 724 1448, где фигура есть на самом деле (`BodyMapShape.fullFigure`):
# сверху и снизу у исходника пустые поля. Пропорция та же 1:2.
FULL_FIGURE: Rect = (38.0, 72.0, 648.0, 1296.0)


# --- разбор SVG-пути ----------------------------------------------------------

_TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_ARGS = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "Z": 0}
# Отрезков на одну кривую при спрямлении: в поле 724 по ширине фигура на
# слайде — сотни точек, 12 отрезков на кривую дают гладкий край без граней.
_CURVE_STEPS = 12


def _cubic(p0: Point, p1: Point, p2: Point, p3: Point, steps: int = _CURVE_STEPS) -> list[Point]:
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
        out.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                    a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return out


def _quad(p0: Point, p1: Point, p2: Point, steps: int = _CURVE_STEPS) -> list[Point]:
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        out.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                    u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))
    return out


def parse_path(d: str) -> list[Polygon]:
    """SVG-путь → замкнутые многоугольники (по одному на подпуть), кривые
    спрямлены. Команды M L H V C S Q T Z, абсолютные и относительные; лишние
    пары после M — неявные L, повтор аргументов — повтор команды, как по SVG."""
    tokens = _TOKEN.findall(d)
    polygons: list[Polygon] = []
    current: Polygon = []
    x = y = 0.0
    start = (0.0, 0.0)
    last_ctrl: Optional[Point] = None
    last_cmd = ""
    i = 0

    def close() -> None:
        nonlocal current
        if len(current) >= 3:
            polygons.append(current)
        current = []

    command = ""
    while i < len(tokens):
        token = tokens[i]
        if token.isalpha():
            command = token
            i += 1
            if command in "Zz":
                close()
                x, y = start
                last_ctrl, last_cmd = None, "Z"
                continue
        elif not command:
            i += 1
            continue
        upper = command.upper()
        n = _ARGS[upper]
        if i + n > len(tokens) or any(tokens[i + k].isalpha() for k in range(n)):
            # Обрывок без чисел — пропустить до следующей команды.
            while i < len(tokens) and not tokens[i].isalpha():
                i += 1
            continue
        args = [float(tokens[i + k]) for k in range(n)]
        i += n
        rel = command.islower()
        ox, oy = (x, y) if rel else (0.0, 0.0)
        if upper == "M":
            close()
            x, y = args[0] + ox, args[1] + oy
            start = (x, y)
            current = [(x, y)]
            # Следующие пары после M — неявные L (m — l).
            command = "l" if rel else "L"
            last_ctrl = None
        elif upper == "L":
            x, y = args[0] + ox, args[1] + oy
            current.append((x, y))
            last_ctrl = None
        elif upper == "H":
            x = args[0] + ox
            current.append((x, y))
            last_ctrl = None
        elif upper == "V":
            y = args[0] + oy
            current.append((x, y))
            last_ctrl = None
        elif upper in "CS":
            if upper == "C":
                c1 = (args[0] + ox, args[1] + oy)
                c2 = (args[2] + ox, args[3] + oy)
                end = (args[4] + ox, args[5] + oy)
            else:
                c1 = (2 * x - last_ctrl[0], 2 * y - last_ctrl[1]) if last_ctrl and last_cmd in "CS" else (x, y)
                c2 = (args[0] + ox, args[1] + oy)
                end = (args[2] + ox, args[3] + oy)
            current.extend(_cubic((x, y), c1, c2, end))
            x, y = end
            last_ctrl = c2
        elif upper in "QT":
            if upper == "Q":
                c = (args[0] + ox, args[1] + oy)
                end = (args[2] + ox, args[3] + oy)
            else:
                c = (2 * x - last_ctrl[0], 2 * y - last_ctrl[1]) if last_ctrl and last_cmd in "QT" else (x, y)
                end = (args[0] + ox, args[1] + oy)
            current.extend(_quad((x, y), c, end))
            x, y = end
            last_ctrl = c
        last_cmd = upper
    close()
    return polygons


@functools.lru_cache(maxsize=2)
def side_polygons(side: str) -> dict[str, tuple[Polygon, ...]]:
    """Разобранные контуры стороны: слаг → многоугольники. Один раз на процесс."""
    source = body_map_data.FRONT if side == FRONT else body_map_data.BACK
    return {slug: tuple(parse_path(d)) for slug, d in source.items()}


def _bbox(polygons: Iterable[Polygon]) -> Optional[tuple[float, float, float, float]]:
    xs: list[float] = []
    ys: list[float] = []
    for polygon in polygons:
        for px, py in polygon:
            xs.append(px)
            ys.append(py)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


# --- зоны групп (MuscleGroupRegions) -----------------------------------------


@dataclasses.dataclass(frozen=True)
class Region:
    """Зона группы: сторона, которой она видна лучше всего, её мышцы и те же
    мышцы с другой стороны; парные мышцы рук превью берёт одной рукой."""

    side: str
    slugs: frozenset[str]
    other_side: frozenset[str] = frozenset()
    crops_one_side: bool = False

    def slugs_on(self, side: str) -> frozenset[str]:
        return self.slugs if side == self.side else self.other_side


# Ключ — машинный слаг встроенной группы (`seed_data.muscle_group_slug`).
# Ноги — целиком, от паха до голени: группа «Ноги» одна на все мышцы ног.
REGIONS: dict[str, Region] = {
    "chest": Region(FRONT, frozenset({"chest"})),
    "back": Region(BACK, frozenset({"upper-back", "lower-back", "trapezius"})),
    "legs": Region(
        FRONT, frozenset({"quadriceps", "adductors", "knees", "tibialis", "calves"}),
        frozenset({"hamstring", "gluteal", "adductors", "calves"}),
    ),
    "shoulders": Region(FRONT, frozenset({"deltoids"}), frozenset({"deltoids"}), crops_one_side=True),
    "biceps": Region(FRONT, frozenset({"biceps"}), crops_one_side=True),
    "triceps": Region(BACK, frozenset({"triceps"}), frozenset({"triceps"}), crops_one_side=True),
    "abs": Region(FRONT, frozenset({"abs", "obliques"})),
}


def group_key(group_name: Optional[str]) -> Optional[str]:
    """Слаг зоны по названию группы на любом языке («Грудь», «Chest»,
    «Brust» → "chest"); своя группа и «Другое» — None (зоны нет)."""
    import seed_data

    if not group_name:
        return None
    canonical = seed_data.canonical_muscle_group_name(group_name)
    slug = seed_data.muscle_group_slug(canonical) if canonical else None
    return slug if slug in REGIONS else None


# --- фигура тренировки (WorkoutBodyMap.figure) --------------------------------


@dataclasses.dataclass(frozen=True)
class Figure:
    """Что закрасить на каждой стороне: основные и помогающие зоны."""

    front: frozenset[str]
    back: frozenset[str]
    front_secondary: frozenset[str] = frozenset()
    back_secondary: frozenset[str] = frozenset()

    def primary(self, side: str) -> frozenset[str]:
        return self.front if side == FRONT else self.back

    def secondary(self, side: str) -> frozenset[str]:
        return self.front_secondary if side == FRONT else self.back_secondary


Muscles = tuple[Sequence[str], Sequence[str]]


def detailed_muscles(muscles: Optional[Muscles]) -> Optional[tuple[frozenset[str], frozenset[str]]]:
    """Разметка, которую можно нарисовать: только знакомые схеме слаги,
    помогающие без основных. None — рисовать по ней нечего."""
    if not muscles:
        return None
    primary = frozenset(muscles[0]) & KNOWN_SLUGS
    secondary = (frozenset(muscles[1]) & KNOWN_SLUGS) - primary
    if not primary and not secondary:
        return None
    return primary, secondary


def figure(exercises: Iterable[tuple[Optional[Muscles], Optional[str]]]) -> Optional[Figure]:
    """Фигура по упражнениям — пары (разметка мышц или None, слаг зоны группы
    или None). Разметка красит свои мышцы (слаги без стороны: каждая сторона
    берёт только свои контуры), без неё — зона группы основным тоном."""
    front: set[str] = set()
    back: set[str] = set()
    secondary: set[str] = set()
    for muscles, region_key in exercises:
        detailed = detailed_muscles(muscles)
        if detailed is not None:
            front |= detailed[0]
            back |= detailed[0]
            secondary |= detailed[1]
        elif region_key in REGIONS:
            region = REGIONS[region_key]
            front |= region.slugs_on(FRONT)
            back |= region.slugs_on(BACK)
    if not front and not back and not secondary:
        return None
    return Figure(
        front=frozenset(front), back=frozenset(back),
        front_secondary=frozenset(secondary - front), back_secondary=frozenset(secondary - back),
    )


# --- кадр и вписывание (BodyMapCrop, BodyMapShape.transform) ------------------


def crop_rect(region_key: str, aspect: float) -> Rect:
    """Кадр с запасом вокруг мышц группы, растянутый по центру до пропорции
    рамки `aspect` (ширина / высота); парные мышцы рук — одной рукой."""
    region = REGIONS[region_key]
    polygons = side_polygons(region.side)
    picked: list[Polygon] = []
    for slug in region.slugs:
        for polygon in polygons.get(slug, ()):
            if region.crops_one_side:
                box = _bbox([polygon])
                if box and (box[0] + box[2]) / 2 >= body_map_data.VIEW_BOX_WIDTH / 2:
                    continue
            picked.append(polygon)
    box = _bbox(picked)
    if box is None or aspect <= 0:
        return FULL_FIGURE
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return FULL_FIGURE
    pad = 0.35 if region.crops_one_side else 0.18
    x0, y0, w, h = x0 - w * pad, y0 - h * pad, w * (1 + 2 * pad), h * (1 + 2 * pad)
    if w / h < aspect:
        width = h * aspect
        x0 -= (width - w) / 2
        w = width
    else:
        height = w / aspect
        y0 -= (height - h) / 2
        h = height
    return x0, y0, w, h


def fit(content: Rect, frame: Rect) -> Optional[tuple[float, float, float]]:
    """Как часть поля `content` вписывается в рамку `frame` по центру с
    сохранением пропорций: (масштаб, сдвиг x, сдвиг y); None — рамка пустая."""
    cx, cy, cw, ch = content
    fx, fy, fw, fh = frame
    scale = min(fw / cw, fh / ch)
    if scale <= 0:
        return None
    dx = fx + (fw - cw * scale) / 2 - cx * scale
    dy = fy + (fh - ch * scale) / 2 - cy * scale
    return scale, dx, dy


def layers(
    side: str, primary: frozenset[str], secondary: frozenset[str] = frozenset(),
) -> list[tuple[str, list[Polygon]]]:
    """Слои фигуры снизу вверх, как в `BodyMapTwoToneFigure`: всё тело
    («base»), помогающие («secondary») и основные («primary») поверх —
    зона в обоих списках читается основной."""
    polygons = side_polygons(side)
    painted = primary | secondary
    base = [p for slug, ps in polygons.items() if slug not in painted for p in ps]
    second = [p for slug, ps in polygons.items() if slug in secondary - primary for p in ps]
    first = [p for slug, ps in polygons.items() if slug in primary for p in ps]
    return [("base", base), ("secondary", second), ("primary", first)]


def placed(polygons: Sequence[Polygon], transform: tuple[float, float, float]) -> list[Polygon]:
    scale, dx, dy = transform
    return [[(x * scale + dx, y * scale + dy) for x, y in polygon] for polygon in polygons]


def painted_slugs(fig: Figure) -> Mapping[str, frozenset[str]]:
    """Закрашенные зоны по сторонам — только те, что есть в контурах стороны."""
    out = {}
    for side in (FRONT, BACK):
        known = frozenset(side_polygons(side))
        out[side] = (fig.primary(side) | fig.secondary(side)) & known
    return out
