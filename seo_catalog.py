"""Каталог бота глазами сайта: снаряд, порог входа, «базовое или
изолирующее», замены и картинка-карточка для ЛЮБОГО упражнения каталога, а
не только для тех, у которых есть своя страница техники (seo_pages.EXERCISES).

Всё выводится из данных бота, ничего не придумывается на странице:

- снаряд — у страниц техники из `seo_library.EX_FACTS` (сверено руками), у
  остальных — по словам в русской идентичности шаблона («гантел», «блок»,
  «штанг»…), плюс короткий список исключений, где слова не говорят
  (`_EQUIP_OVERRIDES`: «Жим ногами» — тренажёр, «Жим Арнольда» — гантели).
  Тест держит, что у каждого показанного упражнения снаряд узнан;
- «базовое» — у упражнения есть паттерн движения бота
  (`seed_data.movement_pattern`: присед, наклон, жимы, тяги), изолирующее —
  паттерна нет;
- замены — семейства `exercise_alternatives.FAMILIES` (то же, что видит
  атлет по кнопке «Альтернативы»), а если их меньше `MIN_SWAPS`, список
  добирается упражнениями той же группы, той же мишени и того же паттерна
  (`similar`), — так же, как человек ищет замену сам: та же мышца, то же
  движение, другой снаряд;
- доля подхода мышце — правилом бота (`seed_data.volume_split`), как на
  страницах мышц.

Карточка с кадром — только у упражнений, у которых есть кадры сайта
(`media/site/ex`, scripts/gen_site_exercise_images.py: для страниц техники —
оба кадра, для остальных показанных — конечный кадр карточкой).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

import exercise_alternatives
import exercise_media
import seed_data
import seo_static

# Снаряд страницы сайта (`/muscles/<мышца>/<снаряд>`) ← снаряд из EX_FACTS.
EQUIPMENT = ("dumbbell", "barbell", "machine", "bodyweight")
_EQUIP_FROM_FACTS = {
    "dumbbells": "dumbbell", "barbell": "barbell", "ez_bar": "barbell", "trap_bar": "barbell",
    "machine": "machine", "cable": "machine", "bodyweight": "bodyweight", "bar": "bodyweight",
    "bars": "bodyweight", "bench": "bodyweight", "wheel": "bodyweight", "kettlebell": "kettlebell",
}
# Где по словам не понять: снаряд прямо.
_EQUIP_OVERRIDES = {
    "Концентрированный подъём на бицепс": "dumbbell", "Концентрированный подъём стоя": "dumbbell",
    "Подъём на бицепс на скамье Скотта": "barbell", "Сгибание рук на бицепс обратным хватом": "barbell",
    "Сгибания Зоттмана": "dumbbell", "Молот на скамье Скотта": "dumbbell", "Жим Арнольда": "dumbbell",
    "Тяга к лицу": "machine", "Тяга каната на заднюю дельту": "machine", "Пулловер": "dumbbell",
    "Жим ногами": "machine", "Жим ногами узкой постановкой": "machine", "Гак-присед": "machine",
    "Болгарские выпады": "dumbbell", "Зашагивания на платформу": "dumbbell", "Подъём на носки стоя": "machine",
    "Подъём на носки сидя": "machine", "Сгибание ног стоя": "machine", "Обратная гиперэкстензия": "machine",
    "Зашагивание с подъёмом колена": "bodyweight", "Подъём на носки «ослик»": "machine", "Жим Палоффа": "machine",
    "Гиперэкстензия": "bodyweight", "Подтягивания с весом": "bodyweight", "Обратные отжимания от скамьи": "bodyweight",
    "Отжимания на брусьях с весом": "bodyweight",
    "Присед с широкой постановкой ног": "barbell", "Присед с узкой постановкой ног": "barbell",
}
_MACHINE_WORDS = ("блок", "кроссовер", "тренажёр", "смит", "хаммер", "рычажн")
_BARBELL_WORDS = ("штанг", "ez", "гриф", "становая", "румынская", "гудморнинг", "хип-траст", "тяга с плинтов",
                  "французский жим", "джей-эм", "тейт", "тяга на прямых ногах")
_BODYWEIGHT_WORDS = ("отжиман", "подтягиван", "без веса", "планк", "скручиван", "подъём ног", "подъём коленей",
                     "петлях", "мостик", "мёртвый", "гиперэкстензия на полу", "складной", "подъём корпуса из",
                     "подъём таза", "подтягивание коленей", "русские повороты", "выпады в ходьбе без")

# Шаблоны сайта (seo_plans): дни из упражнений каталога там, где готовой
# программы в боте нет. Схема — как у программ бота: «4×6–10».
SITE_TEMPLATES: dict[str, list[list[tuple[str, str]]]] = {
    # Пять дней по группам — классический «бро-сплит».
    "bro5": [
        [("Жим штанги лёжа", "4×6–10"), ("Жим гантелей на наклонной скамье", "3×8–12"), ("Жим в тренажёре", "3×10–12"),
         ("Сведение рук в кроссовере", "3×12–15")],
        [("Подтягивания", "4×6–10"), ("Тяга штанги в наклоне", "4×6–10"), ("Тяга верхнего блока", "3×8–12"),
         ("Тяга нижнего блока", "3×10–12"), ("Гиперэкстензия", "3×12–15")],
        [("Присед со штангой", "4×6–10"), ("Румынская тяга", "3×8–10"), ("Жим ногами", "3×10–12"),
         ("Сгибание ног в тренажёре", "3×10–12"), ("Подъём на носки стоя", "4×12–15"), ("Подъём ног в висе", "3×10–15")],
        [("Жим штанги стоя", "4×6–10"), ("Жим гантелей сидя", "3×8–12"), ("Разведение гантелей в стороны", "4×12–15"),
         ("Разведение гантелей в наклоне", "3×12–15"), ("Шраги с гантелями", "3×10–12")],
        [("Подъём штанги на бицепс", "3×8–12"), ("Французский жим", "3×8–12"), ("Подъём гантелей на бицепс молотом", "3×10–12"),
         ("Разгибание на трицепс на блоке", "3×10–15"), ("Подъём гантелей на бицепс на наклонной скамье", "2×10–12"),
         ("Разгибание на трицепс из-за головы на блоке", "2×10–15")],
    ],
    # PHUL: силовой верх и низ, потом объёмный верх и низ.
    "phul": [
        [("Жим штанги лёжа", "4×3–5"), ("Тяга штанги в наклоне", "4×3–5"), ("Жим штанги стоя", "3×5–8"),
         ("Тяга верхнего блока", "3×6–10"), ("Подъём штанги на бицепс", "2×6–10"), ("Жим штанги узким хватом лёжа", "2×6–10")],
        [("Присед со штангой", "4×3–5"), ("Становая тяга", "3×3–5"), ("Жим ногами", "3×10–15"),
         ("Сгибание ног в тренажёре", "3×6–10"), ("Подъём на носки стоя", "4×6–10")],
        [("Жим гантелей на наклонной скамье", "3×8–12"), ("Сведение рук в кроссовере", "3×10–15"), ("Тяга нижнего блока", "3×8–12"),
         ("Тяга гантели в наклоне", "3×8–12"), ("Разведение гантелей в стороны", "3×12–15"),
         ("Подъём гантелей на бицепс на наклонной скамье", "3×10–12"), ("Разгибание на трицепс на блоке с канатом", "3×10–15")],
        [("Фронтальный присед", "3×8–12"), ("Выпады с гантелями", "3×10–12"), ("Разгибание ног в тренажёре", "3×10–15"),
         ("Сгибание ног сидя в тренажёре", "3×10–15"), ("Подъём на носки сидя", "4×10–15"), ("Планка", "3×30–60 сек")],
    ],
    # Плечи и руки за одну тренировку.
    "arms": [
        [("Жим гантелей сидя", "3×8–12"), ("Разведение гантелей в стороны", "3×12–15"), ("Тяга к лицу", "3×12–15"),
         ("Подъём штанги на бицепс", "3×8–12"), ("Разгибание на трицепс на блоке", "3×10–15"),
         ("Подъём гантелей на бицепс молотом", "2×10–12"), ("Французский жим", "2×10–12")],
    ],
}
# Удержание на время — схема в формате бота (seed_data.localized_target
# переводит «сек»).
TIMED_SCHEME = "3×30–60 сек"

# Минимум замен на странице «Чем заменить»: меньше — страница тонкая, её нет.
MIN_SWAPS = 4
MAX_SWAPS = exercise_alternatives.MAX_ALTERNATIVES


# Кэши ниже — по русской идентичности упражнения, без языка: строк для
# показа в них нет, застыть на чужом языке нечему (tests/test_no_frozen_language.py).
@lru_cache(maxsize=None)
def _groups() -> dict[str, str]:
    return {name: group for group, name in reversed(seed_data.EXERCISE_TEMPLATES)}


def group_of(identity: str) -> str:
    return _groups().get(identity, "")


@lru_cache(maxsize=None)
def _page_urls() -> dict[str, str]:
    import seo_pages

    by_slug = {s: url for url, s in seo_pages.EXERCISES}
    return {name: by_slug[slug] for name, slug in exercise_media.EXERCISE_IMAGE_SLUGS.items() if slug in by_slug}


def page_url(identity: str) -> Optional[str]:
    """Адрес страницы техники (`back-squat`) или None — своей страницы нет."""
    return _page_urls().get(identity)


@lru_cache(maxsize=None)
def equipment_of(identity: str) -> Optional[str]:
    """dumbbell | barbell | machine | bodyweight | kettlebell | None."""
    import seo_library

    url = page_url(identity)
    if url and url in seo_library.EX_FACTS:
        return _EQUIP_FROM_FACTS[seo_library.EX_FACTS[url][0]]
    if identity in _EQUIP_OVERRIDES:
        return _EQUIP_OVERRIDES[identity]
    low = identity.lower()
    if any(w in low for w in _MACHINE_WORDS):
        return "machine"
    if "гантел" in low:
        return "dumbbell"
    if "гир" in low:
        return "kettlebell"
    if any(w in low for w in _BARBELL_WORDS):
        return "barbell"
    if (identity in seed_data.BODYWEIGHT_TEMPLATES or identity in seed_data.NO_LOAD_TEMPLATES
            or identity in seed_data.TIMED_TEMPLATES or any(w in low for w in _BODYWEIGHT_WORDS)):
        return "bodyweight"
    return None


def fact_equipment(identity: str) -> Optional[str]:
    """Снаряд подробно (ключ `seo.equip.*`), если упражнение сверено руками."""
    import seo_library

    url = page_url(identity)
    return seo_library.EX_FACTS[url][0] if url and url in seo_library.EX_FACTS else None


def level_of(identity: str) -> Optional[int]:
    """Порог входа 1–3 (seo_library.EX_FACTS) — только у сверенных руками."""
    import seo_library

    url = page_url(identity)
    return seo_library.EX_FACTS[url][1] if url and url in seo_library.EX_FACTS else None


@lru_cache(maxsize=None)
def pattern_of(identity: str) -> Optional[str]:
    return seed_data.movement_pattern(identity, group_of(identity))


@lru_cache(maxsize=None)
def target_of(identity: str) -> Optional[str]:
    return seed_data.muscle_target_for(identity, group_of(identity))


def unilateral(identity: str) -> bool:
    low = identity.lower()
    return any(w in low for w in ("одной рукой", "одной ногой", "на одной ноге", "поочерёдно", "одна рука"))


@lru_cache(maxsize=None)
def share(identity: str, muscle_group: str, muscle_target: Optional[str]) -> float:
    groups, targets = seed_data.volume_split(identity, None, group_of(identity))
    if muscle_target:
        return targets.get(muscle_target, 0.0)
    return groups.get(muscle_group, 0.0)


def has_card_image(identity: str) -> bool:
    slug = exercise_media.EXERCISE_IMAGE_SLUGS.get(identity)
    return bool(slug) and bool(seo_static.site_file(f"ex/{slug}_2-360.webp"))


@lru_cache(maxsize=None)
def card_widths(identity: str) -> tuple[int, ...]:
    """Ширины кадра карточки: у страниц техники 360 и 720, у остальных — 360."""
    slug = exercise_media.EXERCISE_IMAGE_SLUGS[identity]
    return (360, 720) if seo_static.site_file(f"ex/{slug}_2-720.webp") else (360,)


@lru_cache(maxsize=None)
def shown(identity: str) -> bool:
    """Упражнение можно показать на сайте: в каталоге, не скрыто, есть кадры."""
    return (bool(group_of(identity)) and not seed_data.is_catalog_hidden(identity)
            and identity in exercise_media.EXERCISE_IMAGE_SLUGS)


@lru_cache(maxsize=None)
def similar(identity: str) -> tuple[str, ...]:
    """Та же группа, та же мишень и тот же паттерн (у изоляции — тоже без
    паттерна), другое упражнение; по популярности каталога. Группа «Другое»
    (шраги, запястья) — без добора: там разные мышцы под одной вывеской."""
    group = group_of(identity)
    if not group or group == seed_data.OTHER_GROUP_NAME:
        return ()
    target, pattern = target_of(identity), pattern_of(identity)
    out = []
    for g, name in seed_data.EXERCISE_TEMPLATES:
        if name == identity or g != group or not shown(name):
            continue
        if target_of(name) != target or pattern_of(name) != pattern:
            continue
        out.append(name)
    return tuple(sorted(out, key=seed_data.catalog_popularity))


@lru_cache(maxsize=None)
def swaps(identity: str) -> tuple[str, ...]:
    """Замены упражнения: семейство бота, потом добор `similar` до MAX_SWAPS
    (добор — только если семейства мало). Только то, что можно показать."""
    out = [n for n in exercise_alternatives.alternatives_for(identity) if shown(n)]
    if len(out) < MAX_SWAPS:
        for name in similar(identity):
            if name not in out:
                out.append(name)
            if len(out) >= MAX_SWAPS:
                break
    return tuple(out[:MAX_SWAPS])


def card_identities() -> list[str]:
    """Упражнения без своей страницы, которые сайт показывает карточкой
    (замены, мышца × снаряд, дни программ-шаблонов), — им нужен кадр сайта."""
    import seo_pages
    import seo_plans

    paged = {seo_pages._identity_for_media_slug(s) for _u, s in seo_pages.EXERCISES}
    names: list[str] = []
    for _u, slug in seo_pages.EXERCISES:
        names += list(swaps(seo_pages._identity_for_media_slug(slug)))
    names += seo_plans.shown_identities()
    return [n for n in dict.fromkeys(names) if n not in paged and shown(n)]
