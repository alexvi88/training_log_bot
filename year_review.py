"""«Итоги года» — итог года атлета: числа, «личность года» и альбом сторис.

Один календарный год по часам атлета (1 января — 31 декабря, а у текущего
года — по сегодня). Всё считается из тех же законченных тренировок, что
сводка и «Итог недели»; перенесённая импортом история — тоже данные (см.
CLAUDE.md, «Импорт истории»), поэтому атлет, приехавший из Hevy со своими
годами, получает свой год сразу после «Загрузить».

Как у finish_slides, два слоя ради проверок:

* `collect` (async, база) — числа года в `YearFacts`, без единой строки;
* `assemble` (чистая, под `i18n.use_lang`) — колода слайдов, все строки уже
  на языке атлета, и `persona_kind` — правило «личности года» таблицей;
* рендер — синхронный Pillow поверх холста и примитивов finish_slides
  (угольный фон, чипы лампой, крупная цифра, ряд фактов, подпись с App
  Store), уходит в поток.

Правило продукта «утверждение о данных — только когда данные его
подтверждают» здесь держится так: слайд без своих данных не рисуется вовсе
(нет рекордов и роста — нет слайда рекордов; нет привычки по дням и часу —
нет «ритма»), а у «личности года» каждое правило требует своё число.
Подколок нет: итог года — про сделанное.

Короткая история (меньше `FULL_MIN_WORKOUTS` тренировок за год) — честная
короткая форма «Первые недели», а не год из нулей.

В модуле нет строк, вычисленных на импорте (CLAUDE.md, «Ловушка,
встретившаяся шесть раз»): всё через i18n.t внутри функций.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
from collections import Counter
from typing import Any, Optional, Sequence

import analytics
import body_map
import config
import db
import finish_slides as fs
import formatting
import i18n
import seed_data
import timeutil

# Меньше — «Первые недели» вместо года.
FULL_MIN_WORKOUTS = 8
# Упражнение для «январский ты против нынешнего» — хотя бы с этого числа
# тренировок за год, иначе «рост» — это шум двух подходов.
LIFT_MIN_SESSIONS = 4
# Привычный час — только если в него пришлась заметная доля тренировок.
HOUR_MIN_WORKOUTS = 5
HOUR_MIN_SHARE = 0.3
# Групп на слайде «Главные группы года».
TOP_GROUPS = 3

# Правила «личности года» — порядок проверки, первое сработавшее.
PERSONA_KINDS = ("start", "early", "night", "legs", "records", "iron", "regular")
EARLY_BEFORE_HOUR = 9
NIGHT_FROM_HOUR = 21
RECORD_HUNTER_MIN = 10
IRON_STREAK_WEEKS = 8

CAMPAIGN = "year_review"


@dataclasses.dataclass(frozen=True)
class Lift:
    """Самый выросший подъём года: e1RM первого дня против лучшего."""

    exercise: str
    before: float
    after: float

    @property
    def growth_pct(self) -> int:
        return round((self.after - self.before) / self.before * 100) if self.before > 0 else 0


@dataclasses.dataclass(frozen=True)
class Group:
    name: str  # уже на языке атлета
    region: Optional[str]
    sets: int


@dataclasses.dataclass(frozen=True)
class YearFacts:
    """Сырьё итога года — только числа и имена из данных."""

    year: int
    workouts: int
    active_weeks: int
    best_streak: int
    sets: int
    tonnage: float  # в единице аккаунта
    records: int
    top_record: Optional[dict[str, Any]]  # exercise, weight, reps — самый заметный
    lift: Optional[Lift]
    groups: tuple[Group, ...]
    weekday: Optional[int]  # 0 — понедельник; None — привычки нет
    hour: Optional[int]  # местный час; None — привычки нет
    unit: str = "kg"
    female: bool = False

    @property
    def short(self) -> bool:
        return self.workouts < FULL_MIN_WORKOUTS


# ---------- сбор ----------


def year_window(year: int, today: dt.date) -> tuple[dt.date, dt.date]:
    """1 января — 31 декабря, у текущего года — по сегодня."""
    start = dt.date(year, 1, 1)
    end = min(dt.date(year, 12, 31), today)
    return start, end


def usual_hour(hours: Sequence[int]) -> Optional[int]:
    """Привычный час или None: «обычно к 19:00» — утверждение о привычке,
    и у разбросанных по дню тренировок его нет."""
    if len(hours) < HOUR_MIN_WORKOUTS:
        return None
    hour, count = sorted(Counter(hours).items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return hour if count / len(hours) >= HOUR_MIN_SHARE else None


async def _top_lift(
    user_id: int, start: dt.date, end: dt.date, formula: str, tz: int
) -> Optional[Lift]:
    best: Optional[tuple[float, Lift]] = None
    for row in await db.top_exercises_by_frequency(
        user_id, start.isoformat(), end.isoformat(), limit=None,
        min_sessions=LIFT_MIN_SESSIONS, tz_offset=tz,
    ):
        before, after = await db.exercise_e1rm_first_and_best(
            user_id, row["id"], start.isoformat(), end.isoformat(), formula, tz_offset=tz
        )
        if before <= 0 or after <= before:
            continue
        lift = Lift(exercise=row["display_name"], before=before, after=after)
        if lift.growth_pct < 1:
            continue
        ratio = (after - before) / before
        if best is None or ratio > best[0]:
            best = (ratio, lift)
    return best[1] if best else None


async def _groups(user_id: int, start: dt.date, end: dt.date, tz: int, lang: str) -> tuple[Group, ...]:
    counts = await db.weekly_volume_by_group(user_id, start.isoformat(), end.isoformat(), tz_offset=tz)
    names = {g["id"]: g["name"] for g in await db.list_muscle_groups(user_id)}
    out: list[Group] = []
    for gid, sets in counts.items():
        raw = names.get(gid)
        if not raw or not sets or raw.strip().lower() in formatting.VOLUME_HIDDEN_GROUPS:
            continue
        canonical = seed_data.canonical_muscle_group_name(raw) or raw
        out.append(Group(
            name=seed_data.localized_muscle_group_name(canonical, lang),
            region=body_map.group_key(raw),
            sets=sets,
        ))
    out.sort(key=lambda g: (-g.sets, g.name))
    return tuple(out[:TOP_GROUPS])


async def collect(user_id: int, year: Optional[int] = None, user: Any = None) -> Optional[YearFacts]:
    """Числа года или None — законченных тренировок в этом году нет."""
    if user is None:
        user = await db.get_user(user_id)
    if user is None:
        return None
    today = timeutil.user_today(user)
    year = year or today.year
    start, end = year_window(year, today)
    if end < start:
        return None
    tz = timeutil.offset_hours(user)
    unit = user["unit"]
    formula = user["e1rm_formula"]
    dates = [
        d for d in (dt.date.fromisoformat(x) for x in await db.list_finished_workout_dates(user_id, tz_offset=tz))
        if start <= d <= end
    ]
    if not dates:
        return None
    tonnage = sum((await db.daily_tonnage(user_id, start.isoformat(), end.isoformat(), tz_offset=tz)).values())
    sets = sum(
        (await db.weekly_volume_by_group(user_id, start.isoformat(), end.isoformat(), tz_offset=tz)).values()
    )
    # Рекорд года — выше всего, что было ДО 1 января. У того, кто пришёл в
    # этом году, «до» нет, и рекордов нет: первая попытка движения — не рекорд.
    records = await db.e1rm_records_in_window(user_id, start.isoformat(), end.isoformat(), formula, tz_offset=tz)
    top_record = None
    if records:
        row = records[0]
        top_record = {"exercise": row["display_name"], "weight": row["weight"], "reps": row["reps"]}
    hours = await db.list_finished_workout_local_hours(user_id, start.isoformat(), end.isoformat(), tz_offset=tz)
    lang = user["lang"] or i18n.DEFAULT_LANG
    return YearFacts(
        year=year,
        workouts=len(dates),
        active_weeks=len({d - dt.timedelta(days=d.weekday()) for d in dates}),
        best_streak=analytics.max_week_streak(dates),
        sets=sets,
        tonnage=tonnage or 0.0,
        records=len(records),
        top_record=top_record,
        lift=await _top_lift(user_id, start, end, formula, tz),
        groups=await _groups(user_id, start, end, tz, lang),
        weekday=analytics.most_frequent_weekday(dates),
        hour=usual_hour(hours),
        unit=unit,
        female=fs._field(user, "gender") == "female",
    )


# ---------- «личность года» ----------


def persona_kind(facts: YearFacts) -> str:
    """Первое правило из PERSONA_KINDS, которое подтверждают данные."""
    if facts.short:
        return "start"
    if facts.hour is not None and facts.hour < EARLY_BEFORE_HOUR:
        return "early"
    if facts.hour is not None and facts.hour >= NIGHT_FROM_HOUR:
        return "night"
    if facts.groups and facts.groups[0].region == "legs":
        return "legs"
    if facts.records >= RECORD_HUNTER_MIN:
        return "records"
    if facts.best_streak >= IRON_STREAK_WEEKS:
        return "iron"
    return "regular"


def _hour_text(hour: int) -> str:
    return f"{hour:02d}:00"


@dataclasses.dataclass(frozen=True)
class Persona:
    kind: str
    title: str
    body: str


def persona(facts: YearFacts) -> Persona:
    kind = persona_kind(facts)
    params: dict[str, Any] = {
        "workouts": facts.workouts, "weeks": facts.active_weeks, "streak": facts.best_streak,
        "records": facts.records, "hour": _hour_text(facts.hour) if facts.hour is not None else "",
        "group": facts.groups[0].name if facts.groups else "",
        "gender": "female" if facts.female else "male",
    }
    return Persona(
        kind=kind,
        title=i18n.t(f"year.persona.{kind}.title", **params),
        body=i18n.t(f"year.persona.{kind}.body", **params),
    )


# ---------- колода ----------


@dataclasses.dataclass(frozen=True)
class StatSlide:
    """Слайд «чип — цифра — строка тренера — факты». Без цифры (`value`
    пуст) главным идёт `headline` крупными буквами — финальная «личность»."""

    chip: str
    day: str
    value: str
    unit: str
    line: str
    facts: tuple[fs.Fact, ...] = ()
    kicker: str = ""
    headline: str = ""
    seed: str = "year"


Slide = StatSlide | fs.BarbellSlide | fs.BodySlide


@dataclasses.dataclass(frozen=True)
class Deck:
    slides: tuple[Slide, ...]
    signature: fs.Signature
    lang: str
    persona: Persona


def tonnage_text(facts: YearFacts) -> str:
    return formatting.format_tonnage(facts.tonnage, facts.unit) if facts.tonnage > 0 else ""


def _weight(weight: float, unit: str) -> str:
    return f"{formatting.format_weight(weight)}{formatting.unit_label(unit)}"


def _cover(facts: YearFacts, day: str) -> StatSlide:
    fact_list = [
        fs.Fact(str(facts.active_weeks), i18n.t("year.fact.active_weeks", n=facts.active_weeks)),
    ]
    if facts.best_streak > 1:
        fact_list.append(fs.Fact(str(facts.best_streak), i18n.t("year.fact.streak", n=facts.best_streak)))
    if facts.sets:
        fact_list.append(fs.Fact(str(facts.sets), i18n.t("finish_slides.fact.sets", n=facts.sets)))
    return StatSlide(
        chip=i18n.t("year.chip.short" if facts.short else "year.chip.cover"), day=day,
        kicker=i18n.t("weekly.greeting"),
        value=str(facts.workouts), unit=i18n.t("year.cover.unit", n=facts.workouts),
        line=i18n.t("year.cover.line_short" if facts.short else "year.cover.line"),
        facts=tuple(fact_list), seed=f"cover{facts.year}",
    )


def _barbell(facts: YearFacts, day: str) -> Optional[fs.BarbellSlide]:
    text = tonnage_text(facts)
    if not text:
        return None
    value, unit_word = fs.split_number(text)
    is_lb = facts.unit == "lb"
    fact_list = [fs.Fact(str(facts.workouts), i18n.t("year.cover.unit", n=facts.workouts))]
    if facts.sets:
        fact_list.append(fs.Fact(str(facts.sets), i18n.t("finish_slides.fact.sets", n=facts.sets)))
    if facts.records:
        fact_list.append(fs.Fact(str(facts.records), i18n.t("year.records.unit", n=facts.records)))
    return fs.BarbellSlide(
        day=day, title=i18n.t("year.barbell.title"), value=value, unit=unit_word,
        lifted=i18n.t("year.barbell.lifted_female" if facts.female else "year.barbell.lifted"),
        facts=tuple(fact_list), kilograms=formatting.to_kg(facts.tonnage, facts.unit),
        plate_weight=100 if is_lb else 50,
        plate_mark="LB" if is_lb else "KG",  # l10n: маркировка на железном блине
        units_per_pair=200.0 if is_lb else 100.0,
        units_per_kg=config.LB_PER_KG if is_lb else 1.0,
    )


def _records(facts: YearFacts, day: str) -> Optional[StatSlide]:
    lift_fact = ()
    if facts.lift is not None:
        lift_fact = (fs.Fact(
            f"{formatting.format_weight(round(facts.lift.before))} → {_weight(round(facts.lift.after), facts.unit)}",
            facts.lift.exercise, gain=True,
        ),)
    if facts.records and facts.top_record:
        top = facts.top_record
        line = i18n.t(
            "year.records.top", exercise=top["exercise"],
            set=i18n.t("weekly.record.best", weight=_weight(top["weight"], facts.unit), reps=top["reps"]),
        )
        return StatSlide(
            chip=i18n.t("year.chip.records"), day=day, value=str(facts.records),
            unit=i18n.t("year.records.unit", n=facts.records), line=line, facts=lift_fact,
            seed=f"records{facts.year}",
        )
    if facts.lift is not None:
        return StatSlide(
            chip=i18n.t("year.chip.growth"), day=day, value=f"+{facts.lift.growth_pct}%", unit="",
            line=i18n.t("year.growth.line", exercise=facts.lift.exercise), facts=lift_fact,
            seed=f"growth{facts.year}",
        )
    return None


def _body(facts: YearFacts, day: str) -> Optional[fs.BodySlide]:
    drawn = [g for g in facts.groups if g.region is not None]
    figure = body_map.figure((None, g.region) for g in drawn)
    if figure is None:
        return None
    return fs.BodySlide(
        chip=i18n.t("year.chip.body"), day=day, groups=tuple(g.name for g in drawn), figure=figure,
        facts=tuple(fs.Fact(str(g.sets), g.name) for g in drawn),
    )


def _rhythm(facts: YearFacts, day: str) -> Optional[StatSlide]:
    if facts.weekday is None and facts.hour is None:
        return None
    wd = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    if facts.weekday is not None:
        value = i18n.t("date.weekday_short", wd=wd[facts.weekday])
        line = i18n.t("year.rhythm.weekday", weekday=wd[facts.weekday])
    else:
        value = _hour_text(facts.hour)
        line = i18n.t("year.rhythm.hour_only")
    fact_list = []
    if facts.weekday is not None and facts.hour is not None:
        fact_list.append(fs.Fact(_hour_text(facts.hour), i18n.t("year.fact.hour")))
    fact_list.append(fs.Fact(str(facts.active_weeks), i18n.t("year.fact.active_weeks", n=facts.active_weeks)))
    if facts.best_streak > 1:
        fact_list.append(fs.Fact(str(facts.best_streak), i18n.t("year.fact.streak", n=facts.best_streak)))
    return StatSlide(
        chip=i18n.t("year.chip.rhythm"), day=day, value=value, unit="", line=line,
        facts=tuple(fact_list), seed=f"rhythm{facts.year}",
    )


def _final(facts: YearFacts, day: str, who: Persona) -> StatSlide:
    fact_list = [fs.Fact(str(facts.workouts), i18n.t("year.cover.unit", n=facts.workouts))]
    text = tonnage_text(facts)
    if text:
        fact_list.append(fs.Fact(text, i18n.t("finish_slides.fact.tonnage")))
    if facts.records:
        fact_list.append(fs.Fact(str(facts.records), i18n.t("year.records.unit", n=facts.records)))
    return StatSlide(
        chip=i18n.t("year.chip.final"), day=day, value="", unit="", headline=who.title,
        line=who.body, facts=tuple(fact_list), seed=f"final{facts.year}",
    )


def assemble(facts: YearFacts) -> Deck:
    """Колода на текущем языке. Порядок: обложка → штанга → рекорды или рост
    → главные группы → ритм → «личность года». Без данных — без слайда."""
    day = str(facts.year)
    who = persona(facts)
    slides: list[Optional[Slide]] = [
        _cover(facts, day),
        _barbell(facts, day),
        _records(facts, day),
        _body(facts, day),
        _rhythm(facts, day),
        _final(facts, day, who),
    ]
    return Deck(
        slides=tuple(s for s in slides if s is not None), signature=fs.current_signature(),
        lang=i18n.get_lang(), persona=who,
    )


def slide_texts(slide: Slide, signature: fs.Signature) -> list[str]:
    """Все строки слайда, которые уезжают в PNG, — для проверок языка и глифов."""
    if not isinstance(slide, StatSlide):
        return fs.slide_texts(slide, signature)
    texts = [signature.brand, signature.tagline, signature.store_small, signature.store_big,
             slide.chip, slide.day, slide.kicker, slide.value, slide.unit, slide.line, slide.headline]
    for fact in slide.facts:
        texts += [fact.value, fact.label]
    return [text for text in texts if text]


# ---------- рендер ----------


def render_stat(slide: StatSlide, signature: fs.Signature, lang: str, icon: Optional[str] = None) -> bytes:
    canvas = fs._Canvas(lang)
    facts_h = fs._facts_height(canvas) if slide.facts else 0
    # Кирпич подвала с лампой сверху, уход в уголь к тексту.
    wall_bottom = 300.0
    fs._brick(canvas, 0, 0, fs.CARD_W, wall_bottom, slide.seed)
    fs._lamp(canvas)
    canvas.vgradient(0, 120, fs.CARD_W, wall_bottom, [
        (0, fs._rgba(fs.COAL, 0)), (0.7, fs._rgba(fs.COAL, 0.9)), (1, fs._rgba(fs.COAL, 1)),
    ])
    fs._chips(canvas, slide.chip, slide.day)
    width = fs.CARD_W - 32
    y = 200.0
    if slide.kicker:
        kicker = fs._upper(slide.kicker, lang)
        size = fs._fit_size(canvas, kicker, 15, width, minimum=10, kerning=1.2)
        canvas.shadowed_text(16, y + canvas.metrics(size)[0], kicker, size, fs.LAMP, kerning=1.2, radius=4, dy=1)
        y += canvas.line_height(15) + 4
    if slide.value:
        asc, _ = canvas.metrics(96)
        fs._big_number(canvas, 16, y + asc, slide.value, slide.unit, size=96, shadow=True)
        y += canvas.line_height(96) + 4
    elif slide.headline:
        headline = fs._upper(slide.headline, lang)
        size = 34.0
        lines = fs._wrap(canvas, headline, size, width, 3)
        while size > 22 and any(canvas.text_width(line, size) > width for line in lines):
            size -= 1
            lines = fs._wrap(canvas, headline, size, width, 3)
        for line in lines:
            canvas.shadowed_text(16, y + canvas.metrics(size)[0], line, size, fs.WHITE)
            y += canvas.line_height(size)
        y += 10
    if slide.line:
        size = 15.0
        room = fs.CONTENT_H - facts_h - 14 - y
        for line in fs._wrap(canvas, slide.line, size, width, 4):
            if canvas.line_height(size) > room:
                break
            canvas.text(16, y + canvas.metrics(size)[0], line, size, fs._rgba(fs.CHALK, 0.85))
            y += canvas.line_height(size) + 2
            room -= canvas.line_height(size) + 2
    if slide.facts:
        fs._facts_row(canvas, slide.facts, fs.CONTENT_H - facts_h)
    fs._signature(canvas, signature, icon)
    return canvas.finish()


def render_slide(slide: Slide, signature: fs.Signature, lang: str) -> bytes:
    icon = fs.app_icon()
    if isinstance(slide, StatSlide):
        return render_stat(slide, signature, lang, icon)
    return fs.render_slide(slide, signature, lang)


def render_deck(deck: Deck) -> list[bytes]:
    return [render_slide(slide, deck.signature, deck.lang) for slide in deck.slides]


async def build(user_id: int, year: Optional[int] = None, user: Any = None) -> tuple[Optional[Deck], list[bytes]]:
    """Колода и PNG по порядку; (None, []) — тренировок за год нет."""
    if user is None:
        user = await db.get_user(user_id)
    facts = await collect(user_id, year, user)
    if facts is None:
        return None, []
    with i18n.use_lang(user["lang"] or i18n.DEFAULT_LANG):
        deck = assemble(facts)
    return deck, await asyncio.to_thread(render_deck, deck)


def share_url(user_id: int) -> Optional[str]:
    """Ссылка под итогом: App Store с кампанией `ct=year_review`, без него —
    None (бот подставит свою реферальную)."""
    store = config.app_store_url()
    if not store:
        return None
    import acquisition

    return acquisition.with_campaign_token(store, CAMPAIGN)
