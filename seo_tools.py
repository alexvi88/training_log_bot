"""Математика публичных калькуляторов (`/<язык>/tools/*`, seo_pages.py).

Здесь — Python-версия того, что страница считает в браузере, и сам скрипт
`CALC_JS`. Обе стороны берут константы из одного места:

- расчётный максимум — та же формула, что у бота (`analytics.e1rm`: Эпли по
  умолчанию, Бжицки до `BRZYCKI_MAX_REPS`, запас повторов из RPE с потолком
  `RIR_CAP`, подходы длиннее `config.E1RM_MAX_REPS` не считаются). Скрипт
  получает эти числа подстановкой из Python, поэтому разъехаться с ботом
  калькулятор может только вместе с ботом;
- раскладка блинов — своей функцией: в боте калькулятора блинов нет. Ищет
  точный набор (динамикой по сотым долям, а не жадно — у 25/20/15/10 жадный
  выбор для 30кг на сторону даёт «25 и не хватает 5», хотя 20+10 есть), а
  если точного нет — ближайший снизу;
- Wilks — классические коэффициенты (до пересмотра 2020 года), DOTS —
  опубликованные коэффициенты IPF/OpenPowerlifting. Вес тела зажимается в
  те же границы, что у распространённых реализаций.

`tests/test_seo_pages.py` сверяет Python с ботом и гоняет `CALC_JS` в node
на тех же входах.
"""

from __future__ import annotations

import json
from typing import Iterable, Optional

import analytics
import config

# Классический Wilks: 500 / (a + b·x + c·x² + d·x³ + e·x⁴ + f·x⁵), x — вес тела, кг.
WILKS_MEN = (-216.0475144, 16.2606339, -0.002388645, -0.00113732, 7.01863e-06, -1.291e-08)
WILKS_WOMEN = (594.31747775582, -27.23842536447, 0.82112226871, -0.00930733913, 4.731582e-05, -9.054e-08)
WILKS_BW_LIMITS = {"m": (40.0, 201.9), "f": (26.51, 154.53)}

# DOTS: 500 / (a + b·x + c·x² + d·x³ + e·x⁴).
DOTS_MEN = (-307.75076, 24.0900756, -0.1918759221, 0.0007391293, -0.000001093)
DOTS_WOMEN = (-57.96288, 13.6175032, -0.1126655495, 0.0005158568, -0.0000010706)
DOTS_BW_LIMITS = {"m": (40.0, 210.0), "f": (40.0, 150.0)}

PERCENTS = (100, 95, 90, 85, 80, 75, 70, 65, 60, 55, 50)

PLATES = {
    "kg": (25.0, 20.0, 15.0, 10.0, 5.0, 2.5, 1.25, 0.5, 0.25),
    "lb": (55.0, 45.0, 35.0, 25.0, 10.0, 5.0, 2.5, 1.25),
}
DEFAULT_PLATES = {
    "kg": (25.0, 20.0, 15.0, 10.0, 5.0, 2.5, 1.25),
    "lb": (45.0, 35.0, 25.0, 10.0, 5.0, 2.5),
}
BARS = {"kg": (20.0, 15.0, 10.0, 7.0), "lb": (45.0, 35.0, 25.0, 15.0)}
# Сотые доли — самый мелкий блин 0.25кг ложится целым числом.
_SCALE = 100
# Потолок на сторону для перебора: дальше живых весов не бывает.
MAX_PER_SIDE = 400.0


def e1rm(weight: float, reps: int, formula: str = "epley", rpe: Optional[float] = None) -> float:
    return analytics.e1rm(weight, reps, formula, rpe)


def percent_table(one_rm: float) -> list[tuple[int, float]]:
    return [(p, one_rm * p / 100) for p in PERCENTS]


def _poly(coefs: Iterable[float], x: float) -> float:
    return sum(c * x**i for i, c in enumerate(coefs))


def _clamp(value: float, limits: tuple[float, float]) -> float:
    return min(max(value, limits[0]), limits[1])


def wilks(total_kg: float, bodyweight_kg: float, sex: str) -> float:
    x = _clamp(bodyweight_kg, WILKS_BW_LIMITS[sex])
    return total_kg * 500 / _poly(WILKS_MEN if sex == "m" else WILKS_WOMEN, x)


def dots(total_kg: float, bodyweight_kg: float, sex: str) -> float:
    x = _clamp(bodyweight_kg, DOTS_BW_LIMITS[sex])
    return total_kg * 500 / _poly(DOTS_MEN if sex == "m" else DOTS_WOMEN, x)


def plates_per_side(target: float, bar: float, plates: Iterable[float]) -> tuple[list[float], float]:
    """(блины на одну сторону от тяжёлого к лёгкому, вес, который получился).

    Точного набора нет — ближайший снизу; при равном весе — меньше блинов.
    Цель меньше грифа — пустой список и вес грифа."""
    side = min((target - bar) / 2, MAX_PER_SIDE)
    if side <= 0:
        return [], bar
    units = sorted({round(p * _SCALE) for p in plates if p > 0}, reverse=True)
    goal = int(round(side * _SCALE))
    # best[v] — минимальное число блинов, дающее ровно v; choice[v] — последний блин.
    inf = 1 << 30
    best = [inf] * (goal + 1)
    choice = [0] * (goal + 1)
    best[0] = 0
    for v in range(1, goal + 1):
        for u in units:
            if u <= v and best[v - u] + 1 < best[v]:
                best[v] = best[v - u] + 1
                choice[v] = u
    reach = next(v for v in range(goal, -1, -1) if best[v] < inf)
    picked: list[float] = []
    v = reach
    while v > 0:
        picked.append(choice[v] / _SCALE)
        v -= choice[v]
    picked.sort(reverse=True)
    return picked, bar + 2 * reach / _SCALE


def js_constants() -> str:
    return json.dumps({
        "rirCap": analytics.RIR_CAP,
        "brzyckiMaxReps": analytics.BRZYCKI_MAX_REPS,
        "e1rmMaxReps": config.E1RM_MAX_REPS,
        "percents": list(PERCENTS),
        "wilks": {"m": list(WILKS_MEN), "f": list(WILKS_WOMEN)},
        "wilksBw": {k: list(v) for k, v in WILKS_BW_LIMITS.items()},
        "dots": {"m": list(DOTS_MEN), "f": list(DOTS_WOMEN)},
        "dotsBw": {k: list(v) for k, v in DOTS_BW_LIMITS.items()},
        "scale": _SCALE,
        "maxPerSide": MAX_PER_SIDE,
        "lbPerKg": config.LB_PER_KG,
    })


# Чистые функции без DOM — их же тест гоняет в node. Комментариев внутри нет:
# скрипт уходит на страницу как есть.
CALC_JS = """
var C = __CONSTANTS__;
function rir(rpe) { if (rpe == null || isNaN(rpe)) return 0; return Math.min(Math.max(10 - rpe, 0), C.rirCap); }
function epley(w, r, rpe) { var e = r + rir(rpe); return e <= 1 ? w : w * (1 + e / 30); }
function brzycki(w, r, rpe) { var e = r + rir(rpe); if (e <= 1) return w; if (e > C.brzyckiMaxReps) return epley(w, r, rpe); return w * 36 / (37 - e); }
function e1rm(w, r, formula, rpe) { if (r > C.e1rmMaxReps) return 0; return formula === 'brzycki' ? brzycki(w, r, rpe) : epley(w, r, rpe); }
function poly(c, x) { var s = 0; for (var i = 0; i < c.length; i++) s += c[i] * Math.pow(x, i); return s; }
function clamp(v, l) { return Math.min(Math.max(v, l[0]), l[1]); }
function wilks(total, bw, sex) { return total * 500 / poly(C.wilks[sex], clamp(bw, C.wilksBw[sex])); }
function dots(total, bw, sex) { return total * 500 / poly(C.dots[sex], clamp(bw, C.dotsBw[sex])); }
function platesPerSide(target, bar, plates) {
  var side = Math.min((target - bar) / 2, C.maxPerSide);
  if (!(side > 0)) return { plates: [], total: bar };
  var units = [], seen = {};
  plates.forEach(function (p) { var u = Math.round(p * C.scale); if (u > 0 && !seen[u]) { seen[u] = 1; units.push(u); } });
  units.sort(function (a, b) { return b - a; });
  var goal = Math.round(side * C.scale), INF = 1 << 30, best = new Array(goal + 1), choice = new Array(goal + 1);
  best[0] = 0;
  for (var v = 1; v <= goal; v++) {
    best[v] = INF; choice[v] = 0;
    for (var i = 0; i < units.length; i++) {
      var u = units[i];
      if (u <= v && best[v - u] + 1 < best[v]) { best[v] = best[v - u] + 1; choice[v] = u; }
    }
  }
  var reach = goal; while (reach > 0 && best[reach] >= INF) reach--;
  var picked = []; v = reach;
  while (v > 0) { picked.push(choice[v] / C.scale); v -= choice[v]; }
  picked.sort(function (a, b) { return b - a; });
  return { plates: picked, total: bar + 2 * reach / C.scale };
}
function fmt(x) { var r = Math.round(x * 10) / 10; return (r % 1 === 0 ? r.toFixed(0) : r.toFixed(1)); }
"""


def calc_js() -> str:
    return CALC_JS.replace("__CONSTANTS__", js_constants())
