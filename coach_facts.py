"""Карточки движений и сводки для AI-тренера: сигналы считает код, модель
рассуждает.

Живой тренер не пересчитывает подходы в голове: он смотрит в дневник и видит
«77 держит на 6-7, до этого 73 вырос 8→11, на прошлой неделе болел». Модели
мы отдавали сырые подходы, и она путала веса («повторы упали 12→7», когда 12
было на 73), не видела перерывов и болезни и сама решала, что встало. Здесь
всё это сводится в компактный текст на языке атлета:

- карточка движения (movement_card) — статус из analytics.stall_facts (сам
  вердикт тут не пересчитывается), рабочий вес с датой, повторы ТОЛЬКО на
  одном весе, прошлый вес и бэкоффы, e1RM с датами, последние сессии в
  компактной записи, перерывы и заметки о боли, сне и болезни;
- отчёт о прогрессе (progress_report) — карточки активных движений и общий
  контекст: частота, перерывы, вес тела;
- отчёт о балансе (balance_report) — рабочие подходы в неделю по группам и
  мишеням против нормы, частота, паттерны движений, программа «план против
  факта», что встало.

Каждое число несёт дату: модель цитирует отсюда, а не считает сама.

Разминка (подходы легче WARMUP_SHARE от тяжёлого веса сессии в НАЧАЛЕ
упражнения) выкидывается только из счёта рабочих подходов и из показа серии.
e1RM, тренд и вердикт считаются по всем подходам — лучший e1RM сессии и так
даёт тяжёлый подход.

Модуль в i18n_coverage.LOCALIZED: весь текст — ключи `coach.*`. Единственная
кириллица — регулярка заметок (_NOTE_FLAGS), она ищет слова атлета, а не
показывается.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import analytics
import db
import formatting
import i18n
import seed_data
import timeutil

# Подход легче этой доли от тяжёлого веса сессии, стоящий В НАЧАЛЕ упражнения,
# — разминка. Только начало: бэкофф 73 после 77 — рабочий, а не разминка.
WARMUP_SHARE = 0.65
# Окно карточки и отчёта о прогрессе: частота, активность, e1RM.
CARD_WINDOW_WEEKS = 8
# Сколько истории поднимаем под карточки: «до этого 73 за 4 сессии» и
# прибавка веса бывают старше окна.
CARD_HISTORY_WEEKS = 16
# Перерыв — столько дней подряд без единой тренировки.
BREAK_MIN_DAYS = 7
# Сессий в строке «серия» и точек в строке рабочего веса.
CARD_SERIES_SESSIONS = 4
# Карточек в отчёте о прогрессе: ~150-250 токенов на карточку.
REPORT_MAX_CARDS = 12
# Окно отчёта о балансе.
BALANCE_WEEKS = 4
# Заметок на карточку и длина цитаты.
NOTES_PER_CARD = 3
NOTE_QUOTE_CHARS = 40

# Боль, сон, болезнь — в заметках атлета, на обоих языках. Простое совпадение
# слов, без модели: ложное срабатывание стоит строки в карточке, а пропуск —
# «встал» там, где человек неделю болел. «бол» только с окончаниями: иначе
# ловилось бы «больше».
# Русская половина — один литерал: он же стоит в i18n_coverage.ALLOWED_CYRILLIC.
_NOTE_FLAGS_RU = r"бол(?:ит|ят|ел|ела|ели|ею|еть|ь\b|и\b|ью|езн)|ноет|ноют|ныл|травм|потянул|защемил|простыл|простуд|температур|грипп|ковид|орви|не\s*выспал|недосып|бессонн|плохо\s+спал|мало\s+спал|\bсон\b|\bсна\b"
_NOTE_FLAGS_EN = (
    r"\bpain|hurt|\bsore|\bache|injur|tweak|strain|\bsick|\bill\b|\bflu\b|covid|fever"
    r"|sleep|slept|insomnia"
)
_NOTE_FLAGS = re.compile(f"{_NOTE_FLAGS_RU}|{_NOTE_FLAGS_EN}", re.IGNORECASE)


def note_is_flagged(text: str) -> bool:
    return bool(_NOTE_FLAGS.search(text or ""))


# ---------- данные ----------


@dataclass
class LoggedSet:
    key: float  # вес, по которому сравниваем и который показываем (см. _key_of)
    load: float  # нагрузка для e1RM (db.load_of)
    reps: int
    rpe: Optional[float]


@dataclass
class Session:
    workout_id: int
    started_at: str
    sets: list[LoggedSet]

    @property
    def day(self) -> dt.date:
        return dt.date.fromisoformat(self.started_at[:10])

    @property
    def top_key(self) -> float:
        return max((s.key for s in self.sets), default=0.0)

    def best_reps_at(self, key: float) -> Optional[LoggedSet]:
        at = [s for s in self.sets if s.key == key]
        return max(at, key=lambda s: s.reps) if at else None


@dataclass
class Movement:
    exercise_id: int
    name: str  # display_name — уже на языке атлета
    identity: Optional[str]  # original_name — русская идентичность шаблона
    group: Optional[str]  # каноническое (русское) имя группы
    bodyweight_load: str  # none | full | assisted
    sessions: list[Session] = field(default_factory=list)


def _key_of(logged: float, mode: str) -> float:
    """Вес, по которому сравниваются подходы. Железо — записанный вес.
    Подтягивания с поясом — добавленный (+10): нагрузка от веса тела гуляет от
    взвешивания к взвешиванию, и «тот же вес» иначе не повторялся бы никогда.
    Гравитрон — помощь со знаком минус: больше помощи — легче."""
    if mode == "assisted":
        return -abs(logged) if logged else 0.0
    return logged


def movements_from_rows(rows: list[Any]) -> list[Movement]:
    """Строки db.list_sets_for_coach (по упражнению, старые первыми) → движения."""
    out: dict[int, Movement] = {}
    for r in rows:
        mode = r["bodyweight_load"] or "none"
        m = out.get(r["exercise_id"])
        if m is None:
            m = out[r["exercise_id"]] = Movement(
                r["exercise_id"], r["display_name"], r["original_name"] or None,
                r["group_name"], mode,
            )
        if not m.sessions or m.sessions[-1].workout_id != r["workout_id"]:
            m.sessions.append(Session(r["workout_id"], r["started_at"], []))
        m.sessions[-1].sets.append(
            LoggedSet(_key_of(r["logged_weight"], mode), r["weight"], r["reps"], r["rpe"])
        )
    for m in out.values():
        m.sessions.sort(key=lambda s: (s.started_at, s.workout_id))
    return list(out.values())


def working_sets(sets: list[LoggedSet]) -> list[LoggedSet]:
    """Подходы без разминки: ведущие подходы легче WARMUP_SHARE от тяжёлого
    веса сессии. Вес тела без добавки (нагрузка 0) — всё рабочее."""
    top = max((s.load for s in sets), default=0.0)
    if top <= 0:
        return list(sets)
    i = 0
    while i < len(sets) and sets[i].load < top * WARMUP_SHARE:
        i += 1
    return list(sets[i:])


def _stats_sessions(m: Movement, formula: str) -> list[analytics.SessionStats]:
    """Сессии в виде analytics — по нагрузке и со ВСЕМИ подходами: вердикт и
    e1RM считаются как везде (classify_stall, e1rm_trend)."""
    return [
        analytics.SessionStats(
            s.workout_id, s.started_at,
            [analytics.SetRow(x.load, x.reps, s.workout_id, s.started_at, x.rpe) for x in s.sets],
            formula,
        )
        for s in m.sessions
    ]


# ---------- формат ----------


def _num(x: float) -> str:
    x = round(x, 2)
    return f"{0.0 if x == 0 else x:g}"


def _day(d: dt.date, today: dt.date) -> str:
    """09-28 в этом году, 2025-12-30 — в прошлом: год модель знает из шапки."""
    return d.strftime("%m-%d") if d.year == today.year else d.isoformat()


def _range(a: dt.date, b: dt.date, today: dt.date) -> str:
    return f"{_day(a, today)}–{_day(b, today)}"


def _w(key: float, mode: str) -> str:
    if mode in ("full", "assisted"):
        if key == 0:
            return i18n.t("coach.bw")
        return f"+{_num(key)}" if key > 0 else f"-{_num(abs(key))}"
    return _num(key)


def _rpe(s: LoggedSet) -> str:
    return f"@{s.rpe:g}" if s.rpe is not None else ""


def _set(s: LoggedSet, mode: str) -> str:
    return f"{_w(s.key, mode)}×{s.reps}{_rpe(s)}"


def compact_session(sets: list[LoggedSet], mode: str) -> str:
    """«77×6@9 (+73×12@9,10@10)»: тяжёлый подход первым, остальные — по весу
    подряд, повторы через запятую. Разминки нет (working_sets)."""
    ws = working_sets(sets)
    if not ws:
        return ""
    top = max(ws, key=lambda s: (s.key, s.reps))
    rest = list(ws)
    rest.remove(top)
    groups: list[tuple[float, list[str]]] = []
    for s in rest:
        if groups and groups[-1][0] == s.key:
            groups[-1][1].append(f"{s.reps}{_rpe(s)}")
        else:
            groups.append((s.key, [f"{s.reps}{_rpe(s)}"]))
    tail = " + ".join(f"{_w(k, mode)}×{','.join(r)}" for k, r in groups)
    return _set(top, mode) + (f" (+{tail})" if tail else "")


# ---------- перерывы и заметки ----------


def find_breaks(workout_days: list[dt.date], today: dt.date) -> list[tuple[dt.date, Optional[dt.date], int]]:
    """(первый день без тренировок, последний или None — длится до сегодня,
    дней) — промежутки от BREAK_MIN_DAYS дней подряд без тренировок."""
    days = sorted(set(workout_days))
    out: list[tuple[dt.date, Optional[dt.date], int]] = []
    for a, b in zip(days, days[1:], strict=False):
        gap = (b - a).days - 1
        if gap >= BREAK_MIN_DAYS:
            out.append((a + dt.timedelta(days=1), b - dt.timedelta(days=1), gap))
    if days and (today - days[-1]).days >= BREAK_MIN_DAYS:
        out.append((days[-1] + dt.timedelta(days=1), None, (today - days[-1]).days))
    return out


def _break_text(b: tuple[dt.date, Optional[dt.date], int], today: dt.date) -> str:
    start, end, n = b
    if end is None:
        return i18n.t("coach.break_open", date=_day(start, today), n=n)
    return i18n.t("coach.break", range=_range(start, end, today), n=n)


def _quote(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= NOTE_QUOTE_CHARS else text[: NOTE_QUOTE_CHARS - 1].rstrip() + "…"


def flagged_notes_text(notes: list[tuple[dt.date, str]], today: dt.date, limit: int = NOTES_PER_CARD) -> list[str]:
    """Заметки о боли, сне и болезни, одинаковые — одной строкой «×3 с 09-28»."""
    agg: dict[str, list[Any]] = {}
    for day, text in sorted(notes, key=lambda n: n[0]):
        if not note_is_flagged(text):
            continue
        k = " ".join(text.lower().split())
        if k in agg:
            agg[k][1] += 1
        else:
            agg[k] = [day, 1, _quote(text)]
    items = sorted(agg.values(), key=lambda v: v[0], reverse=True)[:limit]
    items.sort(key=lambda v: v[0])
    return [
        i18n.t("coach.note_repeat", text=q, n=n, date=_day(d, today)) if n > 1
        else i18n.t("coach.note_once", text=q, date=_day(d, today))
        for d, n, q in items
    ]


# ---------- карточка движения ----------


@dataclass
class Card:
    text: str
    verdict: Optional[str]
    sessions_in_window: int
    exercise: str


def _status(verdict: Optional[str]) -> str:
    return i18n.t(f"coach.status.{verdict or 'unknown'}")


def _working_weight(sessions: list[Session]) -> tuple[float, int]:
    """(текущий рабочий вес, индекс первой сессии на нём).

    Рабочий — тяжёлый из последних трёх сессий: один лёгкий день его не
    сбрасывает. «С какой даты» — первая сессия на нём после последней сессии
    тяжелее (прибавка, после которой вес не уходил выше)."""
    w = max(s.top_key for s in sessions[-3:])
    start = len(sessions) - 1
    for i in range(len(sessions) - 1, -1, -1):
        top = sessions[i].top_key
        if top > w:
            break
        if top == w:
            start = i
    return w, start


def movement_card(
    m: Movement,
    *,
    today: dt.date,
    unit: str,
    formula: str,
    rep_range: tuple[int, int],
    breaks: list[tuple[dt.date, Optional[dt.date], int]],
    notes: list[tuple[int, Optional[int], dt.date, str]],
) -> Optional[Card]:
    """Карточка одного движения на языке атлета. None — подходов нет.

    `notes` — (workout_id, exercise_id или None, день, текст): берутся заметки
    к этому упражнению и к тренировкам, где оно было."""
    sessions = [s for s in m.sessions if s.sets]
    if not sessions:
        return None
    mode = m.bodyweight_load
    since_window = today - dt.timedelta(weeks=CARD_WINDOW_WEEKS)
    in_window = [s for s in sessions if s.day >= since_window]
    stats = _stats_sessions(m, formula)
    facts = analytics.stall_facts(stats, today, unit)
    verdict = facts.get("verdict")

    lines: list[str] = []
    group = seed_data.localized_muscle_group_name(m.group, i18n.get_lang()) if m.group else ""
    status = _status(verdict)
    bump = analytics.weight_increase(stats, today)
    if bump is not None and bump["weeks_ago"] <= 4:
        status += i18n.t("coach.status.new_weight")
    lines.append(i18n.t(
        "coach.card.head", name=m.name, group=f" · {group}" if group else "",
        freq=_num(round(len(in_window) / observed_weeks([sessions[0].day], today, CARD_WINDOW_WEEKS), 1)),
        status=status,
    ))
    if facts.get("why"):
        lines.append(i18n.t("coach.card.why", why=facts["why"]))

    # Рабочий вес: повторы только НА нём (best_reps_at), по сессиям.
    w, start = _working_weight(sessions)
    at_w = [s for s in sessions[start:] if s.top_key == w]
    points = [s.best_reps_at(w) for s in at_w]
    series = " → ".join(_set(p, mode) for p in points[-CARD_SERIES_SESSIONS:] if p)
    lo, hi = rep_range
    last_reps = points[-1].reps if points and points[-1] else 0
    advice = "add_weight" if last_reps >= hi else ("below" if last_reps < lo else "add_reps")
    lines.append(i18n.t(
        "coach.card.working", w=_w(w, mode), date=_day(at_w[0].day, today), series=series,
        n=len(at_w), lo=lo, hi=hi, advice=i18n.t(f"coach.advice.{advice}"),
    ))

    # Прошлый вес: повторы на нём до перехода и бэкоффы на нём после.
    before = sessions[:start]
    if before:
        p = before[-1].top_key
        if p != w:
            run_start = len(before) - 1
            for i in range(len(before) - 1, -1, -1):
                if before[i].top_key > p:
                    break
                if before[i].top_key == p:
                    run_start = i
            run = [s.best_reps_at(p) for s in before[run_start:] if s.top_key == p]
            run = [x for x in run if x]
            text = i18n.t(
                "coach.card.before", w=_w(p, mode), first=run[0].reps, last=run[-1].reps, n=len(run),
            )
            backoffs = [
                i18n.t("coach.card.set_on", set=_set(b, mode), date=_day(s.day, today))
                for s in at_w if (b := s.best_reps_at(p)) is not None
            ][-2:]
            if backoffs:
                text += i18n.t("coach.card.backoff", sets=", ".join(backoffs))
            lines.append(text)

    # e1RM за окно — без RPE, как у e1rm_trend и classify_stall.
    scored = []
    for s in sessions:
        if s.day < since_window:
            continue
        best = None
        for x in s.sets:
            v = analytics.e1rm(x.load, x.reps, formula)
            if v > 0 and (best is None or v > best[0]):
                best = (v, x)
        if best:
            scored.append((s, best))
    if scored:
        top_s, (top_v, top_x) = max(scored, key=lambda t: t[1][0])
        last_s, (last_v, last_x) = scored[-1]
        text = i18n.t(
            "coach.card.e1rm", best=_num(round(top_v)), set=_set(top_x, mode), date=_day(top_s.day, today),
            last=_num(round(last_v)), lset=_set(last_x, mode), ldate=_day(last_s.day, today),
        )
        trend = analytics.e1rm_trend(stats, today)
        if trend and trend.get("slope_per_week") is not None:
            text += i18n.t("coach.card.e1rm_slope", slope=f"{trend['slope_per_week']:+g}")
        lines.append(text)
    elif in_window:
        best = max(in_window, key=lambda s: max(x.reps for x in s.sets))
        lines.append(i18n.t(
            "coach.card.best_reps", best=max(x.reps for x in best.sets), date=_day(best.day, today),
            last=max(x.reps for x in in_window[-1].sets), ldate=_day(in_window[-1].day, today),
        ))

    shown = sessions[-CARD_SERIES_SESSIONS:]
    lines.append(i18n.t("coach.card.series", items=" · ".join(
        f"{_day(s.day, today)} {compact_session(s.sets, mode)}" for s in shown
    )))

    context: list[str] = []
    span_from = shown[0].day
    context += [_break_text(b, today) for b in breaks if (b[1] or today) >= span_from]
    ids = {s.workout_id for s in sessions}
    mine = [
        (day, text) for wid, ex_id, day, text in notes
        if (ex_id == m.exercise_id) or (ex_id is None and wid in ids)
    ]
    context += flagged_notes_text(mine, today)
    if context:
        lines.append(i18n.t("coach.card.context", items="; ".join(context)))

    return Card("\n".join(lines), verdict, len(in_window), m.name)


# ---------- загрузка ----------


def observed_weeks(workout_days: list[dt.date], today: dt.date, cap: int) -> float:
    """Сколько недель дневник вообще ведётся, не больше `cap`: частоту «раз в
    неделю за 8 недель» у атлета с трёхнедельной историей иначе делили бы на
    восемь, и три тренировки в неделю читались бы как полторы."""
    if not workout_days:
        return float(cap)
    days = (today - min(workout_days)).days + 1
    return min(float(cap), max(1.0, days / 7))


@dataclass
class CoachData:
    user: Any
    today: dt.date
    unit: str
    formula: str
    rep_range: tuple[int, int]
    workout_days: list[dt.date]
    breaks: list[tuple[dt.date, Optional[dt.date], int]]
    notes: list[tuple[int, Optional[int], dt.date, str]]


async def load_context(user_id: int, *, history_weeks: int = CARD_HISTORY_WEEKS) -> Optional[CoachData]:
    user = await db.get_user(user_id)
    if user is None:
        return None
    today = timeutil.user_today(user)
    since = (today - dt.timedelta(weeks=history_weeks)).isoformat()
    days = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id)]
    window_from = today - dt.timedelta(weeks=history_weeks)
    # Перерыв, начавшийся до окна, тоже перерыв: якорь — последняя тренировка до него.
    anchor = [d for d in days if d < window_from][-1:]
    breaks = find_breaks(anchor + [d for d in days if d >= window_from], today)
    notes = [
        (r["workout_id"], r["exercise_id"], dt.date.fromisoformat(r["started_at"][:10]), r["note"])
        for r in await db.list_notes_for_coach(user_id, since)
    ]
    return CoachData(
        user, today, user["unit"], user["e1rm_formula"], analytics.user_rep_range(user),
        days, breaks, notes,
    )


def card_for(m: Movement, data: CoachData) -> Optional[Card]:
    return movement_card(
        m, today=data.today, unit=data.unit, formula=data.formula, rep_range=data.rep_range,
        breaks=data.breaks, notes=data.notes,
    )


async def cards_by_exercise(user_id: int, data: Optional[CoachData] = None) -> dict[int, Card]:
    """{exercise_id: карточка} по всем неархивным движениям за CARD_HISTORY_WEEKS."""
    data = data or await load_context(user_id)
    if data is None:
        return {}
    since = (data.today - dt.timedelta(weeks=CARD_HISTORY_WEEKS)).isoformat()
    out: dict[int, Card] = {}
    for m in movements_from_rows(await db.list_sets_for_coach(user_id, since)):
        card = card_for(m, data)
        if card is not None:
            out[m.exercise_id] = card
    return out


async def exercise_card(user_id: int, exercise_id: int) -> Optional[str]:
    """Карточка одного упражнения (для get_exercise_progress) — по всей его
    истории, чтобы «до этого» было и у давно не менявшегося веса."""
    data = await load_context(user_id)
    if data is None:
        return None
    ex = await db.get_exercise(exercise_id)
    if ex is None:
        return None
    group = await db.get_muscle_group(ex["primary_group_id"]) if ex["primary_group_id"] else None
    mode = ex["bodyweight_load"] or "none"
    m = Movement(exercise_id, ex["display_name"], ex["original_name"] or None,
                 group["name"] if group else None, mode)
    for r in await db.list_sets_for_exercise(exercise_id):
        if not m.sessions or m.sessions[-1].workout_id != r["workout_id"]:
            m.sessions.append(Session(r["workout_id"], r["started_at"], []))
        m.sessions[-1].sets.append(
            LoggedSet(_key_of(r["weight"], mode), db.load_of(r), r["reps"], r["rpe"])
        )
    card = card_for(m, data)
    return card.text if card else None


# ---------- отчёт о прогрессе ----------


_VERDICT_ORDER = {"dead_end": 0, "regressing": 0}


async def progress_report(user_id: int) -> dict[str, Any]:
    """Пресет «что растёт, что встало»: карточки движений, которые делались за
    последние CARD_WINDOW_WEEKS недель (не больше REPORT_MAX_CARDS; вставшие
    первыми, дальше — что чаще делается), и общий контекст."""
    data = await load_context(user_id)
    if data is None:
        return {"error": "user not found"}
    today = data.today
    cards = [c for c in (await cards_by_exercise(user_id, data)).values() if c.sessions_in_window > 0]
    cards.sort(key=lambda c: (_VERDICT_ORDER.get(c.verdict or "", 1), -c.sessions_in_window, c.exercise))

    def per_week(weeks: int) -> str:
        since = today - dt.timedelta(weeks=weeks)
        n = sum(1 for d in data.workout_days if d > since)
        return _num(round(n / observed_weeks(data.workout_days, today, weeks), 1))

    ctx = [
        i18n.t("coach.report.head", today=today.isoformat(), unit=formatting.unit_label(data.unit),
               lo=data.rep_range[0], hi=data.rep_range[1]),
        i18n.t("coach.report.freq", w4=per_week(4), w8=per_week(CARD_WINDOW_WEEKS)),
    ]
    observed = observed_weeks(data.workout_days, today, CARD_WINDOW_WEEKS)
    if observed < CARD_WINDOW_WEEKS:
        ctx.append(i18n.t("coach.report.short_diary", weeks=_num(round(observed, 1))))
    window_from = today - dt.timedelta(weeks=CARD_WINDOW_WEEKS)
    recent_breaks = [b for b in data.breaks if (b[1] or today) >= window_from]
    ctx.append(
        i18n.t("coach.report.breaks", items="; ".join(_break_text(b, today) for b in recent_breaks))
        if recent_breaks else i18n.t("coach.report.no_breaks")
    )
    logs = [
        r for r in await db.list_bodyweight_logs(user_id)
        if dt.date.fromisoformat(r["logged_at"][:10]) >= window_from
    ]
    u = formatting.unit_label(data.unit)
    if len(logs) >= 2:
        first, last = logs[0], logs[-1]
        ctx.append(i18n.t(
            "coach.report.bodyweight",
            first=f"{_num(first['weight'])}{u}", d1=_day(dt.date.fromisoformat(first["logged_at"][:10]), today),
            last=f"{_num(last['weight'])}{u}", d2=_day(dt.date.fromisoformat(last["logged_at"][:10]), today),
            delta=f"{'+' if last['weight'] >= first['weight'] else '-'}{_num(abs(last['weight'] - first['weight']))}{u}",
        ))
    elif logs:
        ctx.append(i18n.t(
            "coach.report.bodyweight_one", w=f"{_num(logs[0]['weight'])}{u}",
            date=_day(dt.date.fromisoformat(logs[0]["logged_at"][:10]), today),
        ))
    workout_notes = flagged_notes_text(
        [(d, text) for _, ex_id, d, text in data.notes if ex_id is None and d >= window_from], today,
    )
    if workout_notes:
        ctx.append(i18n.t("coach.report.notes", items="; ".join(workout_notes)))

    return {
        "context": "\n".join(ctx),
        "cards": [c.text for c in cards[:REPORT_MAX_CARDS]],
        "not_shown": [c.exercise for c in cards[REPORT_MAX_CARDS:]],
    }


# ---------- отчёт о балансе ----------


async def balance_report(
    user_id: int,
    *,
    target_min: dict[str, int],
    group_min: dict[str, int],
) -> dict[str, Any]:
    """Пресет «слабые места»: что недорабатывается, по истории за BALANCE_WEEKS.

    target_min/group_min — минимумы методики (ai_trainer.PROGRAM_TARGET_MIN_SETS,
    PROGRAM_GROUP_MIN_SETS): одни числа с промптом и проверкой программы."""
    data = await load_context(user_id, history_weeks=12)
    if data is None:
        return {"error": "user not found"}
    today = data.today
    lang = i18n.get_lang()
    start = today - dt.timedelta(days=BALANCE_WEEKS * 7 - 1)
    # Дневнику две недели — делим на две, а не на четыре (см. observed_weeks).
    weeks = round(observed_weeks(data.workout_days, today, BALANCE_WEEKS), 1)

    rows = await db.list_sets_for_coach(user_id, start.isoformat(), include_archived=True)
    group_sets: dict[str, float] = {}
    group_days: dict[str, set[dt.date]] = {}
    target_sets: dict[str, float] = {}
    pattern_sets: dict[str, int] = {}
    for m in movements_from_rows(rows):
        pattern = seed_data.movement_pattern(m.identity or m.name, m.group)
        groups, targets = seed_data.volume_split(m.identity, m.name, m.group)
        for s in m.sessions:
            n = len(working_sets(s.sets))
            if not n:
                continue
            for g, share in groups.items():
                group_sets[g] = group_sets.get(g, 0.0) + n * share
                group_days.setdefault(g, set()).add(s.day)
            for t, share in targets.items():
                target_sets[t] = target_sets.get(t, 0.0) + n * share
            if pattern:
                pattern_sets[pattern] = pattern_sets.get(pattern, 0) + n

    def avg(x: float) -> float:
        return round(x / weeks, 1)

    lines = [i18n.t(
        "coach.balance.head", start=start.isoformat(), end=today.isoformat(), weeks=_num(weeks),
        lo=analytics.WEEKLY_VOLUME_MIN, hi=analytics.WEEKLY_VOLUME_MAX,
    )]
    names = [g["name"] for g in await db.list_muscle_groups(user_id)]
    for g in names:
        total = group_sets.get(g, 0.0)
        if not total and g == seed_data.OTHER_GROUP_NAME:
            continue
        a = avg(total)
        status = analytics.classify_weekly_volume(round(a))
        if status == "high" and g == seed_data.LEGS_GROUP_NAME:
            status = "in_range"  # ноги: несколько крупных мышц, выше 12 — норма
        if total and g in group_min and a < group_min[g]:
            status = "low"
        lines.append(i18n.t(
            "coach.balance.group", group=seed_data.localized_muscle_group_name(g, lang),
            sets=_num(a), freq=_num(avg(len(group_days.get(g, ())))),
            status=i18n.t(f"coach.volume.{status}"),
        ))
        for t in seed_data.MUSCLE_TARGETS_BY_GROUP.get(g, ()) if total else ():
            ta = avg(target_sets.get(t, 0.0))
            if not ta and t in seed_data.OPTIONAL_MUSCLE_TARGETS:
                continue
            mn = target_min.get(t)
            lines.append(i18n.t(
                "coach.balance.target", target=seed_data.localized_muscle_target_name(t, lang), sets=_num(ta),
                min=i18n.t("coach.balance.min", n=mn) if mn else "",
                flag=i18n.t("coach.balance.below_min") if mn and ta < mn else "",
            ))
    lines.append(i18n.t("coach.balance.deadlift_note"))

    lines.append(i18n.t("coach.balance.patterns", items=", ".join(
        f"{i18n.t(f'coach.pattern.{p}')} {_num(avg(pattern_sets.get(p, 0)))}" for p in seed_data.MOVEMENT_PATTERNS
    )))
    missing = [i18n.t(f"coach.pattern.{p}") for p in seed_data.MOVEMENT_PATTERNS if not pattern_sets.get(p)]
    if missing:
        lines.append(i18n.t("coach.balance.missing", items=", ".join(missing)))

    # План против факта: программа, по которой больше всего тренировок за окно.
    prog_rows = await db.list_program_sessions_since(user_id, start.isoformat())
    if prog_rows:
        by_prog: dict[int, list[Any]] = {}
        for r in prog_rows:
            by_prog.setdefault(r["program_id"], []).append(r)
        pid = max(by_prog, key=lambda p: (len(by_prog[p]), by_prog[p][-1]["started_at"]))
        sessions = by_prog[pid]
        days_in = sessions[0]["day_count"]
        per_day: dict[str, int] = {}
        for r in sessions:
            per_day[r["day_name"]] = per_day.get(r["day_name"], 0) + 1
        lines.append(i18n.t(
            "coach.balance.program", name=sessions[0]["program_name"], plan=min(days_in, 7), days=days_in,
            done=_num(avg(len(sessions))),
            items=", ".join(f"{d} ×{n}" for d, n in per_day.items()),
        ))
    else:
        lines.append(i18n.t("coach.balance.no_program"))

    # Что встало — вердикт analytics.stall_facts, не свой.
    since = (today - dt.timedelta(weeks=12)).isoformat()
    stalled = []
    for m in movements_from_rows(await db.list_sets_for_coach(user_id, since)):
        facts = analytics.stall_facts(_stats_sessions(m, data.formula), today, data.unit)
        if facts.get("stalled"):
            stalled.append(f"{m.name} ({_status(facts['verdict'])})")
    lines.append(
        i18n.t("coach.balance.stalled", items="; ".join(stalled)) if stalled
        else i18n.t("coach.balance.no_stalled")
    )
    return {"report": "\n".join(lines)}
