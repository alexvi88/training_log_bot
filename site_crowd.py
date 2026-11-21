"""«Что пишут в дневник наши атлеты» — обезличенные цифры по всем дневникам
на страницах техники сайта (`/exercises/<упражнение>`) и на «чем заменить»
(`/exercises/<упражнение>/alternatives`).

Зачем: своих данных, кроме нас, ни у кого нет — это то, чем страница
техники отличается от сотни таких же пересказов техники в сети.

**Приватность — жёстко.**

- Каждая цифра стоит минимум на `K_MIN_USERS` разных атлетах: и число
  атлетов, и вес, и вес по полу (у каждого пола — свои K), и отношение к весу
  тела, и повторы, и подходы, и ритм. Не набралось — этой цифры нет, а не
  набралось ни одной — блока нет совсем, без заглушек.
- Свои аккаунты и демо мимо (`db.own_account_ids`).
- Ничего про отдельного человека: сначала у каждого атлета берётся его
  типичное значение (медиана по его тренировкам — так один фанат с сотней
  тренировок весит как один человек), потом по атлетам — медиана и
  25-й–75-й процентили. Числа округлены, атлетов — «20+», «50+»…

**Считается раз в сутки, не на запрос.** Страница берёт посчитанное
(`cached`); пересчёт — `refresh` из маршрута страницы и sitemap, не чаще
CACHE_SECONDS, под замком (два первых запроса не считают дважды).

**Дата страницы честная.** Показанные числа хранятся в базе
(`site_crowd_stats`) вместе с днём, когда они заметно поменялись, — это и
есть `lastmod` страницы (`changed_on`). Новое значение заменяет показанное,
только когда ушло от него дальше чем на ¾ шага округления, а число атлетов
переходит в следующую ступень с запасом 10%: суточная дрожь медианы около
границы округления дату не двигает.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import statistics
import time
from collections import defaultdict
from typing import Any, Optional

import config

logger = logging.getLogger(__name__)

# Минимум разных атлетов за любой показанной цифрой.
K_MIN_USERS = 20
# Окно — последний год: картина «как тренируются сейчас», а не за всю историю.
WINDOW_DAYS = 365
CACHE_SECONDS = 24 * 3600
RETRY_SECONDS = 600
# Ритм («раз в N дней») — только у тех, кто сделал упражнение хотя бы трижды;
# перерыв длиннее GAP_MAX_DAYS — это пауза в тренировках, а не ритм.
MIN_SESSIONS_FOR_RHYTHM = 3
GAP_MAX_DAYS = 60
# Ступени числа атлетов: показываем «не меньше», а не точное число.
ATHLETE_STEPS = (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000)
# Отсев опечаток: такие подходы и такой вес тела в цифры не идут.
MAX_TOP_KG = 500.0
MAX_REPS = 100
BODYWEIGHT_KG = (30.0, 250.0)
# Насколько новое значение должно уйти от показанного, чтобы его заменить,
# — в долях шага округления.
HYSTERESIS = 0.75

_cache: dict[str, Any] = {"at": 0.0, "data": {}, "changed": {}}
_lock = asyncio.Lock()


# ---------- округление ----------


def weight_step(kg: float) -> float:
    return 2.5 if kg < 40 else 5.0


def _snap(value: float, step: float) -> float:
    return round(round(value / step) * step, 2)


def _keep_or_snap(raw: float, step: float, shown: Optional[float]) -> float:
    if shown is not None and abs(raw - shown) <= step * HYSTERESIS:
        return shown
    return _snap(raw, step)


def athletes_step(count: int, shown: Optional[int] = None) -> int:
    """Ступень «N+» для числа атлетов (count ≥ K). Вверх — с запасом 10%,
    вниз — сразу: показанное «50+» никогда не больше настоящего числа."""
    step = max(s for s in ATHLETE_STEPS if s <= count)
    if shown and shown < step and count < step * 1.1:
        return shown
    return step


# ---------- расчёт ----------


def _spread(values: list[float]) -> Optional[tuple[float, float, float]]:
    """(p25, медиана, p75) по значениям атлетов — только когда их ≥ K."""
    if len(values) < K_MIN_USERS:
        return None
    q1, med, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return q1, med, q3


def _day(iso: str) -> dt.date:
    return dt.date.fromisoformat(iso[:10])


def aggregate(rows: list[tuple]) -> dict[str, dict[str, Any]]:
    """Сырые строки `db.site_crowd_sessions` → цифры по упражнениям, ещё не
    округлённые. Упражнение без K атлетов в ответ не попадает вовсе; цифра,
    у которой не набралось K, — тоже."""
    # идентичность → атлет → его тренировки с этим упражнением
    sessions: dict[str, dict[int, list[tuple]]] = defaultdict(lambda: defaultdict(list))
    profile: dict[int, tuple[Optional[str], Optional[float]]] = {}
    for user_id, _wid, started_at, identity, bw_load, n_sets, top_kg, top_reps, gender, bw_kg in rows:
        sessions[identity][user_id].append((started_at, bw_load or "none", n_sets, top_kg, top_reps))
        profile[user_id] = (gender, bw_kg)

    out: dict[str, dict[str, Any]] = {}
    for identity, by_user in sessions.items():
        if len(by_user) < K_MIN_USERS:
            continue
        weight: list[float] = []
        weight_by_gender: dict[str, list[float]] = {"male": [], "female": []}
        ratio: list[float] = []
        reps: list[float] = []
        sets: list[float] = []
        gaps: list[float] = []
        for user_id, items in by_user.items():
            gender, bw_kg = profile[user_id]
            # Вес — только у упражнений, где нагрузка — железо: у подтягиваний
            # «вес подхода» — это вес тела плюс отягощение, сравнивать нечего.
            loaded = [top for _s, load, _n, top, _r in items if load == "none" and top and 0 < top <= MAX_TOP_KG]
            if loaded:
                w = statistics.median(loaded)
                weight.append(w)
                if gender in weight_by_gender:
                    weight_by_gender[gender].append(w)
                if bw_kg and BODYWEIGHT_KG[0] <= bw_kg <= BODYWEIGHT_KG[1]:
                    ratio.append(w / bw_kg)
            user_reps = [r for *_x, r in items if r and 0 < r <= MAX_REPS]
            if user_reps:
                reps.append(statistics.median(user_reps))
            sets.append(statistics.median(n for _s, _l, n, _t, _r in items))
            days = sorted({_day(s) for s, *_x in items})
            if len(days) >= MIN_SESSIONS_FOR_RHYTHM:
                user_gaps = [(b - a).days for a, b in zip(days, days[1:], strict=False) if (b - a).days <= GAP_MAX_DAYS]
                if len(user_gaps) >= MIN_SESSIONS_FOR_RHYTHM - 1:
                    gaps.append(statistics.median(user_gaps))
        stats: dict[str, Any] = {"athletes": len(by_user)}
        for key, values in (("weight", weight), ("weight_m", weight_by_gender["male"]),
                            ("weight_f", weight_by_gender["female"]), ("bw", ratio), ("reps", reps)):
            spread = _spread(values)
            if spread:
                stats[key] = spread
        if len(sets) >= K_MIN_USERS:
            stats["sets"] = statistics.median(sets)
        if len(gaps) >= K_MIN_USERS:
            stats["every_days"] = statistics.median(gaps)
        out[identity] = stats
    return out


def publish(raw: dict[str, Any], shown: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Округлить цифры одного упражнения для страницы, держась за уже
    показанные, пока новое значение не ушло от них заметно."""
    shown = shown or {}
    out: dict[str, Any] = {"athletes": athletes_step(raw["athletes"], shown.get("athletes"))}
    for key in ("weight", "weight_m", "weight_f", "bw", "reps"):
        if key not in raw:
            continue
        old = shown.get(key) or [None, None, None]
        triple = []
        for value, prev in zip(raw[key], old, strict=True):
            step = weight_step(value) if key.startswith("weight") else (0.05 if key == "bw" else 1.0)
            triple.append(_keep_or_snap(value, step, prev))
        if key == "reps":
            triple = [int(x) for x in triple]
        out[key] = triple
    for key in ("sets", "every_days"):
        if key in raw:
            out[key] = int(_keep_or_snap(raw[key], 1.0, shown.get(key)))
    return out


async def refresh(force: bool = False) -> dict[str, dict[str, Any]]:
    """Пересчитать раз в CACHE_SECONDS; вернуть показанные цифры."""
    now = time.monotonic()
    if not force and _cache["at"] and now - float(_cache["at"]) < CACHE_SECONDS:
        return _cache["data"]
    async with _lock:
        if not force and _cache["at"] and time.monotonic() - float(_cache["at"]) < CACHE_SECONDS:
            return _cache["data"]
        _cache["at"] = time.monotonic()
        try:
            await _recount()
        except Exception:  # noqa: BLE001 — без базы страница просто без блока
            logger.debug("site crowd numbers unavailable", exc_info=True)
            # Сбой — не повод ждать сутки: следующая попытка через RETRY_SECONDS.
            _cache["at"] = time.monotonic() - CACHE_SECONDS + RETRY_SECONDS
    return _cache["data"]


async def _recount() -> None:
    import db

    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = await db.site_crowd_sessions(since, await db.own_account_ids())
    fresh = aggregate(rows)
    state = await db.site_crowd_state()
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    changed_rows: dict[str, tuple[str, str]] = {}
    data: dict[str, dict[str, Any]] = {}
    changed: dict[str, str] = {}
    for identity in set(fresh) | set(state):
        old_payload, old_on = state.get(identity, ("null", ""))
        old = json.loads(old_payload)
        new = publish(fresh[identity], old) if identity in fresh else None
        payload = json.dumps(new, ensure_ascii=False, sort_keys=True)
        if payload != json.dumps(old, ensure_ascii=False, sort_keys=True):
            changed_rows[identity] = (payload, today)
            old_on = today
        if new:
            data[identity] = new
        if old_on:
            changed[identity] = old_on
    if changed_rows:
        await db.save_site_crowd_state(changed_rows)
    _cache["data"] = data
    _cache["changed"] = changed


def cached() -> dict[str, dict[str, Any]]:
    return _cache["data"]


def stats_for(identity: str) -> Optional[dict[str, Any]]:
    return _cache["data"].get(identity)


def changed_on(identities: list[str]) -> Optional[str]:
    """Последний день, когда цифры этих упражнений заметно поменялись."""
    dates = [_cache["changed"][i] for i in identities if i in _cache["changed"]]
    return max(dates) if dates else None


def reset() -> None:
    _cache["at"] = 0.0
    _cache["data"] = {}
    _cache["changed"] = {}


# ---------- показ ----------


def _kg(value: float, lang: str) -> str:
    import seo_pages as sp

    t = sp._t(lang)
    text = sp._w(value, t("seo.tool.unit_kg"))
    if lang == "en":
        lb = _snap(value * config.LB_PER_KG, 5.0)
        text += f" ({sp._w(lb, t('seo.tool.unit_lb'))})"
    return text


def _ratio(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") + "×"


def _spread_html(lang: str, triple: list, fmt) -> str:
    import seo_pages as sp

    low, med, high = triple
    if low == high:
        # Разброса нет («от 8 до 8») — только само значение.
        return fmt(med)
    return sp._t(lang)("seo.crowd.spread_html", median=fmt(med), low=fmt(low), high=fmt(high))


def exercise_block(identity: str, lang: str) -> str:
    """Блок на странице техники. Нет цифр — пустая строка."""
    import seo_pages as sp

    stats = stats_for(identity)
    if not stats:
        return ""
    t = sp._t(lang)
    rows: list[tuple[str, str]] = []
    if "weight" in stats:
        rows.append((t("seo.crowd.weight"), _spread_html(lang, stats["weight"], lambda v: _kg(v, lang))))
    for key, label in (("weight_m", "seo.crowd.men"), ("weight_f", "seo.crowd.women")):
        if key in stats:
            rows.append((t(label), _spread_html(lang, stats[key], lambda v: _kg(v, lang))))
    if "bw" in stats:
        rows.append((t("seo.crowd.bw"), _spread_html(lang, stats["bw"], _ratio)))
    if "reps" in stats:
        rows.append((t("seo.crowd.reps"), _spread_html(lang, stats["reps"], lambda v: str(int(v)))))
    if "sets" in stats:
        rows.append((t("seo.crowd.sets"), sp._e(t("seo.crowd.sets_value", sets=stats["sets"]))))
    if "every_days" in stats:
        rows.append((t("seo.crowd.rhythm"), sp._e(t("seo.crowd.rhythm_value", days=stats["every_days"]))))
    facts = "".join(f"<dt>{sp._e(k)}</dt><dd>{v}</dd>" for k, v in rows)
    return (
        f"<section class='crowd reveal' id=crowd><h2>{sp._e(t('seo.crowd.h'))}</h2>"
        f"<p>{sp._e(t('seo.crowd.lead', athletes=stats['athletes'], k=K_MIN_USERS))}</p>"
        + (f"<dl class=facts>{facts}</dl>" if facts else "")
        + f"<p class=muted>{sp._e(t('seo.crowd.note'))}</p></section>"
    )


def swaps_block(identities: list[str], lang: str) -> str:
    """Блок на «чем заменить»: сколько атлетов пишут каждую из замен на
    странице — только те, за которыми ≥ K. Ни одной — пустая строка."""
    import seed_data
    import seo_pages as sp

    known = [(i, stats_for(i)["athletes"]) for i in identities if stats_for(i)]
    if not known:
        return ""
    t = sp._t(lang)
    known.sort(key=lambda item: (-item[1], identities.index(item[0])))
    items = "".join(
        f"<li><span>{sp._e(seed_data.localized_exercise_name(i, lang))}</span> "
        f"<span class=muted>{sp._e(t('seo.crowd.athletes', count=n))}</span></li>" for i, n in known)
    return (f"<section class='crowd reveal' id=crowd><h2>{sp._e(t('seo.crowd.swaps.h'))}</h2>"
            f"<p>{sp._e(t('seo.crowd.swaps.lead', k=K_MIN_USERS))}</p><ul class=links>{items}</ul>"
            f"<p class=muted>{sp._e(t('seo.crowd.swaps.note'))}</p></section>")
