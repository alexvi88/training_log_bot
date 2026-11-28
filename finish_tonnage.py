"""Слайд тоннажа в итогах тренировки — «ЗА ТРЕНИРОВКУ ТЫ ПОДНЯЛ… Это 3
легковушки» (`rewards.tonnage_object` в ответе POST /v1/workouts/{id}/finish).

Предметы — те же, что у шутки «это как N слонов» (`formatting._TONNAGE_OBJECTS`,
названия — `tonnage.object.<id>` с ICU-формами): приложение рисует каждому id
свой пиксельный спрайт.

**Какой период.** Эта тренировка. Исключение одно — тоннаж за всё время
перешёл веху (`lifetime_milestones_up_to`: горбач, пассажирский самолёт, синий
кит, космическая станция, дальше каждые +420т): тогда всё время. Считается
только по записанному у нас (`db.tonnage_totals_before`, без импорта), у
занесения задним числом вехи нет — его вносят не по порядку. Месяца нет: слайд
про то, что человек сделал сегодня.

**От тренировки к тренировке — новый предмет.** Подходящие предметы (тоннаж
покрывает их от 1 до `MAX_COUNT` раз) идут по весу, а выбор — по порядковому
номеру тренировки атлета: следующая тренировка того же тоннажа берёт соседний
предмет, а не тот же. Повторный finish отдаёт то же самое — номер считается «до
этой тренировки» по её id.

**Новые предметы — только той сборке, что их рисует.** Приложение рисует
спрайт по id; старая сборка на незнакомый id показывает блин. Поэтому предметы
из `NEW_IDS` уходят в слайд, только если `X-App-Version` сборки не ниже
`NEW_IDS_MIN_BUILD` (бот и сайт видят их всегда — там текст).

**Только правда.** Количество — целые предметы, которые тоннаж покрывает
полностью (вниз, а не до ближайшего): «Это 3 легковушки» — значит, поднято не
меньше трёх. Подпись под слайдом — настоящие числа периода.
"""

from __future__ import annotations

from typing import Any, Optional

import config
import formatting
import i18n
import lifecycle_events

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


# Предметы, которые приложение рисует только с этой сборки.
NEW_IDS = frozenset({"gorilla", "moose", "bison", "giraffe", "hippo", "rhino", "tractor", "mammoth"})
NEW_IDS_MIN_BUILD = 139


def _objects() -> list[tuple[str, float]]:
    return [(obj_id, float(w)) for _e, w, obj_id in formatting._TONNAGE_OBJECTS]


def client_build(app_version: Optional[str]) -> Optional[int]:
    """Номер сборки из `X-App-Version` («1.0.9 (139)» → 139); None — не разобрать."""
    if not app_version or "(" not in app_version:
        return None
    tail = app_version.rsplit("(", 1)[1].rstrip(") ")
    return int(tail) if tail.isdigit() else None


def drawable_ids() -> frozenset[str]:
    """id, которые клиент этого запроса умеет нарисовать."""
    every = frozenset(obj_id for obj_id, _w in _objects())
    build = client_build(lifecycle_events.app_version())
    if build is not None and build >= NEW_IDS_MIN_BUILD:
        return every
    return every - NEW_IDS


def _times(total_kg: float, w: float) -> int:
    """Сколько предметов в тоннаже — обычным округлением, не вниз: над
    предметом стоит тоннаж, округлённый до десятой тонны, и 2.68т при бизоне
    900кг «Это 2 бизона» читалось как ошибка (2.7 / 0.9 = 3, владелец)."""
    return max(1, int(total_kg / w + 0.5))


def heaviest_fit(total_kg: float) -> Optional[tuple[int, str, int]]:
    """(индекс, id, сколько целых) самого тяжёлого предмета, который тоннаж
    покрывает хотя бы раз; None — легче самого лёгкого."""
    best = None
    for i, (obj_id, w) in enumerate(_objects()):
        if w <= total_kg:
            best = (i, obj_id, _times(total_kg, w))
    return best


def workout_object(
    total_kg: float, seed: int, allowed: Optional[frozenset[str]] = None,
) -> Optional[tuple[str, int]]:
    """Предмет тренировки: из тех, что тоннаж покрывает от 1 до MAX_COUNT раз,
    `seed`-й по весу (порядковый номер тренировки — соседние тренировки берут
    соседние предметы); None — слишком легко. `allowed` — что клиент рисует."""
    if total_kg < MIN_KG:
        return None
    objects = [(obj_id, w) for obj_id, w in _objects() if allowed is None or obj_id in allowed]
    candidates = [(obj_id, _times(total_kg, w)) for obj_id, w in objects if w <= total_kg and _times(total_kg, w) <= MAX_COUNT]
    if not candidates:
        fitting = [(obj_id, _times(total_kg, w)) for obj_id, w in objects if w <= total_kg]
        return fitting[-1] if fitting else None
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

    unit = user["unit"]
    imported = db.is_imported_workout(workout)
    seed = int(workout["id"])
    if celebrate_periods and not imported:
        _w, _s, life_before = await db.tonnage_totals_before(workout["user_id"], workout["id"])
        seed = _w
        life_w, life_s = _w + 1, _s + sets
        before_kg, after_kg = formatting.to_kg(life_before, unit), formatting.to_kg(life_before + tonnage, unit)
        if tonnage > 0 and crossed_lifetime(before_kg, after_kg):
            fit = heaviest_fit(after_kg)
            if fit is not None:
                return slide_json(
                    period="lifetime", total=life_before + tonnage, unit=unit, obj_id=fit[1], count=fit[2],
                    caption=i18n.t("tonnage.slide.caption.period", workouts=life_w, sets=life_s),
                )
    picked = workout_object(formatting.to_kg(tonnage, unit), seed, drawable_ids())
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

