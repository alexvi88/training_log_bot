"""Слайд тоннажа в итогах тренировки — «ЗА ТРЕНИРОВКУ ТЫ ПОДНЯЛ… Это 3
легковушки» (`rewards.tonnage_object` в ответе POST /v1/workouts/{id}/finish).

Предметы — те же, что у шутки «это как N слонов» (`formatting._TONNAGE_OBJECTS`,
названия — `tonnage.object.<id>` с ICU-формами): приложение рисует каждому id
свой пиксельный спрайт.

**Какой период.** По умолчанию — эта тренировка. Если она перевела тоннаж
календарного месяца через новый предмет (самый тяжёлый подходящий предмет
сменился или его стало больше) — месяц. Если тоннаж за всё время перешёл веху
(`lifetime_milestones_up_to`: горбач, пассажирский самолёт, синий кит, космическая
станция, дальше каждые +420т) — всё время, это главнее всего. Месяц и всё время считаются
только по записанному у нас (`db.tonnage_totals_before`, без импорта), у
занесения задним числом их нет вовсе — его вносят не по порядку.

**Тот же ответ на повтор.** «До этой тренировки» — по id тренировки, а
предмет тренировки выбирается с сидом id: повторный finish отдаёт то же самое.

**Только правда.** Количество — целые предметы, которые тоннаж покрывает
полностью (вниз, а не до ближайшего): «Это 3 легковушки» — значит, поднято не
меньше трёх. Подпись под слайдом — настоящие числа периода.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

import config
import formatting
import i18n

# Легче этого сравнивать не с чем — тот же порог, что у
# formatting.format_tonnage_equivalent.
MIN_KG = 150.0
# Предмет тренировки — из тех, что помещаются от 1 до стольких раз: больше
# десяти спрайтов на слайде уже не пересчитать глазами.
MAX_COUNT = 10
STATION_KG = 420000.0
# Вехи всего времени: каждый предмет от 30т, дальше — каждые +420т после
# космической станции.
_FIXED_MILESTONES = tuple(w for _e, w, _id in formatting._TONNAGE_OBJECTS if w >= 30000)


def lifetime_milestones_up_to(total_kg: float) -> list[float]:
    """Вехи всего времени не выше `total_kg` (по возрастанию)."""
    out = [float(m) for m in _FIXED_MILESTONES if m <= total_kg]
    k = 2
    while STATION_KG * k <= total_kg:
        out.append(STATION_KG * k)
        k += 1
    return out


def crossed_lifetime(before_kg: float, after_kg: float) -> bool:
    return any(m > before_kg for m in lifetime_milestones_up_to(after_kg))


def _objects() -> list[tuple[str, float]]:
    return [(obj_id, float(w)) for _e, w, obj_id in formatting._TONNAGE_OBJECTS]


def heaviest_fit(total_kg: float) -> Optional[tuple[int, str, int]]:
    """(индекс, id, сколько целых) самого тяжёлого предмета, который тоннаж
    покрывает хотя бы раз; None — легче самого лёгкого."""
    best = None
    for i, (obj_id, w) in enumerate(_objects()):
        if w <= total_kg:
            best = (i, obj_id, int(total_kg // w))
    return best


def crossed_month(before_kg: float, after_kg: float) -> bool:
    """Месячный тоннаж перешёл через новый предмет: самый тяжёлый подходящий
    сменился на более тяжёлый или его стало больше. С нуля (первая тренировка
    месяца) — не считается: тогда месяц и есть эта тренировка."""
    if before_kg <= 0:
        return False
    old, new = heaviest_fit(before_kg), heaviest_fit(after_kg)
    if new is None:
        return False
    if old is None:
        return True
    return (new[0], new[2]) > (old[0], old[2])


def workout_object(total_kg: float, seed: int) -> Optional[tuple[str, int]]:
    """Предмет тренировки: из тех, что тоннаж покрывает от 1 до MAX_COUNT раз,
    по сиду (id тренировки); None — слишком легко."""
    if total_kg < MIN_KG:
        return None
    candidates = [(obj_id, int(total_kg // w)) for obj_id, w in _objects() if 1 <= total_kg // w <= MAX_COUNT]
    if not candidates:
        fit = heaviest_fit(total_kg)
        return None if fit is None else (fit[1], fit[2])
    return candidates[seed % len(candidates)]


def _weight_text(kg: float, unit: str) -> str:
    """Вес предмета в единицах атлета, единица вплотную: «30т», «750кг», «66,139lb»."""
    if unit == "lb":
        return formatting.format_lb_tonnage(kg * config.LB_PER_KG)
    if kg >= 1000:
        return f"{formatting.format_weight(round(kg / 1000, 1))}{i18n.t('unit.ton_short')}"
    return f"{formatting.format_weight(kg)}{formatting.unit_label('kg')}"


def _object_weight(obj_id: str) -> float:
    return dict(_objects())[obj_id]


def next_target(total_kg: float) -> Optional[tuple[str, int]]:
    """Следующая цель: первый предмет тяжелее тоннажа; выше космической
    станции — на одну станцию больше."""
    for obj_id, w in _objects():
        if w > total_kg:
            return obj_id, 1
    return "space_station", int(total_kg // STATION_KG) + 1


def slide_json(
    *, period: str, total: float, unit: str, obj_id: str, count: int,
    caption: Optional[str],
) -> dict[str, Any]:
    total_kg = formatting.to_kg(total, unit)
    next_id, next_count = next_target(total_kg)
    one = i18n.t(f"tonnage.object.{obj_id}", n=1)
    return {
        "id": obj_id,
        "count": count,
        "period": period,
        "eyebrow": i18n.t(f"tonnage.slide.eyebrow.{period}"),
        "tonnage_text": formatting.format_tonnage(total, unit),
        "title": i18n.t("tonnage.slide.title", count=count, noun=i18n.t(f"tonnage.object.{obj_id}", n=count)),
        "scale_text": formatting.caps(
            i18n.t("tonnage.slide.scale", noun=one, weight=_weight_text(_object_weight(obj_id), unit))
        ),
        "caption": caption,
        "next_title": i18n.t(
            "tonnage.slide.next", count=next_count,
            noun=i18n.t(f"tonnage.object.{next_id}", n=next_count),
            weight=_weight_text(_object_weight(next_id) * next_count, unit),
        ),
    }


async def tonnage_object_json(
    workout: Any, user: Any, tonnage: float, sets: int, exercises: int, *, celebrate_periods: bool,
) -> Optional[dict[str, Any]]:
    """`rewards.tonnage_object` — под языком атлета (его выставляет вызывающий).

    `tonnage` — тоннаж этой тренировки по нагрузке в единицах атлета, `sets` и
    `exercises` — её подходы и упражнения. `celebrate_periods=False` —
    занесение задним числом: только сама тренировка."""
    import db
    import timeutil

    unit = user["unit"]
    imported = db.is_imported_workout(workout)
    if celebrate_periods and not imported:
        _w, _s, life_before = await db.tonnage_totals_before(workout["user_id"], workout["id"])
        life_w, life_s = _w + 1, _s + sets
        before_kg, after_kg = formatting.to_kg(life_before, unit), formatting.to_kg(life_before + tonnage, unit)
        if tonnage > 0 and crossed_lifetime(before_kg, after_kg):
            fit = heaviest_fit(after_kg)
            if fit is not None:
                return slide_json(
                    period="lifetime", total=life_before + tonnage, unit=unit, obj_id=fit[1], count=fit[2],
                    caption=i18n.t("tonnage.slide.caption.period", workouts=life_w, sets=life_s),
                )
        tz = timeutil.offset_hours(user)
        month = _local_month(workout["started_at"], tz)
        m_w, m_s, m_before = await db.tonnage_totals_before(
            workout["user_id"], workout["id"], month=month, tz_offset=tz,
        )
        before_kg, after_kg = formatting.to_kg(m_before, unit), formatting.to_kg(m_before + tonnage, unit)
        if tonnage > 0 and crossed_month(before_kg, after_kg):
            fit = heaviest_fit(after_kg)
            if fit is not None:
                return slide_json(
                    period="month", total=m_before + tonnage, unit=unit, obj_id=fit[1], count=fit[2],
                    caption=i18n.t("tonnage.slide.caption.period", workouts=m_w + 1, sets=m_s + sets),
                )
    picked = workout_object(formatting.to_kg(tonnage, unit), int(workout["id"]))
    if picked is None:
        return None
    return slide_json(
        period="workout", total=tonnage, unit=unit, obj_id=picked[0], count=picked[1],
        caption=i18n.t("tonnage.slide.caption.workout", exercises=exercises, sets=sets),
    )


def looks_backfilled(workout: Any) -> bool:
    """Закрытая тренировка была занесением задним числом: у него конец равен
    началу, а начало — метка timeutil.backdated_moment (полдень минус
    полчаса на час офсета, ровно :00 или :30 минут). Статус после финиша у
    всех `finished`, поэтому повтор finish узнаёт его так."""
    started, finished = workout["started_at"] or "", workout["finished_at"] or ""
    return started == finished and started[13:19] in (":00:00", ":30:00")


def _local_month(started_at: str, tz_offset: int) -> str:
    """'YYYY-MM' местного месяца тренировки — тот же сдвиг, что db._local_day."""
    moment = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    if moment.tzinfo is not None:
        moment = moment.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return (moment + dt.timedelta(hours=tz_offset)).strftime("%Y-%m")

