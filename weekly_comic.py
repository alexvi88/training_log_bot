"""«Неделя комиксом» — четыре кадра про реальную неделю атлета на экране
«Итог недели» (поле `comic` в GET /v1/weekly-summary).

Рисует кадры приложение (пиксельные спрайты); сервер решает, КАКИЕ кадры и с
какими подписями. Всё считается из дневника, без модели и без денег.

Кадры 1–3 — дни недели с тренировками, по порядку дат. Дней больше трёх —
берутся самые интересные (рекорд → особая сцена → больше рабочих подходов →
раньше), а на экране они всё равно идут по порядку. Сцена дня:

1. `first` — самая первая тренировка в дневнике атлета;
2. `comeback` — первая тренировка после паузы ≥ COMEBACK_GAP_DAYS;
3. `night` (начало с 22:00 до 04:59 по часам атлета) / `morning` (05:00–06:59);
4. поза по главному упражнению: грудь → bench, ноги → squat, спина → pull,
   плечи → press, руки → curl; у остальных групп — FALLBACK_POSE. Главное
   упражнение — то, где больше рабочих подходов (при равенстве — тоннажа). В
   день с рекордом и поза, и подпись — по рекордному упражнению.

Дней с тренировками меньше трёх — кадры добиваются `skip` (атлет не добрал
до своей нормы — той же, с которой сравнивает вердикт, — и неделя не первая и
не возвращение; не больше одного на комикс) или `rest`. У них `day` — null, а
`label` — день недели, в котором тренировки не было (последние такие дни
недели; у незакрытой недели — только уже прошедшие), а если таких дней не
хватило — «…». Кадр 4 — `week_closed` (норма взята или сравнивать не с чем)
или `week_partial`, подпись — счёт «N из M».

Подписи — варианты `weekly.comic.caption.<вид>.<n>` в каталогах; вариант,
которому не хватает данных, выпадает (тот же weekly_summary.pick_body), выбор
детерминирован от атлета, недели и номера кадра. Строк, вычисленных на
импорте, в модуле нет (CLAUDE.md, «Ловушка, встретившаяся шесть раз»).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Optional

import db
import formatting
import i18n
import seed_data
import weekly_summary

# Сцены, которые умеет рисовать приложение. Не локализуются.
SCENES = (
    "bench", "squat", "press", "curl", "pull", "night", "morning", "comeback",
    "skip", "rest", "first", "week_closed", "week_partial",
)

# Поза по встроенной группе мышц (seed_data.muscle_group_slug).
POSE_BY_GROUP = {
    "chest": "bench",
    "legs": "squat",
    "back": "pull",
    "shoulders": "press",
    "biceps": "curl",
    "triceps": "curl",
}
# Пресс, «Другое», свои группы и упражнения без группы — общая поза со штангой.
FALLBACK_POSE = "bench"

NIGHT_FROM_HOUR = 22
NIGHT_UNTIL_HOUR = 5  # 04:59 — ещё ночь
MORNING_UNTIL_HOUR = 7  # 06:59 — ещё раннее утро

STORY_PANELS = 3
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class SetPick:
    """Подход, который называет подпись: имя — показываемое (display_name)."""

    exercise: str
    weight: float
    reps: int


@dataclass(frozen=True)
class DayStory:
    """Один день с тренировками — сырьё кадра, без текста."""

    day: dt.date
    pose: str
    hour: int  # местный час начала тренировки, которая рассказывает день
    top: Optional[SetPick]  # подпись дня; None — тренировка без подходов
    record: bool = False
    is_first: bool = False
    comeback_weeks: Optional[int] = None
    work_sets: int = 0


@dataclass(frozen=True)
class ComicFacts:
    week_start: dt.date
    stories: list[DayStory]
    this: int  # тренировок за неделю
    base: Optional[int]  # норма: «обычно», иначе прошлая неделя (comparison_base)
    filler_days: list[dt.date]  # дни без тренировок, которые можно подписать
    gentle: bool  # первая неделя или возвращение — без подколки


def scene_of(story: DayStory) -> str:
    if story.is_first:
        return "first"
    if story.comeback_weeks is not None:
        return "comeback"
    if story.hour >= NIGHT_FROM_HOUR or story.hour < NIGHT_UNTIL_HOUR:
        return "night"
    if story.hour < MORNING_UNTIL_HOUR:
        return "morning"
    return story.pose


def _special(story: DayStory) -> bool:
    return scene_of(story) in ("first", "comeback", "night", "morning")


def pick_stories(stories: list[DayStory]) -> list[DayStory]:
    """До трёх дней: рекорд → особая сцена → больше подходов → раньше; на
    экране — по порядку дат."""
    ranked = sorted(
        stories, key=lambda s: (not s.record, not _special(s), -s.work_sets, s.day)
    )
    return sorted(ranked[:STORY_PANELS], key=lambda s: s.day)


def _set_text(pick: SetPick, unit: str) -> str:
    if not pick.weight:
        return i18n.t("weekly.comic.set.bodyweight", reps=pick.reps)
    return i18n.t(
        "weekly.comic.set.weight",
        weight=f"{formatting.format_weight(pick.weight)}{formatting.unit_label(unit)}",
        reps=pick.reps,
    )


def _day_label(day: dt.date) -> str:
    return i18n.t("weekly.comic.label.day", wd=_WEEKDAYS[day.weekday()])


def _story_panel(story: DayStory, unit: str, seed: str) -> dict[str, Any]:
    scene = scene_of(story)
    params: dict[str, Any] = {}
    if story.top is not None:
        params["exercise"] = story.top.exercise
        params["set"] = _set_text(story.top, unit)
    if scene == "first":
        kind = "first"
    elif scene == "comeback":
        kind = "comeback"
        params["weeks"] = story.comeback_weeks
    elif scene in ("night", "morning"):
        kind = f"{scene}_record" if story.record and story.top is not None else scene
    else:
        kind = "record" if story.record and story.top is not None else "plain"
    caption = weekly_summary.pick_body(f"weekly.comic.caption.{kind}.", params, f"{seed}:{kind}")
    return {
        "day": story.day.isoformat(),
        "label": _day_label(story.day),
        "scene": scene,
        "caption": caption,
        "record": story.record,
    }


def _filler_panels(facts: ComicFacts, count: int, seed: str) -> list[tuple[Optional[dt.date], dict]]:
    """Кадры без тренировки: последние подходящие дни недели, `skip` — один и
    последним, только если норма не взята и неделя не первая и не возвращение."""
    days = sorted(facts.filler_days)[-count:] if count else []
    slots: list[Optional[dt.date]] = list(days) + [None] * (count - len(days))
    may_skip = facts.base is not None and facts.this < facts.base and not facts.gentle
    out = []
    for i, day in enumerate(slots):
        kind = "skip" if may_skip and i == len(slots) - 1 else "rest"
        out.append((day, {
            "day": None,
            "label": _day_label(day) if day is not None else i18n.t("weekly.comic.label.none"),
            "scene": kind,
            "caption": weekly_summary.pick_body(
                f"weekly.comic.caption.{kind}.", {}, f"{seed}:filler{i}:{kind}"
            ),
            "record": False,
        }))
    return out


def _final_panel(facts: ComicFacts, seed: str) -> dict[str, Any]:
    base = facts.base if facts.base else None
    closed = base is None or facts.this >= base
    if base is None:
        kind = "free"
    elif facts.this > base:
        kind = "over"
    elif facts.this == base:
        kind = "closed"
    else:
        kind = "partial"
    caption = weekly_summary.pick_body(
        f"weekly.comic.caption.final.{kind}.", {"this": facts.this, "base": base}, f"{seed}:final:{kind}"
    )
    return {
        "day": None,
        "label": i18n.t("weekly.comic.label.total"),
        "scene": "week_closed" if closed else "week_partial",
        "caption": caption,
        "record": False,
    }


def build(facts: ComicFacts, *, unit: str, seed: str) -> Optional[dict[str, Any]]:
    """Комикс из четырёх кадров или None, если за неделю нет ни одной
    тренировки: рассказывать нечего, а выдумывать нельзя."""
    if facts.this <= 0 or not facts.stories:
        return None
    chosen = pick_stories(facts.stories)
    placed: list[tuple[Optional[dt.date], dict]] = [
        (s.day, _story_panel(s, unit, f"{seed}:{s.day.isoformat()}")) for s in chosen
    ]
    placed += _filler_panels(facts, STORY_PANELS - len(chosen), seed)
    # По дням недели; кадр без дня («…») — в конце.
    placed.sort(key=lambda p: (p[0] is None, p[0] or dt.date.max))
    panels = [p for _, p in placed] + [_final_panel(facts, seed)]
    return {"panels": panels}


# ---------- сбор из дневника ----------

def _pose(group_name: Optional[str]) -> str:
    slug = seed_data.muscle_group_slug(group_name) if group_name else None
    return POSE_BY_GROUP.get(slug or "", FALLBACK_POSE)


def _local_hour(started_at: str, tz: int) -> int:
    ts = dt.datetime.fromisoformat(started_at)
    if ts.tzinfo is not None:
        ts = ts.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return (ts + dt.timedelta(hours=tz)).hour


@dataclass
class _Workout:
    id: int
    started_at: str
    day: dt.date
    sets: list[Any]


def _main_exercise(sets: list[Any]) -> Optional[int]:
    """Упражнение с наибольшим числом рабочих подходов, при равенстве — с
    большим тоннажем, затем — начатое раньше."""
    stats: dict[int, list[float]] = {}
    for s in sets:
        st = stats.setdefault(s["exercise_id"], [0, 0.0, s["sid"]])
        st[0] += 1
        st[1] += (s["load_w"] or 0.0) * s["reps"]
    if not stats:
        return None
    return max(stats, key=lambda ex: (stats[ex][0], stats[ex][1], -stats[ex][2]))


def _top_set(sets: list[Any], exercise_id: int) -> Any:
    own = [s for s in sets if s["exercise_id"] == exercise_id]
    return max(own, key=lambda s: (s["weight"] or 0.0, s["reps"], -s["sid"]))


def _pick(row: Any) -> SetPick:
    return SetPick(exercise=row["display_name"], weight=row["weight"] or 0.0, reps=row["reps"])


async def collect(
    user_id: int,
    week_start: dt.date,
    *,
    facts: weekly_summary.WeekFacts,
    records: list[dict[str, Any]],
    this_dates: list[dt.date],
    today: dt.date,
    unit: str,
    formula: str,
    tz: int,
) -> Optional[dict[str, Any]]:
    """Комикс недели по дневнику. `facts`, `records`, `this_dates` — то, что уже
    собрал weekly_summary.collect: норма, рекорды и первая/возвращение здесь
    ровно те же, что у вердикта и списка рекордов на том же экране."""
    if not this_dates:
        return None
    week_end = week_start + dt.timedelta(days=6)
    rows = await db.week_workout_sets(
        user_id, week_start.isoformat(), week_end.isoformat(), formula, tz_offset=tz
    )
    workouts: dict[int, _Workout] = {}
    for r in rows:
        w = workouts.setdefault(
            r["workout_id"], _Workout(r["workout_id"], r["started_at"], dt.date.fromisoformat(r["d"]), [])
        )
        if r["sid"] is not None and r["reps"] and r["reps"] > 0:
            w.sets.append(r)
    if not workouts:
        return None

    # Рекордный подход — тот же, что у списка рекордов: максимум e1RM недели,
    # при ничьей — более ранний. Порядок списка (самый заметный первым) решает,
    # какой рекорд рассказывает день, если их в нём несколько.
    record_at: dict[dt.date, tuple[_Workout, Any]] = {}
    for rec in records:
        best = None
        for w in workouts.values():
            for s in w.sets:
                if s["exercise_id"] != rec["exercise_id"]:
                    continue
                if best is None or (s["e1rm"], -s["sid"]) > (best[1]["e1rm"], -best[1]["sid"]):
                    best = (w, s)
        if best is not None and best[0].day not in record_at:
            record_at[best[0].day] = best

    first_day = min(this_dates)
    by_day: dict[dt.date, list[_Workout]] = {}
    for w in sorted(workouts.values(), key=lambda w: (w.started_at, w.id)):
        by_day.setdefault(w.day, []).append(w)

    stories = []
    for day, day_workouts in by_day.items():
        work_sets = sum(len(w.sets) for w in day_workouts)
        if day in record_at:
            w, s = record_at[day]
            top: Optional[SetPick] = _pick(s)
            pose = _pose(s["group_name"])
        else:
            w = max(day_workouts, key=lambda w: (len(w.sets), -w.id))
            main = _main_exercise(w.sets)
            top_row = _top_set(w.sets, main) if main is not None else None
            top = _pick(top_row) if top_row is not None else None
            pose = _pose(top_row["group_name"]) if top_row is not None else FALLBACK_POSE
        is_first = facts.is_first and day == first_day
        comeback = (
            not facts.is_first and day == first_day and facts.gap_days is not None
            and facts.gap_days >= weekly_summary.COMEBACK_GAP_DAYS
        )
        stories.append(DayStory(
            day=day,
            pose=pose,
            hour=_local_hour(w.started_at, tz),
            top=top,
            record=day in record_at,
            is_first=is_first,
            comeback_weeks=(facts.gap_days // 7) if comeback else None,
            work_sets=work_sets,
        ))

    # Дни без тренировок, которые можно подписать у кадров skip/rest: у
    # незакрытой недели — только уже прошедшие (сегодня ещё можно прийти).
    last = min(week_end, today - dt.timedelta(days=1)) if today <= week_end else week_end
    trained = set(by_day)
    filler_days = [
        week_start + dt.timedelta(days=i) for i in range(7)
        if week_start + dt.timedelta(days=i) <= last and week_start + dt.timedelta(days=i) not in trained
    ]
    comic_facts = ComicFacts(
        week_start=week_start,
        stories=stories,
        this=facts.this,
        base=weekly_summary.comparison_base(facts),
        filler_days=filler_days,
        gentle=facts.is_first or any(s.comeback_weeks is not None for s in stories),
    )
    return build(comic_facts, unit=unit, seed=f"{user_id}:{week_start.isoformat()}")
