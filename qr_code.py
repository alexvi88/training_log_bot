"""QR-код в SVG без сторонних библиотек — для кнопки «скачать» на десктопе
(seo_pages.py: на компьютере ссылку в App Store проще снять телефоном, чем
набрать).

Зачем свой кодировщик, а не `qrcode`/`segno`: нужна одна короткая ссылка, и
ради неё не хочется тянуть зависимость в прод. Кодировщик урезан ровно под
эту задачу: байтовый режим, уровень коррекции M (15%), версии 1–10 (до 213
байт — любая ссылка в App Store или в бота влезает с запасом). Маска
выбирается по штрафам из стандарта ISO/IEC 18004 (раздел 7.8.3), так что
код читается любой камерой.

`tests/test_qr_code.py` сверяет матрицу побитно с эталонами
(`tests/qr_fixtures.json`), снятыми офлайн библиотекой `qrcode` 8.2 на тех
же строках и той же маске. Маску реализации выбирают каждая по-своему
(у `qrcode` в штрафе N3 пропуски ради скорости), а годится любая из восьми —
поэтому эталон снят при маске, которую выбрал этот модуль, и ещё при одной.
"""

from __future__ import annotations

from functools import lru_cache

# Версия → (EC-байтов на блок, [(блоков, данных в блоке), ...]) для уровня M.
_BLOCKS_M: dict[int, tuple[int, tuple[tuple[int, int], ...]]] = {
    1: (10, ((1, 16),)),
    2: (16, ((1, 28),)),
    3: (26, ((1, 44),)),
    4: (18, ((2, 32),)),
    5: (24, ((2, 43),)),
    6: (16, ((4, 27),)),
    7: (18, ((4, 31),)),
    8: (22, ((2, 38), (2, 39))),
    9: (22, ((3, 36), (2, 37))),
    10: (26, ((4, 43), (1, 44))),
}
_ALIGN = {
    1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30), 6: (6, 34),
    7: (6, 22, 38), 8: (6, 24, 42), 9: (6, 26, 46), 10: (6, 28, 50),
}
_ECL_M_BITS = 0b00
MAX_VERSION = max(_BLOCKS_M)


# ---------- поле Галуа GF(256), многочлен 0x11D ----------

_EXP = [0] * 512
_LOG = [0] * 256
_v = 1
for _i in range(255):
    _EXP[_i] = _v
    _LOG[_v] = _i
    _v <<= 1
    if _v & 0x100:
        _v ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


@lru_cache(maxsize=None)
def _generator(degree: int) -> tuple[int, ...]:
    """Коэффициенты (x − α⁰)(x − α¹)…(x − α^(n−1)) без старшего, от старших к младшим."""
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, c in enumerate(poly):
            nxt[j] ^= c
            nxt[j + 1] ^= _gf_mul(c, _EXP[i])
        poly = nxt
    return tuple(poly[1:])


def _rs_remainder(data: list[int], degree: int) -> list[int]:
    gen = _generator(degree)
    rem = [0] * degree
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for i, g in enumerate(gen):
            rem[i] ^= _gf_mul(g, factor)
    return rem


# ---------- данные ----------


def _capacity(version: int) -> int:
    _ec, groups = _BLOCKS_M[version]
    return sum(n * size for n, size in groups)


def _pick_version(length: int) -> int:
    for version in _BLOCKS_M:
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * length <= 8 * _capacity(version):
            return version
    raise ValueError(f"payload of {length} bytes exceeds QR version {MAX_VERSION}")


def _codewords(payload: bytes, version: int) -> list[int]:
    count_bits = 8 if version < 10 else 16
    bits: list[int] = []

    def put(value: int, width: int) -> None:
        bits.extend((value >> i) & 1 for i in range(width - 1, -1, -1))

    put(0b0100, 4)
    put(len(payload), count_bits)
    for b in payload:
        put(b, 8)
    capacity_bits = 8 * _capacity(version)
    put(0, min(4, capacity_bits - len(bits)))
    put(0, (-len(bits)) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    i = 0
    while len(data) < _capacity(version):
        data.append(pad[i % 2])
        i += 1

    ec_len, groups = _BLOCKS_M[version]
    blocks: list[list[int]] = []
    pos = 0
    for n, size in groups:
        for _ in range(n):
            blocks.append(data[pos:pos + size])
            pos += size
    ecs = [_rs_remainder(b, ec_len) for b in blocks]
    out: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        out.extend(b[i] for b in blocks if i < len(b))
    for i in range(ec_len):
        out.extend(e[i] for e in ecs)
    return out


# ---------- матрица ----------


class _Grid:
    def __init__(self, version: int):
        self.version = version
        self.size = 4 * version + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def set(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.fixed[y][x] = True

    def function_patterns(self) -> None:
        n = self.size
        for i in range(n):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        dist = max(abs(dx), abs(dy))
                        self.set(x, y, dist not in (2, 4))
        pos = _ALIGN[self.version]
        last = len(pos) - 1
        for i, cx in enumerate(pos):
            for j, cy in enumerate(pos):
                if (i == 0 and j == 0) or (i == 0 and j == last) or (i == last and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits(0)  # резерв; настоящие биты — после выбора маски
        self.version_bits()

    def format_bits(self, mask: int) -> None:
        data = (_ECL_M_BITS << 3) | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = ((data << 10) | rem) ^ 0x5412
        n = self.size

        def bit(i: int) -> bool:
            return (bits >> i) & 1 == 1

        for i in range(6):
            self.set(8, i, bit(i))
        self.set(8, 7, bit(6))
        self.set(8, 8, bit(7))
        self.set(7, 8, bit(8))
        for i in range(9, 15):
            self.set(14 - i, 8, bit(i))
        for i in range(8):
            self.set(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set(8, n - 15 + i, bit(i))
        self.set(8, n - 8, True)

    def version_bits(self) -> None:
        if self.version < 7:
            return
        rem = self.version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = (self.version << 12) | rem
        for i in range(18):
            dark = (bits >> i) & 1 == 1
            a, b = self.size - 11 + i % 3, i // 3
            self.set(a, b, dark)
            self.set(b, a, dark)

    def place(self, codewords: list[int]) -> None:
        n = self.size
        i = 0
        total = len(codewords) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.fixed[y][x] and i < total:
                        self.dark[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        for y in range(self.size):
            for x in range(self.size):
                if not self.fixed[y][x] and _MASKS[mask](x, y):
                    self.dark[y][x] = not self.dark[y][x]


_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def _penalty(m: list[list[bool]]) -> int:
    """Штрафы N1–N4 из ISO/IEC 18004 7.8.3."""
    n = len(m)
    score = 0
    lines = m + [[m[y][x] for y in range(n)] for x in range(n)]
    finder_a = [True, False, True, True, True, False, True, False, False, False, False]
    finder_b = finder_a[::-1]
    for line in lines:
        run = 1
        for i in range(1, n):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run = 1
        if run >= 5:
            score += 3 + run - 5
        for i in range(n - 10):
            window = line[i:i + 11]
            if window in (finder_a, finder_b):
                score += 40
    for y in range(n - 1):
        for x in range(n - 1):
            c = m[y][x]
            if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(sum(row) for row in m)
    total = n * n
    k = abs(dark * 20 - total * 10) // total
    score += 10 * k
    return score


def matrix(text: str, mask: int | None = None) -> list[list[bool]]:
    """Матрица модулей без тихой зоны: True — тёмный. mask=None — лучшая по штрафам."""
    payload = text.encode("utf-8")
    version = _pick_version(len(payload))
    words = _codewords(payload, version)
    best: tuple[int, list[list[bool]]] | None = None
    for m in (range(8) if mask is None else (mask,)):
        grid = _Grid(version)
        grid.function_patterns()
        grid.place(words)
        grid.apply_mask(m)
        grid.format_bits(m)
        score = _penalty(grid.dark)
        if best is None or score < best[0]:
            best = (score, [row[:] for row in grid.dark])
    assert best is not None
    return best[1]


@lru_cache(maxsize=32)
def svg(text: str, *, module: int = 4, quiet: int = 4, dark: str = "#1a1210", light: str = "#ffffff") -> str:
    """SVG-картинка кода: одна заливка фона и один path из горизонтальных полос."""
    m = matrix(text)
    n = len(m)
    side = n + 2 * quiet
    parts = []
    for y, row in enumerate(m):
        x = 0
        while x < n:
            if row[x]:
                start = x
                while x < n and row[x]:
                    x += 1
                parts.append(f"M{start + quiet} {y + quiet}h{x - start}v1h-{x - start}z")
            else:
                x += 1
    px = side * module
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}" width="{px}" height="{px}" '
        f'shape-rendering="crispEdges"><rect width="{side}" height="{side}" fill="{light}"/>'
        f'<path fill="{dark}" d="{"".join(parts)}"/></svg>'
    )
