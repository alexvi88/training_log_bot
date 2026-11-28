"""«Место в зале» — где лучший подход атлета стоит среди других атлетов
дневника: «топ-35%» в жиме лёжа, гистограмма зала и следующий шаг.

**Что сравнивается.** Одно число на атлета и упражнение — лучшее за
WINDOW_DAYS дней той же меркой, что «Оценка» (`progress_score.metric_of`):
e1RM по Эпли у упражнений с весом (подходы до config.E1RM_MAX_REPS повторов),
у «… с весом» (`seed_data.is_weighted_bodyweight`) — e1RM довеска, у своего
веса — лучшие повторы в подходе. Упражнения на время не сравниваются. Внутри
всё в кг; показ — в единицах атлета.

**Корзина — идентичность каталога** (`exercises.original_name`, русское имя
шаблона), а не показанное имя: атлеты на ru, en, es… сравниваются в одной
корзине. Своё упражнение, объединённое с каталожным, считается (подходы
переехали на копию каталога); своё без идентичности каталога — нет.

**Когорта** — пол атлета (`users.gender`), если он выбран и в корзине его пола
не меньше config.STANDING_MIN_ATHLETES атлетов, иначе все. База `abs` — само число, `rel` — к
весу тела (только атлеты с весом тела в дневнике; у повторов `rel` нет — тогда
отдаётся `abs`).

**Приватность — жёстко.**

- Любая показанная цифра стоит минимум на config.STANDING_MIN_ATHLETES (20, как у цифр сайта) атлетах; «топ-1%» —
  только при TOP1_MIN_USERS, иначе потолок «топ-5%». Проценты — с шагом 5
  и вверх (ceil): «топ-35%» значит «не ниже 35%», не приукрашивая.
- Ниже середины места нет вовсе (решение владельца: «че людей
  расстраивать»): упражнение недоступно с причиной `below_median`, в
  списке `/v1/standing` его нет, карточка упражнения блок не рисует.
- Сколько атлетов в корзине, наружу не уходит ни числом, ни ступенью
  (решение владельца: аудитория пока маленькая) — ни в тексте, ни в JSON.
  Порог K тоже не называется.
- Свои аккаунты и демо мимо (`db.own_account_ids`). В таблице
  `crowd_standing_buckets` — квантили и гистограммы, ни одного id.

**Пересчёт раз в сутки**, лениво: первый запрос дня берёт вчерашнее и
пересчитывает в фоне под одним замком; пусто совсем — считает сразу. Своё
число атлета считается вживую по его подходам — рекорд виден сразу.
"""

from __future__ import annotations

import asyncio
import bisect
import datetime as dt
import functools
import json
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Optional

import analytics
import config
import formatting
import i18n
import progress_score
import seed_data

logger = logging.getLogger(__name__)

# Порог корзины — config.STANDING_MIN_ATHLETES (читается при каждом обращении:
# тест и переменная окружения меняют его без перезагрузки модуля).
def _k() -> int:
    return int(config.STANDING_MIN_ATHLETES)


TOP1_MIN_USERS = 200
WINDOW_DAYS = 90
BINS = 20
CACHE_SECONDS = 24 * 3600
RETRY_SECONDS = 600
MAX_E1RM_KG = 500.0
MAX_REPS = 100
BODYWEIGHT_KG = (30.0, 250.0)
COHORTS = ("all", "male", "female")
BASES = ("abs", "rel")
TOP_STEP = 5

REASON_FEW = "few_athletes"
REASON_NO_DATA = "no_data"
REASON_NOT_CATALOG = "not_catalog"
REASON_BELOW_MEDIAN = "below_median"

_cache: dict[str, Any] = {"at": 0.0, "data": {}, "day": ""}
_lock = asyncio.Lock()
_background: Optional[asyncio.Task] = None


# ---------- идентичность и мерка ----------


@functools.lru_cache(maxsize=1)
def _catalog() -> dict[str, str]:
    # Идентичности — русские имена шаблонов, от языка не зависят: кэш безопасен.
    return {seed_data._fold_identity(name): name for _g, name in seed_data.EXERCISE_TEMPLATES}


def catalog_identity(original_name: Optional[str]) -> Optional[str]:
    """Каноничная идентичность каталога или None — своё упражнение."""
    if not original_name:
        return None
    return _catalog().get(seed_data._fold_identity(original_name))


def metric_for(identity: str) -> Optional[str]:
    """e1rm / reps / None (на время) — по шаблону, одинаково у всех атлетов."""
    load = seed_data.BODYWEIGHT_TEMPLATES.get(identity, ("none", 0.0))[0]
    return progress_score.metric_of(identity, load)


def epley(weight: float, reps: int) -> float:
    """Та же мерка, что db._standing_e1rm_sql: Эпли без RPE."""
    if reps > config.E1RM_MAX_REPS or weight <= 0:
        return 0.0
    return weight if reps <= 1 else weight * (1 + reps / 30.0)


def _db_value(metric: str, identity: str, load_e1rm, added_e1rm, reps) -> Optional[float]:
    if metric == progress_score.METRIC_REPS:
        return float(reps) if reps and 0 < reps <= MAX_REPS else None
    value = added_e1rm if seed_data.is_weighted_bodyweight(identity) else load_e1rm
    return float(value) if value and 0 < value <= MAX_E1RM_KG else None


# ---------- корзины ----------


def _quantile(sorted_values: list[float], share: float) -> float:
    pos = (len(sorted_values) - 1) * share
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def build_bucket(values: list[float]) -> dict[str, Any]:
    """Квантили 0..100 и гистограмма на BINS столбиков; меньше K — только n
    (для внутреннего «мало данных», наружу n не уходит)."""
    n = len(values)
    if n < _k():
        return {"n": n}
    ordered = sorted(values)
    q = [round(_quantile(ordered, p / 100), 4) for p in range(101)]
    lo, hi = q[2], q[98]
    if hi <= lo:
        hi = lo + 1e-6
    counts = [0] * BINS
    for v in ordered:
        counts[_bin(v, lo, hi)] += 1
    top = max(counts)
    return {"n": n, "q": q, "h": [round(c / top, 3) for c in counts], "lo": lo, "hi": hi}


def _bin(value: float, lo: float, hi: float) -> int:
    return max(0, min(BINS - 1, int((value - lo) / (hi - lo) * BINS)))


def aggregate(rows: list[tuple]) -> dict[str, dict[str, Any]]:
    """Сырьё `db.crowd_standing_bests` → корзины по идентичностям."""
    per: dict[str, dict[int, tuple[float, Optional[str], Optional[float]]]] = defaultdict(dict)
    for user_id, name, load_e1rm, added_e1rm, reps, gender, bw_kg in rows:
        identity = catalog_identity(name)
        if identity is None:
            continue
        metric = metric_for(identity)
        if metric is None:
            continue
        value = _db_value(metric, identity, load_e1rm, added_e1rm, reps)
        if value is None:
            continue
        prev = per[identity].get(user_id)
        if prev is None or value > prev[0]:
            per[identity][user_id] = (value, gender, bw_kg)
    out: dict[str, dict[str, Any]] = {}
    for identity, users in per.items():
        metric = metric_for(identity)
        cohorts: dict[str, dict[str, Any]] = {}
        for cohort in COHORTS:
            members = [(v, bw) for v, g, bw in users.values() if cohort == "all" or g == cohort]
            entry = {"abs": build_bucket([v for v, _bw in members])}
            if metric == progress_score.METRIC_E1RM:
                entry["rel"] = build_bucket([
                    v / bw for v, bw in members if bw and BODYWEIGHT_KG[0] <= bw <= BODYWEIGHT_KG[1]
                ])
            cohorts[cohort] = entry
        out[identity] = {"metric": metric, "cohorts": cohorts}
    return out


# ---------- кэш ----------


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


async def _recount() -> None:
    import db

    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    data = aggregate(await db.crowd_standing_bests(since, await db.own_account_ids()))
    today = _today()
    await db.save_crowd_standing_state(
        {identity: json.dumps(payload, sort_keys=True) for identity, payload in data.items()}, today,
    )
    _cache.update(data=data, day=today, at=time.monotonic())


async def _load_saved() -> None:
    """Корзины из базы — после рестарта процесса не считать заново то, что
    уже посчитано сегодня."""
    import db

    async with _lock:
        if _cache["day"]:
            return
        state = await db.crowd_standing_state()
        if state:
            day = next(iter(state.values()))[1]
            _cache.update(data={k: json.loads(p) for k, (p, _d) in state.items()}, day=day)
            if day == _today():
                _cache["at"] = time.monotonic()


async def _recount_locked(force: bool = False) -> None:
    async with _lock:
        if not force and _fresh():
            return
        try:
            await _recount()
        except Exception:  # noqa: BLE001 — без корзин экран честно скажет «мало данных»
            logger.warning("crowd standing recount failed", exc_info=True)
            _cache["at"] = time.monotonic() - CACHE_SECONDS + RETRY_SECONDS


def _fresh() -> bool:
    return bool(_cache["at"]) and time.monotonic() - float(_cache["at"]) < CACHE_SECONDS


async def refresh(force: bool = False) -> dict[str, dict[str, Any]]:
    """Корзины на сегодня. Есть вчерашние — отдаёт их и пересчитывает в фоне;
    нет никаких — считает сразу."""
    global _background
    if force:
        await _recount_locked(force=True)
        return _cache["data"]
    if not _cache["day"]:
        await _load_saved()
    if not _fresh():
        if _cache["data"]:
            if _background is None or _background.done():
                _background = asyncio.create_task(_recount_locked())
        else:
            await _recount_locked()
    return _cache["data"]


def cached() -> dict[str, dict[str, Any]]:
    return _cache["data"]


def reset() -> None:
    global _background
    _cache.update(at=0.0, data={}, day="")
    _background = None


# ---------- место атлета ----------


@dataclass(frozen=True)
class Placement:
    cohort: str
    n: int
    above_median: bool
    top: Optional[int]
    #: Сколько добрать в мерке корзины до середины (ниже неё) или до
    #: следующей ступени `next_top` (выше); None — нечего добирать.
    to_median: Optional[float]
    next_top: Optional[int]
    to_next: Optional[float]
    you_bin: int
    bucket: dict[str, Any]
    #: Не слабее самого сильного в корзине — «№1 в дневнике» вместо «топ-5%»
    #: (владелец: «топ-5%» лучшему читалось как «впереди ещё 5%»).
    best: bool = False


def percentile_of(q: list[float], value: float) -> float:
    """Доля атлетов (в процентах) с результатом ниже `value` по квантилям;
    на плато одинаковых значений — середина плато."""
    if value < q[0]:
        return 0.0
    if value > q[-1]:
        return 100.0
    lo, hi = bisect.bisect_left(q, value), bisect.bisect_right(q, value)
    if lo < hi:
        return (lo + hi - 1) / 2
    i = lo - 1
    return i + (value - q[i]) / (q[lo] - q[i])


def top_of(raw_top: float, n: int) -> int:
    if n >= TOP1_MIN_USERS and raw_top <= 1:
        return 1
    return max(TOP_STEP, math.ceil(raw_top / TOP_STEP - 1e-9) * TOP_STEP)


def place(bucket: dict[str, Any], value: float, cohort: str = "all") -> Optional[Placement]:
    """Место значения в корзине; None — корзина меньше K."""
    n = int(bucket.get("n") or 0)
    if n < _k() or "q" not in bucket:
        return None
    q = bucket["q"]
    median = q[50]
    you_bin = _bin(value, bucket["lo"], bucket["hi"])
    if value < median:
        return Placement(cohort, n, False, None, median - value, None, None, you_bin, bucket)
    top = min(50, top_of(100 - percentile_of(q, value), n))
    next_top = top - TOP_STEP if top > TOP_STEP else (1 if top == TOP_STEP and n >= TOP1_MIN_USERS else None)
    to_next = None
    if next_top is not None:
        need = q[100 - next_top] - value
        to_next = need if need > 0 else None
    best = value >= q[100]
    if best:
        next_top, to_next = None, None
    return Placement(cohort, n, True, top, None, next_top, to_next, you_bin, bucket, best)


def pick_bucket(entry: Optional[dict[str, Any]], gender: Optional[str], basis: str
                ) -> tuple[str, Optional[dict[str, Any]]]:
    """Когорта пола, если в ней не меньше K, иначе все."""
    if entry is None:
        return "all", None
    cohorts = entry["cohorts"]
    if gender in ("male", "female"):
        own = cohorts.get(gender, {}).get(basis)
        if own and int(own.get("n") or 0) >= _k():
            return gender, own
    return "all", cohorts.get("all", {}).get(basis)


@dataclass(frozen=True)
class OwnBest:
    identity: str
    metric: str
    exercise_id: int
    name: str
    #: В единицах атлета: e1RM (или повторы).
    value: float
    #: Лучший подход: вес (у «… с весом» — довесок) и повторы.
    weight: float
    reps: int


def own_bests(rows: list[Any]) -> dict[str, OwnBest]:
    """Лучшее атлета по каждой идентичности каталога из строк
    `db.standing_own_sets`. Упражнение — то, где был последний подход
    идентичности (у атлета их может быть два: копия и объединённое своё)."""
    out: dict[str, OwnBest] = {}
    last: dict[str, tuple[int, str]] = {}
    for r in rows:
        identity = catalog_identity(r["original_name"])
        if identity is None:
            continue
        metric = metric_for(identity)
        if metric is None:
            continue
        last[identity] = (r["exercise_id"], r["display_name"])
        reps = int(r["reps"] or 0)
        if metric == progress_score.METRIC_REPS:
            value, weight = float(reps), 0.0
            if reps > MAX_REPS:
                continue
        else:
            weight = float((r["weight"] if seed_data.is_weighted_bodyweight(identity) else r["load"]) or 0)
            value = epley(weight, reps)
        if value <= 0:
            continue
        prev = out.get(identity)
        if prev is None or value > prev.value:
            out[identity] = OwnBest(identity, metric, r["exercise_id"], r["display_name"], value, weight, reps)
    return {
        identity: OwnBest(b.identity, b.metric, last[identity][0], last[identity][1], b.value, b.weight, b.reps)
        for identity, b in out.items()
    }


def _since() -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")


# ---------- тексты ----------


def _num(value: float) -> str:
    return formatting.format_weight(round(value, 1))


def _weight(value_user_unit: float, unit: str) -> str:
    return f"{_num(value_user_unit)}{formatting.unit_label(unit)}"


def _ratio(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") + "×"


def _round_up(value: float, step: float) -> float:
    return max(step, math.ceil(value / step - 1e-9) * step)


def _delta_text(delta: float, metric: str, basis: str, unit: str, best: OwnBest, bw_kg: Optional[float]) -> str:
    """Сколько добавить к лучшему подходу: вес на блинах (шаг блина атлета)
    или повторы. `delta` — в мерке корзины (кг e1RM, доля веса тела, повторы)."""
    if metric == progress_score.METRIC_REPS:
        return i18n.t("standing.reps", n=max(1, math.ceil(delta - 1e-9)))
    e1rm_kg = delta * bw_kg if basis == "rel" and bw_kg else delta
    e1rm_user = e1rm_kg * config.LB_PER_KG if unit == "lb" else e1rm_kg
    per_set = e1rm_user if best.reps <= 1 else e1rm_user / (1 + best.reps / 30.0)
    return _weight(_round_up(per_set, analytics.plate_step(unit)), unit)


def _axis_value(value: float, metric: str, basis: str, unit: str) -> str:
    if metric == progress_score.METRIC_REPS:
        return str(int(round(value)))
    if basis == "rel":
        return _ratio(round(value, 2))
    shown = value * config.LB_PER_KG if unit == "lb" else value
    step = 5.0 if unit == "lb" or value >= 40 else 2.5
    return _weight(round(round(shown / step) * step, 2), unit)


def _value_text(best: OwnBest, basis: str, unit: str, bw_kg: Optional[float]) -> str:
    if best.metric == progress_score.METRIC_REPS:
        return i18n.t("standing.value.reps", n=int(best.value))
    if basis == "rel" and bw_kg:
        kg = formatting.to_kg(best.value, unit)
        return i18n.t("standing.value.rel", ratio=_ratio(round(kg / bw_kg, 2)))
    key = "standing.value.e1rm_added" if seed_data.is_weighted_bodyweight(best.identity) else "standing.value.e1rm"
    return i18n.t(key, value=_weight(round(best.value), unit))


def set_text(best: OwnBest, unit: str) -> str:
    """«105кг × 3», «+20кг × 5», «14 повторов»."""
    if best.metric == progress_score.METRIC_REPS:
        return i18n.t("standing.value.reps", n=best.reps)
    plus = "+" if seed_data.is_weighted_bodyweight(best.identity) else ""
    return f"{plus}{_weight(best.weight, unit)} × {best.reps}"


def _empty(exercise_id: int, name: str, metric: Optional[str], basis: str, reason: str,
           value_text: Optional[str] = None) -> dict[str, Any]:
    return {
        "available": False, "reason": reason, "exercise_id": exercise_id, "name": name,
        "metric": metric or progress_score.METRIC_E1RM, "basis": basis, "above_median": False,
        "top": None, "stronger_than": None, "label": i18n.t(f"standing.label.{reason}"),
        "value_text": value_text, "cohort_text": None, "hint": i18n.t(f"standing.hint.{reason}"),
        "histogram": [], "you_bin": None, "axis_min": None, "axis_median": None, "axis_max": None,
        "rel_available": False,
    }


def standing_json(
    *, exercise_id: int, name: str, identity: Optional[str], best: Optional[OwnBest], basis: str,
    unit: str, gender: Optional[str], bw_kg: Optional[float], buckets: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Объект `Standing` (см. api_v1_standing) — под языком атлета."""
    if identity is None:
        return _empty(exercise_id, name, None, basis, REASON_NOT_CATALOG)
    metric = metric_for(identity)
    if metric is None:
        # Удержание на время: сравнивать секунды по лучшему подходу не с чем.
        return _empty(exercise_id, name, None, basis, REASON_NOT_CATALOG)
    if best is None:
        return _empty(exercise_id, name, metric, basis, REASON_NO_DATA)
    entry = buckets.get(identity)
    rel_value = None
    if metric == progress_score.METRIC_E1RM and bw_kg and BODYWEIGHT_KG[0] <= bw_kg <= BODYWEIGHT_KG[1]:
        rel_value = formatting.to_kg(best.value, unit) / bw_kg
    rel_placement = None
    if rel_value is not None:
        cohort, bucket = pick_bucket(entry, gender, "rel")
        rel_placement = place(bucket, rel_value, cohort) if bucket else None
    if basis == "rel" and rel_placement is not None:
        used_basis, placement = "rel", rel_placement
    else:
        cohort, bucket = pick_bucket(entry, gender, "abs")
        value = best.value if metric == progress_score.METRIC_REPS else formatting.to_kg(best.value, unit)
        used_basis, placement = "abs", (place(bucket, value, cohort) if bucket else None)
    if placement is None:
        return {**_empty(exercise_id, name, metric, used_basis, REASON_FEW,
                         _value_text(best, used_basis, unit, bw_kg)),
                "rel_available": rel_placement is not None}
    if not placement.above_median:
        return {**_empty(exercise_id, name, metric, used_basis, REASON_BELOW_MEDIAN,
                         _value_text(best, used_basis, unit, bw_kg)),
                "rel_available": rel_placement is not None}
    q = placement.bucket["q"]
    if placement.best:
        label = i18n.t("standing.label.best")
        hint = i18n.t("standing.hint.best")
    else:
        label = i18n.t("standing.label.top", top=placement.top)
        hint = None
        if placement.to_next is not None and placement.next_top is not None:
            hint = i18n.t("standing.hint.next", top=placement.next_top,
                          delta=_delta_text(placement.to_next, metric, used_basis, unit, best, bw_kg))
    return {
        "available": True, "reason": None, "exercise_id": exercise_id, "name": name,
        "metric": metric, "basis": used_basis, "above_median": placement.above_median,
        "top": placement.top,
        "stronger_than": None if placement.top is None else 100 - placement.top,
        "label": label,
        "value_text": _value_text(best, used_basis, unit, bw_kg),
        "cohort_text": i18n.t(f"standing.cohort.{placement.cohort}"),
        "hint": hint,
        "histogram": list(placement.bucket["h"]),
        "you_bin": placement.you_bin,
        "axis_min": _axis_value(placement.bucket["lo"], metric, used_basis, unit),
        "axis_median": i18n.t("standing.axis.median", value=_axis_value(q[50], metric, used_basis, unit)),
        "axis_max": _axis_value(placement.bucket["hi"], metric, used_basis, unit),
        "rel_available": rel_placement is not None,
    }


def _bw_kg(user: Any, row: Any) -> Optional[float]:
    if row is None or not row["weight"]:
        return None
    return formatting.to_kg(float(row["weight"]), user["unit"])


async def exercise_standing(user: Any, exercise: Any, basis: str) -> dict[str, Any]:
    """`Standing` одного упражнения атлета."""
    import db

    buckets = await refresh()
    identity = catalog_identity(exercise["original_name"])
    best = None
    if identity is not None:
        bests = own_bests(await db.standing_own_sets(user["telegram_id"], since=_since()))
        best = bests.get(identity)
    bw = _bw_kg(user, await db.get_latest_bodyweight(user["telegram_id"]))
    return standing_json(
        exercise_id=exercise["id"], name=exercise["display_name"], identity=identity, best=best,
        basis=basis, unit=user["unit"], gender=user["gender"], bw_kg=bw, buckets=buckets,
    )


MAX_ITEMS = 8
MAX_UNAVAILABLE = 2


async def overview(user: Any, basis: str) -> dict[str, Any]:
    """GET /v1/standing: упражнения атлета с подходами за окно, только
    каталог; сначала доступные (по величине корзины), потом до двух «мало
    данных»; не больше MAX_ITEMS."""
    import db

    buckets = await refresh()
    bests = own_bests(await db.standing_own_sets(user["telegram_id"], since=_since()))
    bw = _bw_kg(user, await db.get_latest_bodyweight(user["telegram_id"]))
    available, few = [], []
    for identity, best in bests.items():
        item = standing_json(
            exercise_id=best.exercise_id, name=best.name, identity=identity, best=best, basis=basis,
            unit=user["unit"], gender=user["gender"], bw_kg=bw, buckets=buckets,
        )
        size = int(((buckets.get(identity) or {}).get("cohorts", {}).get("all", {}).get("abs") or {}).get("n") or 0)
        if item["reason"] == REASON_BELOW_MEDIAN:
            continue
        (available if item["available"] else few).append((size, best.name, item))
    available.sort(key=lambda x: (-x[0], x[1]))
    few.sort(key=lambda x: (-x[0], x[1]))
    items = [x[2] for x in available][:MAX_ITEMS]
    items += [x[2] for x in few][:min(MAX_UNAVAILABLE, MAX_ITEMS - len(items))]
    return {"basis": basis, "intro": i18n.t("standing.intro", days=WINDOW_DAYS), "items": items}


# ---------- итог тренировки ----------


async def finish_standing_json(workout: Any, user: Any) -> Optional[dict[str, Any]]:
    """`rewards.standing`: только если эта тренировка поставила рекорд (той же
    меркой, всё время, при прошлой истории упражнения) в упражнении с
    доступной корзиной и атлет после неё выше середины. Дёшево: корзины —
    из памяти или из базы (вчерашние пересчитываются в фоне; считает на месте
    только самый первый запрос после выкладки, когда таблица пуста), свои
    числа — по агрегату на тренировку, без загрузки подходов истории."""
    import db

    buckets = await refresh()
    if not buckets:
        return None
    user_id = workout["user_id"]
    this = own_bests(await db.standing_own_sets(user_id, workout_id=workout["id"]))
    if not this:
        return None
    rows = await db.standing_bests_by_workout(user_id)
    since = _since()
    unit = user["unit"]
    best_pick = None
    for identity, best in this.items():
        entry = buckets.get(identity)
        if entry is None:
            continue
        all_before = 0.0
        window_before = 0.0
        for r in rows:
            if r["workout_id"] == workout["id"] or catalog_identity(r["original_name"]) != identity:
                continue
            v = _row_value(best.metric, identity, r)
            all_before = max(all_before, v)
            if r["started_at"] >= since:
                window_before = max(window_before, v)
        if all_before <= 0 or best.value <= all_before:
            continue
        window_after = max(window_before, best.value)
        cohort, bucket = pick_bucket(entry, user["gender"], "abs")
        to_bucket = (lambda v: v) if best.metric == progress_score.METRIC_REPS else (
            lambda v: formatting.to_kg(v, unit))
        after = place(bucket, to_bucket(window_after), cohort) if bucket else None
        if after is None or not after.above_median:
            continue
        gained = None
        if window_before > 0:
            before = place(bucket, to_bucket(window_before), cohort)
            if before is not None and before.above_median and before.top - after.top > 0:
                gained = before.top - after.top
        key = (gained or 0, -after.top)
        if best_pick is None or key > best_pick[0]:
            best_pick = (key, identity, best, after, gained)
    if best_pick is None:
        return None
    _key, identity, best, after, gained = best_pick
    hint = None
    if after.to_next is not None and after.next_top is not None:
        hint = i18n.t("standing.hint.next", top=after.next_top,
                      delta=_delta_text(after.to_next, best.metric, "abs", unit, best, None))
    shown = set_text(best, unit)
    headline = (i18n.t("standing.finish.headline_gained", set=shown, gained=gained) if gained
                else i18n.t("standing.finish.headline", set=shown, stronger=100 - after.top))
    stronger = 100 - after.top
    return {
        "exercise_id": best.exercise_id,
        "name": best.name,
        "top": after.top,
        "stronger_than": stronger,
        "gained": gained,
        "eyebrow": formatting.caps(i18n.t("standing.finish.eyebrow", name=best.name)),
        "headline": headline,
        "hint": hint,
        "footnote": i18n.t(f"standing.finish.footnote.{after.cohort}", days=WINDOW_DAYS),
        "crowd_index": max(0, min(BINS - 1, round(stronger / 5) - 1)),
        # «e1RM ≈ 148кг» — чем меряли (владелец: «упоминание 1ПМ» на слайде).
        "value_text": _value_text(best, "abs", unit, None),
        # Сильнейший в корзине — слайд пишет «№1 в дневнике» вместо «95%».
        "best": after.best,
        "best_label": i18n.t("standing.label.best") if after.best else None,
    }


def _row_value(metric: str, identity: str, row: Any) -> float:
    if metric == progress_score.METRIC_REPS:
        return float(row["best_reps"] or 0)
    value = row["added_e1rm"] if seed_data.is_weighted_bodyweight(identity) else row["load_e1rm"]
    return float(value or 0)
