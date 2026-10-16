"""Математика публичных калькуляторов (`/<язык>/tools/*`, seo_pages.py).

Здесь — Python-версия того, что страница считает в браузере, и сам скрипт
`CALC_JS`. Обе стороны берут константы из одного места:

- расчётный максимум — та же формула, что у бота (`analytics.e1rm`: Эпли по
  умолчанию, Бжицки до `BRZYCKI_MAX_REPS`, запас повторов из RPE с потолком
  `RIR_CAP`, подходы длиннее `config.E1RM_MAX_REPS` не считаются). Скрипт
  получает эти числа подстановкой из Python, поэтому разъехаться с ботом
  калькулятор может только вместе с ботом;
- раскладка блинов — своей функцией (её показывает разминка: что вешать на
  сторону у каждого подхода; отдельного калькулятора блинов больше нет). Ищет
  точный набор (динамикой по сотым долям, а не жадно — у 25/20/15/10 жадный
  выбор для 30кг на сторону даёт «25 и не хватает 5», хотя 20+10 есть), а
  если точного нет — ближайший снизу;
- Wilks — классические коэффициенты (до пересмотра 2020 года), DOTS —
  опубликованные коэффициенты IPF/OpenPowerlifting. Вес тела зажимается в
  те же границы, что у распространённых реализаций;
- IPF GL (Goodlift) — 100 ÷ (A − B·e^(−C·вес)), коэффициенты — таблица IPF
  2020 в той же записи, что у OpenPowerlifting (crates/coefficients/src/
  goodlift.rs), там же опубликованные примеры IPF, которыми их сверяет тест;
- разминка — своя лесенка процентов от рабочего веса, округлённая вниз до
  шага, который собирается блинами;
- таблица RPE — не чужая таблица, а та же формула e1RM, развёрнутая
  обратно: % от максимума = 1 ÷ e1RM(1кг, повторы, RPE);
- нормативы силы — своя шкала по DOTS (пороги `LEVELS`), разложенная на
  одно упражнение долей в сумме (`LIFT_SHARE`): медианы по выступлениям в
  классическом троеборье (данные OpenPowerlifting, общественное достояние).

`tests/test_seo_pages.py` сверяет Python с ботом и гоняет `CALC_JS` в node
на тех же входах.
"""

from __future__ import annotations

import json
import math
import os
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

# IPF GL: (A, B, C) по (соревнование, экипировка, пол). sbd — троеборье, b — жим.
GL_PARAMS = {
    ("sbd", "classic", "m"): (1199.72839, 1025.18162, 0.009210),
    ("sbd", "equipped", "m"): (1236.25115, 1449.21864, 0.01644),
    ("sbd", "classic", "f"): (610.32796, 1045.59282, 0.03048),
    ("sbd", "equipped", "f"): (758.63878, 949.31382, 0.02435),
    ("b", "classic", "m"): (320.98041, 281.40258, 0.01008),
    ("b", "equipped", "m"): (381.22073, 733.79378, 0.02398),
    ("b", "classic", "f"): (142.40398, 442.52671, 0.04724),
    ("b", "equipped", "f"): (221.82209, 357.00377, 0.02937),
}
GL_MIN_BW = 35.0

# Разминка: (доля рабочего веса, повторы). Последняя ступень — только перед
# тяжёлым подходом (до WARMUP_HEAVY_REPS повторов): перед десяткой она лишняя.
WARMUP_STEPS = ((0.4, 5), (0.6, 3), (0.8, 2), (0.9, 1))
WARMUP_HEAVY_REPS = 5
WARMUP_BAR_REPS = 10
WARMUP_STEP = {"kg": 2.5, "lb": 5.0}

# Таблица RPE: строки — RPE, столбцы — повторы.
RPE_ROWS = (10.0, 9.5, 9.0, 8.5, 8.0, 7.5, 7.0, 6.5, 6.0)
RPE_REPS = tuple(range(1, 13))

# Нормативы: пороги DOTS-эквивалента суммы, общие для мужчин и женщин (DOTS
# уже выравнивает пол и вес). 450 — около 95-го перцентиля мужчин в
# классике на соревнованиях USAPL, 300 — около 15-го: уровень первого старта.
LEVELS = (("beginner", 150.0), ("novice", 225.0), ("intermediate", 300.0), ("advanced", 375.0), ("elite", 450.0))
# Доля упражнения в сумме — медианы по 11 672 мужским и 5 824 женским
# выступлениям в классическом троеборье (400 последних по номеру папки
# соревнований USAPL в opl-data, октябрь 2026), округлённые так, чтобы сумма
# дала 100%.
LIFT_SHARE = {
    "m": {"squat": 0.355, "bench": 0.235, "deadlift": 0.41},
    "f": {"squat": 0.36, "bench": 0.20, "deadlift": 0.44},
}
STRENGTH_LIFTS = ("bench", "squat", "deadlift")
STRENGTH_BW = {"m": tuple(range(50, 145, 5)), "f": tuple(range(40, 115, 5))}
STRENGTH_BW_LB = {"m": tuple(range(110, 320, 10)), "f": tuple(range(90, 250, 10))}

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


def ipf_gl(total_kg: float, bodyweight_kg: float, sex: str, event: str = "sbd", equipment: str = "classic") -> float:
    """Очки IPF GL. 0 — вес тела меньше GL_MIN_BW: там формула не определена."""
    if bodyweight_kg < GL_MIN_BW or total_kg <= 0:
        return 0.0
    a, b, c = GL_PARAMS[(event, equipment, sex)]
    denominator = a - b * math.exp(-c * bodyweight_kg)
    return total_kg * max(0.0, 100 / denominator) if denominator else 0.0


def _floor_step(value: float, step: float) -> float:
    return math.floor(value / step + 1e-9) * step


def warmup_sets(work: float, bar: float, step: float, work_reps: int = 5) -> list[tuple[float, int]]:
    """Разминочные подходы до рабочего веса: (вес, повторы), от грифа вверх.

    Ступень, которая после округления не тяжелее предыдущей или уже не легче
    рабочего веса, выпадает — у лёгкого рабочего веса лесенка короче."""
    if work <= bar or step <= 0:
        return []
    sets = [(bar, WARMUP_BAR_REPS)]
    last = bar
    steps = WARMUP_STEPS if work_reps <= WARMUP_HEAVY_REPS else WARMUP_STEPS[:-1]
    for share, reps in steps:
        weight = _floor_step(work * share, step)
        if weight <= last + 1e-9 or weight >= work - 1e-9:
            continue
        sets.append((weight, reps))
        last = weight
    return sets


def rpe_percent(reps: int, rpe: float, formula: str = "epley") -> float:
    """Сколько процентов от максимума — подход на `reps` при этом RPE."""
    one = analytics.e1rm(1.0, reps, formula, rpe)
    return 100 / one if one else 0.0


def rpe_chart(formula: str = "epley") -> list[tuple[float, list[float]]]:
    return [(rpe, [rpe_percent(r, rpe, formula) for r in RPE_REPS]) for rpe in RPE_ROWS]


def dots_coefficient(bodyweight_kg: float, sex: str) -> float:
    x = _clamp(bodyweight_kg, DOTS_BW_LIMITS[sex])
    return 500 / _poly(DOTS_MEN if sex == "m" else DOTS_WOMEN, x)


def lift_share(sex: str, lift: str) -> float:
    """Доля упражнения в сумме; сама сумма троеборья (`total`) — вся, 1."""
    return 1.0 if lift == "total" else LIFT_SHARE[sex][lift]


def level_weight(threshold: float, lift: str, bodyweight_kg: float, sex: str) -> float:
    """Вес в упражнении (или сумма — `total`), с которого начинается уровень с этим порогом DOTS."""
    return threshold / dots_coefficient(bodyweight_kg, sex) * lift_share(sex, lift)


def lift_score(lift_kg: float, lift: str, bodyweight_kg: float, sex: str) -> float:
    """DOTS-эквивалент одного упражнения: как если бы сумма была в той же пропорции."""
    return lift_kg / lift_share(sex, lift) * dots_coefficient(bodyweight_kg, sex)


def level_index(score: float) -> int:
    """Номер уровня в LEVELS (−1 — ниже первого порога)."""
    index = -1
    for i, (_name, threshold) in enumerate(LEVELS):
        if score >= threshold - 1e-9:
            index = i
    return index


def round_to(value: float, step: float) -> float:
    return math.floor(value / step + 0.5) * step


def strength_table(lift: str, sex: str, unit: str = "kg") -> list[tuple[float, list[float]]]:
    """(вес тела, [вес упражнения на каждый уровень]) в выбранных единицах:
    кг округляются до 2.5, фунты — до 5."""
    rows = []
    if unit == "kg":
        for bw in STRENGTH_BW[sex]:
            rows.append((float(bw), [round_to(level_weight(t, lift, bw, sex), 2.5) for _n, t in LEVELS]))
    else:
        for bw_lb in STRENGTH_BW_LB[sex]:
            bw = bw_lb / config.LB_PER_KG
            rows.append((float(bw_lb), [round_to(level_weight(t, lift, bw, sex) * config.LB_PER_KG, 5.0) for _n, t in LEVELS]))
    return rows


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


# ---------- «когда я пожму 100?» ----------
#
# Прогноз, а не обещание. Темп роста расчётного максимума за неделю — доля
# от текущего e1RM по уровню (новичок до полугода, средний — до двух лет,
# дальше — опытный), двумя концами: быстрый и медленный. Темп тает как
# 1 / (1 + t/GOAL_DECAY_WEEKS): через год — вдвое ниже исходного, через два — втрое,
# потому что каждый следующий килограмм даётся дороже предыдущего. Частота —
# множитель: одна тренировка с этим движением в неделю растит медленнее, три
# — чуть быстрее двух (у силы частота заметна, Pelland et al.). Сверху
# любой темп ограничен тем, что бот вообще предлагает: двойная прогрессия
# (analytics.suggest_progression) прибавляет не больше повтора за
# тренировку, а повтор — это около 1/30 веса (Эпли); до этого потолка
# прогноз не доходит, но ограничение в коде есть.
GOAL_RATES = {"beginner": (0.015, 0.008), "intermediate": (0.006, 0.003), "advanced": (0.0025, 0.001)}
GOAL_FREQ = {1: 0.8, 2: 1.0, 3: 1.1, 4: 1.15}
GOAL_DECAY_WEEKS = 52
GOAL_MAX_WEEKS = 156
GOAL_STEP_WEEKS = 4


def goal_rate(level: str, freq: int, fast: bool, week: int) -> float:
    r0 = GOAL_RATES[level][0 if fast else 1] * GOAL_FREQ[min(max(freq, 1), 4)]
    return r0 / (1 + week / GOAL_DECAY_WEEKS)


def goal_curve(current: float, level: str, freq: int, fast: bool, weeks: int = GOAL_MAX_WEEKS) -> list[float]:
    """e1RM по неделям: [неделя 0, 1, …, weeks]."""
    out = [current]
    value = current
    cap_per_week = max(freq, 1) / 30
    for week in range(weeks):
        value *= 1 + min(goal_rate(level, freq, fast, week), cap_per_week)
        out.append(value)
    return out


def goal_weeks(current: float, target: float, level: str, freq: int, fast: bool) -> Optional[int]:
    """Через сколько недель кривая дойдёт до цели; None — дольше GOAL_MAX_WEEKS."""
    if current <= 0 or target <= 0:
        return None
    if current >= target:
        return 0
    for week, value in enumerate(goal_curve(current, level, freq, fast)):
        if value >= target - 1e-9:
            return week
    return None


# ---------- 5/3/1 ----------
#
# Классическая схема Джима Вендлера: тренировочный максимум (ТМ) — 90%
# разового максимума; неделя 1 — 65/75/85% ТМ на 5/5/5+, неделя 2 —
# 70/80/90% на 3/3/3+, неделя 3 — 75/85/95% на 5/3/1+, неделя 4 — разгрузка
# 40/50/60% на 5. Разминка — 40/50/60% ТМ на 5/5/3. Веса — вниз до шага,
# который собирается блинами (как у разминки). После цикла ТМ растёт на
# 2.5кг в жимах и 5кг в приседе и тяге (5 и 10 фунтов).
FIVE31_TM = 0.9
FIVE31_WEEKS = (
    ((0.65, "5"), (0.75, "5"), (0.85, "5+")),
    ((0.70, "3"), (0.80, "3"), (0.90, "3+")),
    ((0.75, "5"), (0.85, "3"), (0.95, "1+")),
    ((0.40, "5"), (0.50, "5"), (0.60, "5")),
)
FIVE31_WARMUP = ((0.40, "5"), (0.50, "5"), (0.60, "3"))
FIVE31_STEP_UP = {"kg": {"upper": 2.5, "lower": 5.0}, "lb": {"upper": 5.0, "lower": 10.0}}


def five31(one_rm: float, step: float, tm_share: float = FIVE31_TM) -> dict:
    """{'tm': ТМ, 'warmup': [(вес, повторы)], 'weeks': [[(доля, вес, повторы)]]}."""
    tm = one_rm * tm_share
    return {
        "tm": tm,
        "warmup": [(_floor_step(tm * p, step), reps) for p, reps in FIVE31_WARMUP],
        "weeks": [[(p, _floor_step(tm * p, step), reps) for p, reps in week] for week in FIVE31_WEEKS],
    }


# ---------- перцентиль на помосте ----------

_PCT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seo_data", "strength_percentiles.json")
_pct_data: Optional[dict] = None


def percentile_data() -> dict:
    """Таблица из seo_data/strength_percentiles.json (scripts/build_strength_percentiles.py)."""
    global _pct_data
    if _pct_data is None:
        with open(_PCT_FILE, encoding="utf-8") as fh:
            _pct_data = json.load(fh)
    return _pct_data


def weight_class(sex: str, bodyweight_kg: float) -> str:
    classes = percentile_data()["classes"][sex]
    for cls in classes[:-1]:
        if bodyweight_kg <= float(cls):
            return cls
    return classes[-1]


def percentile(value_kg: float, sex: str, bodyweight_kg: float, lift: str) -> Optional[tuple[float, str, int]]:
    """(перцентиль 0–100, весовая категория, атлетов в клетке).

    0 — ниже 1-го перцентиля, 100 — выше 99-го; между точками таблицы —
    линейно. None — клетки нет."""
    data = percentile_data()
    cls = weight_class(sex, bodyweight_kg)
    cell = data["table"].get(sex, {}).get(cls, {}).get(lift)
    if not cell or value_kg <= 0:
        return None
    qs, ps = cell["q"], data["quantiles"]
    if value_kg < qs[0]:
        return 0.0, cls, cell["n"]
    if value_kg >= qs[-1]:
        return 100.0, cls, cell["n"]
    for i in range(len(qs) - 1):
        lo, hi = qs[i], qs[i + 1]
        if lo <= value_kg < hi:
            share = (value_kg - lo) / (hi - lo) if hi > lo else 0.0
            return ps[i] + (ps[i + 1] - ps[i]) * share, cls, cell["n"]
    return 100.0, cls, cell["n"]


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
        "gl": {"|".join(k): list(v) for k, v in GL_PARAMS.items()},
        "glMinBw": GL_MIN_BW,
        "warmupSteps": [list(x) for x in WARMUP_STEPS],
        "warmupHeavyReps": WARMUP_HEAVY_REPS,
        "warmupBarReps": WARMUP_BAR_REPS,
        "rpeRows": list(RPE_ROWS),
        "rpeReps": list(RPE_REPS),
        "levels": [t for _n, t in LEVELS],
        "share": {sex: {**shares, "total": 1.0} for sex, shares in LIFT_SHARE.items()},
        "goalRates": GOAL_RATES,
        "goalFreq": {str(k): v for k, v in GOAL_FREQ.items()},
        "goalDecay": GOAL_DECAY_WEEKS,
        "goalMax": GOAL_MAX_WEEKS,
        "five31Weeks": [[list(x) for x in w] for w in FIVE31_WEEKS],
        "five31Warmup": [list(x) for x in FIVE31_WARMUP],
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
function ipfGl(total, bw, sex, ev, eq) {
  if (bw < C.glMinBw || !(total > 0)) return 0;
  var p = C.gl[ev + '|' + eq + '|' + sex], d = p[0] - p[1] * Math.exp(-p[2] * bw);
  return d ? total * Math.max(0, 100 / d) : 0;
}
function floorStep(v, s) { return Math.floor(v / s + 1e-9) * s; }
function warmupSets(work, bar, step, reps) {
  if (!(work > bar) || !(step > 0)) return [];
  var out = [[bar, C.warmupBarReps]], last = bar;
  var steps = reps <= C.warmupHeavyReps ? C.warmupSteps : C.warmupSteps.slice(0, -1);
  for (var i = 0; i < steps.length; i++) {
    var w = floorStep(work * steps[i][0], step);
    if (w <= last + 1e-9 || w >= work - 1e-9) continue;
    out.push([w, steps[i][1]]); last = w;
  }
  return out;
}
function rpePercent(reps, rpe, formula) { var one = e1rm(1, reps, formula, rpe); return one ? 100 / one : 0; }
function dotsCoef(bw, sex) { return 500 / poly(C.dots[sex], clamp(bw, C.dotsBw[sex])); }
function levelWeight(t, lift, bw, sex) { return t / dotsCoef(bw, sex) * C.share[sex][lift]; }
function liftScore(w, lift, bw, sex) { return w / C.share[sex][lift] * dotsCoef(bw, sex); }
function levelIndex(score) { var k = -1; for (var i = 0; i < C.levels.length; i++) if (score >= C.levels[i] - 1e-9) k = i; return k; }
function goalRate(level, freq, fast, week) { var r0 = C.goalRates[level][fast ? 0 : 1] * C.goalFreq[String(Math.min(Math.max(freq, 1), 4))]; return r0 / (1 + week / C.goalDecay); }
function goalCurve(cur, level, freq, fast, weeks) {
  var out = [cur], v = cur, cap = Math.max(freq, 1) / 30;
  for (var w = 0; w < weeks; w++) { v *= 1 + Math.min(goalRate(level, freq, fast, w), cap); out.push(v); }
  return out;
}
function goalWeeks(cur, target, level, freq, fast) {
  if (!(cur > 0) || !(target > 0)) return null;
  if (cur >= target) return 0;
  var c = goalCurve(cur, level, freq, fast, C.goalMax);
  for (var w = 0; w < c.length; w++) if (c[w] >= target - 1e-9) return w;
  return null;
}
function weightClass(P, sex, bw) { var cl = P.classes[sex]; for (var i = 0; i < cl.length - 1; i++) if (bw <= parseFloat(cl[i])) return cl[i]; return cl[cl.length - 1]; }
function percentile(P, v, sex, bw, lift) {
  var cls = weightClass(P, sex, bw), cell = ((P.table[sex] || {})[cls] || {})[lift];
  if (!cell || !(v > 0)) return null;
  var q = cell.q, p = P.quantiles;
  if (v < q[0]) return [0, cls, cell.n];
  if (v >= q[q.length - 1]) return [100, cls, cell.n];
  for (var i = 0; i < q.length - 1; i++) if (q[i] <= v && v < q[i + 1]) { var s = q[i + 1] > q[i] ? (v - q[i]) / (q[i + 1] - q[i]) : 0; return [p[i] + (p[i + 1] - p[i]) * s, cls, cell.n]; }
  return [100, cls, cell.n];
}
function five31(orm, step, share) {
  var tm = orm * share, f = function (p) { return floorStep(tm * p, step); };
  return { tm: tm, warmup: C.five31Warmup.map(function (x) { return [f(x[0]), x[1]]; }),
    weeks: C.five31Weeks.map(function (w) { return w.map(function (x) { return [x[0], f(x[0]), x[1]]; }); }) };
}
function fmt(x) { var r = Math.round(x * 10) / 10; return (r % 1 === 0 ? r.toFixed(0) : r.toFixed(1)); }
"""


def calc_js() -> str:
    return CALC_JS.replace("__CONSTANTS__", js_constants())
