"""Global presets copied into a user's account on first login.

EXERCISE_TEMPLATES is the read-only pre-pick catalog shown under "📋 Выбрать из
шаблона". Picking a template forks an independent, user-owned copy
(db.fork_exercise_from_template), so the templates themselves are never
referenced by workouts/sets and can be added, renamed, or removed freely. The
catalog is reconciled into the DB on every startup (db._sync_exercise_templates),
so editing this list updates existing deployments, not just fresh databases.

Each group is ordered roughly by popularity — the staple compound lifts first,
then their dumbbell/cable/machine variants, then isolation/accessory work.

## Language

The Russian strings below (group names, exercise names, program text) are not
just text — they're the canonical IDENTITY that keys three other catalogs:
`exercise_media.EXERCISE_IMAGE_SLUGS`, `exercise_descriptions.EXERCISE_DESCRIPTIONS`,
and `exercises.original_name` in the DB (see `exercise_media.catalog_key`'s
docstring for why `original_name`, not the mutable `name`/`display_name`, is
the lookup key). That identity is Russian on every user's account regardless
of their language — translating it would silently break the link to photos
and technique descriptions for anyone who isn't on `ru`.

What a user actually SEES is a different, localized string, resolved through
`localized_*` below at the moment something is shown or forked. English text
lives in `locales/en.json` (100 exercises is 100 keys — completeness is
enforced by `tests/test_catalog_language.py`), keyed off the same
free-exercise-db slug already used for demo photos
(`exercise_media.EXERCISE_IMAGE_SLUGS`) so there's exactly one slug table to
keep in sync, not two. `locales/ru.json` carries the same keys too, mirroring
the Russian strings verbatim — not because `ru` needs a catalog round-trip
(`localized_*` short-circuits to the literal right here for
`lang == i18n.DEFAULT_LANG`, so this file stays the single source of truth
for what a `ru` user sees), but because the repo-wide invariant
(`tests/test_i18n_no_leaks.py::test_catalogs_have_matching_keys`) requires
every product key to exist in both catalogs, so a key can never end up
English-only by accident.
"""

from __future__ import annotations

import exercise_media
import i18n

# (name, emoji, sort_order)
MUSCLE_GROUP_PRESETS = [
    ("Грудь", "💪", 1),
    ("Трицепс", "💪", 2),
    ("Бицепс", "💪", 3),
    ("Плечи", "🤷", 4),
    ("Спина", "🔙", 5),
    ("Ноги", "🦵", 6),
    ("Другое", "🔥", 7),
]

# Locale-catalog slug for each preset group's English name — English's own
# small, hand-written table (only 7 entries, no risk of drifting the way 100
# exercise names would) rather than a locale key derived from the Russian
# name itself, which isn't ASCII-safe.
_MUSCLE_GROUP_SLUGS: dict[str, str] = {
    "Грудь": "chest",
    "Трицепс": "triceps",
    "Бицепс": "biceps",
    "Плечи": "shoulders",
    "Спина": "back",
    "Ноги": "legs",
    "Другое": "other",
}


def localized_muscle_group_name(canonical_name: str, lang: str) -> str:
    """Display name for a muscle group in `lang`.

    Muscle groups are global rows (`muscle_groups.user_id IS NULL`, see
    `db._seed_globals`) shared by every account — unlike exercises, they are
    never forked into a per-user copy, so there is no row to localize at
    creation time. `name` in the DB stays the Russian preset forever; this is
    the only place the English label exists, resolved fresh on every render.
    """
    if lang == i18n.DEFAULT_LANG:
        return canonical_name
    slug = _MUSCLE_GROUP_SLUGS.get(canonical_name)
    if slug is None:
        return canonical_name
    return i18n.t_in(lang, f"muscle_group.{slug}.name")

# Движения, в которых нагрузка — это ты сам (exercises.bodyweight_load).
# {имя шаблона: (режим, доля веса тела)}.
#
# Доли — не выдумка «на глаз», а общепринятые оценки того, какая часть массы
# реально идёт через рабочие мышцы: в подтягиваниях и на брусьях висит всё тело,
# в отжиманиях от пола на руки приходится примерно две трети (стопы держат
# остальное), в отжиманиях узким хватом — столько же.
#
# Списком, а не флагом у каждой строки каталога: EXERCISE_TEMPLATES — это пары
# (группа, имя), и раздувать их до кортежей ради семи упражнений значило бы
# трогать каждую из ста строк.
#
# Здесь намеренно НЕТ планок, скручиваний и подъёмов ног: там либо считается
# время, либо вес тела почти не двигается по вертикали, и приписывать им тоннаж
# значило бы раздувать цифру, а не считать её.
BODYWEIGHT_TEMPLATES: dict[str, tuple[str, float]] = {
    "Подтягивания": ("full", 1.0),
    "Подтягивания обратным хватом": ("full", 1.0),
    "Отжимания на брусьях": ("full", 1.0),
    "Отжимания от пола": ("full", 0.65),
    "Отжимания узким хватом": ("full", 0.65),
    "Гиперэкстензия": ("full", 0.45),
}

# (group_name, exercise_name) — group_name must match a MUSCLE_GROUP_PRESETS name.
EXERCISE_TEMPLATES = [
    # ---------- Грудь ----------
    ("Грудь", "Жим штанги лёжа"),
    ("Грудь", "Жим штанги на наклонной скамье"),
    ("Грудь", "Жим штанги на наклонной скамье вниз головой"),
    ("Грудь", "Жим гантелей лёжа"),
    ("Грудь", "Жим гантелей на наклонной скамье"),
    ("Грудь", "Жим гантелей на наклонной скамье вниз головой"),
    ("Грудь", "Жим в тренажёре"),
    ("Грудь", "Жим в тренажёре Хаммер"),
    ("Грудь", "Отжимания от пола"),
    ("Грудь", "Отжимания на брусьях"),
    ("Грудь", "Сведение рук в кроссовере"),
    ("Грудь", "Сведение рук в тренажёре «бабочка»"),
    ("Грудь", "Разведение гантелей лёжа"),
    ("Грудь", "Разведение гантелей на наклонной скамье"),
    # ---------- Трицепс ----------
    ("Трицепс", "Жим штанги узким хватом лёжа"),
    ("Трицепс", "Французский жим"),
    ("Трицепс", "Французский жим с гантелями"),
    ("Трицепс", "Разгибание на трицепс на блоке"),
    ("Трицепс", "Разгибание на трицепс на блоке с канатом"),
    ("Трицепс", "Разгибание на трицепс на блоке обратным хватом"),
    ("Трицепс", "Разгибание на трицепс из-за головы на блоке"),
    ("Трицепс", "Разгибание на трицепс в кроссовере одной рукой"),
    ("Трицепс", "Разгибание гантели за головой"),
    ("Трицепс", "Разгибание руки с гантелью в наклоне"),
    ("Трицепс", "Отжимания узким хватом"),
    # ---------- Бицепс ----------
    ("Бицепс", "Подъём штанги на бицепс"),
    ("Бицепс", "Подъём EZ-штанги на бицепс"),
    ("Бицепс", "Подъём гантелей на бицепс"),
    ("Бицепс", "Подъём гантелей на бицепс молотом"),
    ("Бицепс", "Подъём гантелей на бицепс на наклонной скамье"),
    ("Бицепс", "Концентрированный подъём на бицепс"),
    ("Бицепс", "Подъём на бицепс на скамье Скотта"),
    ("Бицепс", "Подъём на бицепс в тренажёре"),
    ("Бицепс", "Сгибание рук в блоке"),
    ("Бицепс", "Сгибание рук на бицепс с канатом в кроссовере"),
    ("Бицепс", "Сгибание рук на бицепс обратным хватом"),
    # ---------- Плечи ----------
    ("Плечи", "Жим штанги стоя"),
    ("Плечи", "Жим гантелей сидя"),
    ("Плечи", "Жим гантелей стоя"),
    ("Плечи", "Жим Арнольда"),
    ("Плечи", "Жим в тренажёре на плечи"),
    ("Плечи", "Разведение гантелей в стороны"),
    ("Плечи", "Махи в кроссовере"),
    ("Плечи", "Подъём гантелей перед собой"),
    ("Плечи", "Подъём штанги перед собой"),
    ("Плечи", "Тяга штанги к подбородку"),
    ("Плечи", "Разведение гантелей в наклоне"),
    ("Плечи", "Обратные разведения в тренажёре"),
    # ---------- Спина ----------
    ("Спина", "Подтягивания"),
    ("Спина", "Подтягивания обратным хватом"),
    ("Спина", "Тяга верхнего блока"),
    ("Спина", "Тяга верхнего блока обратным хватом"),
    ("Спина", "Тяга верхнего блока узким хватом"),
    ("Спина", "Тяга штанги в наклоне"),
    ("Спина", "Тяга штанги в наклоне обратным хватом"),
    ("Спина", "Тяга Т-грифа"),
    ("Спина", "Тяга гантели в наклоне"),
    ("Спина", "Тяга гантелей лёжа на наклонной скамье"),
    ("Спина", "Тяга нижнего блока"),
    ("Спина", "Тяга в тренажёре Хаммер"),
    ("Спина", "Становая тяга"),
    ("Спина", "Становая тяга сумо"),
    ("Спина", "Гиперэкстензия"),
    ("Спина", "Пулловер"),
    # ---------- Ноги ----------
    ("Ноги", "Присед со штангой"),
    ("Ноги", "Фронтальный присед"),
    ("Ноги", "Присед в Смите"),
    ("Ноги", "Жим ногами"),
    ("Ноги", "Гак-присед"),
    ("Ноги", "Выпады с гантелями"),
    ("Ноги", "Выпады со штангой"),
    ("Ноги", "Болгарские выпады"),
    ("Ноги", "Зашагивания на платформу"),
    ("Ноги", "Румынская тяга"),
    ("Ноги", "Тяга на прямых ногах"),
    ("Ноги", "Разгибание ног в тренажёре"),
    ("Ноги", "Сгибание ног в тренажёре"),
    ("Ноги", "Сгибание ног сидя в тренажёре"),
    ("Ноги", "Разведение ног в тренажёре"),
    ("Ноги", "Сведение ног в тренажёре"),
    ("Ноги", "Ягодичный мостик"),
    ("Ноги", "Ягодичный мостик со штангой"),
    ("Ноги", "Отведение ноги в кроссовере"),
    ("Ноги", "Подъём на носки стоя"),
    ("Ноги", "Подъём на носки сидя"),
    # ---------- Другое (пресс, кор, предплечья, трапеции) ----------
    ("Другое", "Скручивания"),
    ("Другое", "Скручивания на наклонной скамье"),
    ("Другое", "Скручивания в блоке"),
    ("Другое", "Подъём ног в висе"),
    ("Другое", "Подъём коленей в висе"),
    ("Другое", "Подъём ног лёжа"),
    ("Другое", "Планка"),
    ("Другое", "Боковая планка"),
    ("Другое", "Велосипедные скручивания"),
    ("Другое", "Русские повороты"),
    ("Другое", "Колесо для пресса"),
    ("Другое", "Шраги со штангой"),
    ("Другое", "Шраги с гантелями"),
    ("Другое", "Сгибание запястий со штангой"),
    ("Другое", "Разгибание запястий со штангой"),
]


def localized_exercise_name(canonical_name: str, lang: str) -> str:
    """Display name for an exercise template in `lang`.

    `canonical_name` is always the Russian name from EXERCISE_TEMPLATES — the
    identity `db.fork_exercise_from_template` writes into
    `exercises.original_name` forever, regardless of the forking user's
    language (see the module docstring). For `ru` that string already IS the
    display text; other languages resolve it through the locale catalog,
    keyed by the same free-exercise-db slug already used for demo photos
    (`exercise_media.EXERCISE_IMAGE_SLUGS`) so the two catalogs can't drift
    apart into separate spellings of the same lift.

    Falls back to `canonical_name` for a name with no slug (shouldn't happen
    for anything in EXERCISE_TEMPLATES — tests/test_catalog_language.py
    checks the full 100 — but a user's own hand-typed exercise can reach here
    too via `original_name`, and it has no English translation to offer).
    """
    if lang == i18n.DEFAULT_LANG:
        return canonical_name
    slug = exercise_media.EXERCISE_IMAGE_SLUGS.get(canonical_name)
    if slug is None:
        return canonical_name
    return i18n.t_in(lang, f"exercise.{slug}.name")


# ---------- ready-made workout programs ----------
#
# WORKOUT_PROGRAMS is a read-only catalog of ready-made splits shown under
# "✨ Готовые программы" in the routines screen. Picking one instantiates each
# day as a user-owned routine (db.create_routine_from_program): every exercise
# is resolved to the user's own copy, forking it from the global template
# catalog when the user doesn't have it yet. Because instantiation goes through
# the same routine machinery as "save from last workout", programs need no DB
# rows of their own — they're pure data.
#
# INVARIANT: every exercise name below MUST exist in EXERCISE_TEMPLATES so it
# always resolves to a global template and never ends up dropped. This is
# enforced by tests/test_programs.py.
#
# Each program is a dict:
#   key         — short stable id used in callback data (rt:prog:<key>)
#   name        — button/title text
#   meta        — one-line "who it's for · how often"
#   description — a couple of sentences shown on the detail screen
#   days        — list of (routine_name, [(exercise_name, target), ...]); one routine per day
#
# `target` is the recommended sets×reps for that exercise ("4×6–8"), free-form
# text: it's shown on the program card, saved onto the routine
# (routine_exercises.target) when the program is added, and surfaced again while
# logging that exercise during a workout. It's a recommendation, never a
# constraint — nothing validates the sets a user actually logs against it.
#
# Все программы — зальные: под домашние тренировки без железа каталог не
# рассчитан. Порядок списка — это порядок кнопок в каталоге, и он же порог
# входа: сверху то, для чего хватит двух свободных вечеров и базовых движений,
# снизу — сплиты под режим.
WORKOUT_PROGRAMS = [
    {
        "key": "fullbody2",
        "name": "Всё тело — 2 дня",
        "meta": "новичкам · 2 тренировки в неделю",
        "description": (
            "Для тех, у кого на зал два вечера в неделю. Каждая тренировка "
            "цепляет всё тело, поэтому пропуск одной не оставляет группу без "
            "работы на две недели. Два дня — это мало, но это работает; три "
            "будут лучше, когда время появится."
        ),
        "days": [
            ("Всё тело — A", [
                ("Присед со штангой", "3×6–10"),
                ("Жим штанги лёжа", "3×6–10"),
                ("Тяга верхнего блока", "3×8–12"),
                ("Жим гантелей сидя", "3×8–12"),
                ("Подъём штанги на бицепс", "2×10–12"),
                ("Планка", "3×30–60 сек"),
            ]),
            ("Всё тело — B", [
                ("Румынская тяга", "3×8–10"),
                ("Жим гантелей на наклонной скамье", "3×8–12"),
                ("Тяга штанги в наклоне", "3×8–10"),
                ("Разведение гантелей в стороны", "3×12–15"),
                ("Разгибание на трицепс на блоке", "3×10–15"),
                ("Скручивания", "3×15–20"),
            ]),
        ],
    },
    {
        "key": "fullbody3",
        "name": "🌱 Всё тело — 3 дня",
        "meta": "новичкам · 3 тренировки в неделю",
        "description": (
            "Классика для старта. Три похожие тренировки на всё тело за неделю "
            "(например пн/ср/пт). Базовые движения, минимум изоляции — быстро "
            "ставишь технику и прибавляешь в силе."
        ),
        "days": [
            ("Всё тело — день 1", [
                ("Присед со штангой", "3×5–8"),
                ("Жим штанги лёжа", "3×5–8"),
                ("Тяга штанги в наклоне", "3×6–10"),
                ("Жим штанги стоя", "3×6–10"),
                ("Подъём штанги на бицепс", "2×10–12"),
                ("Планка", "3×30–60 сек"),
            ]),
            ("Всё тело — день 2", [
                ("Становая тяга", "2×5"),
                ("Жим гантелей на наклонной скамье", "3×8–12"),
                ("Тяга верхнего блока", "3×8–12"),
                ("Разведение гантелей в стороны", "3×12–15"),
                ("Разгибание на трицепс на блоке", "3×10–15"),
                ("Скручивания", "3×15–20"),
            ]),
            ("Всё тело — день 3", [
                ("Жим ногами", "3×10–12"),
                ("Отжимания на брусьях", "3×6–10"),
                ("Подтягивания", "3×5–8"),
                ("Тяга нижнего блока", "3×10–12"),
                ("Подъём гантелей на бицепс молотом", "2×10–12"),
                ("Подъём ног в висе", "3×10–15"),
            ]),
        ],
    },
    {
        "key": "strength5x5",
        "name": "🏋️ Сила 5×5 — A/B",
        "meta": "новичкам на силу · 3 тренировки в неделю",
        "description": (
            "Пять подходов по пять, три больших движения за тренировку, "
            "чередуешь день A и день B: A-B-A на одной неделе, B-A-B на "
            "следующей. Изоляции тут нет намеренно — весь смысл в том, чтобы "
            "каждую тренировку добавлять на гриф по 2.5 кг, пока добавляется."
        ),
        "days": [
            ("Сила — день A", [
                ("Присед со штангой", "5×5"),
                ("Жим штанги лёжа", "5×5"),
                ("Тяга штанги в наклоне", "5×5"),
            ]),
            ("Сила — день B", [
                ("Присед со штангой", "5×5"),
                ("Жим штанги стоя", "5×5"),
                ("Становая тяга", "1×5"),
            ]),
        ],
    },
    {
        "key": "upperlower",
        "name": "Верх / Низ — 4 дня",
        "meta": "средний уровень · 2–4 тренировки в неделю",
        "description": (
            "Тело делится на верх и низ, каждый прорабатывается дважды в неделю. "
            "Больше объёма на группу, чем в full body, но восстановиться проще, "
            "чем в сплите на каждый день. Если вечеров всего два — гоняй те же "
            "два дня один раз за неделю."
        ),
        "days": [
            ("Верх", [
                ("Жим штанги лёжа", "4×6–8"),
                ("Тяга штанги в наклоне", "4×6–10"),
                ("Жим гантелей сидя", "3×8–10"),
                ("Тяга верхнего блока", "3×8–12"),
                ("Подъём штанги на бицепс", "3×10–12"),
                ("Разгибание на трицепс на блоке", "3×10–15"),
            ]),
            ("Низ", [
                ("Присед со штангой", "4×6–8"),
                ("Румынская тяга", "3×8–10"),
                ("Жим ногами", "3×10–12"),
                ("Сгибание ног в тренажёре", "3×10–12"),
                ("Подъём на носки стоя", "4×12–15"),
                ("Скручивания", "3×15–20"),
            ]),
        ],
    },
    {
        "key": "ppl",
        "name": "🔁 Толкай / Тяни / Ноги",
        "meta": "средний–продвинутый · 3–6 тренировок в неделю",
        "description": (
            "Push / Pull / Legs. Тренировки бьются по функции: жимовые мышцы, "
            "тянущие мышцы и ноги. Гоняешь по кругу 3 или 6 раз в неделю — "
            "гибко под твой график."
        ),
        "days": [
            ("Толкай", [
                ("Жим штанги лёжа", "4×6–8"),
                ("Жим штанги стоя", "3×8–10"),
                ("Жим гантелей на наклонной скамье", "3×8–12"),
                ("Разведение гантелей в стороны", "3×12–15"),
                ("Разгибание на трицепс на блоке", "3×10–15"),
                ("Французский жим", "3×10–12"),
            ]),
            ("Тяни", [
                ("Становая тяга", "3×5"),
                ("Подтягивания", "4×6–10"),
                ("Тяга штанги в наклоне", "3×8–10"),
                ("Тяга верхнего блока", "3×8–12"),
                ("Подъём штанги на бицепс", "3×10–12"),
                ("Подъём гантелей на бицепс молотом", "3×10–12"),
            ]),
            ("Ноги", [
                ("Присед со штангой", "4×6–8"),
                ("Румынская тяга", "3×8–10"),
                ("Жим ногами", "3×10–12"),
                ("Сгибание ног в тренажёре", "3×10–12"),
                ("Подъём на носки стоя", "4×12–15"),
                ("Подъём ног в висе", "3×10–15"),
            ]),
        ],
    },
    {
        "key": "split3",
        "name": "💪 Сплит на 3 дня",
        "meta": "средний уровень · 3 тренировки в неделю",
        "description": (
            "Бро-сплит: каждая тренировка — своя пара групп. Грудь с трицепсом, "
            "спина с бицепсом, ноги с плечами. Много объёма на целевые мышцы и "
            "неделя на восстановление каждой."
        ),
        "days": [
            ("Грудь + трицепс", [
                ("Жим штанги лёжа", "4×6–10"),
                ("Жим гантелей на наклонной скамье", "3×8–12"),
                ("Сведение рук в кроссовере", "3×12–15"),
                ("Разгибание на трицепс на блоке", "3×10–15"),
                ("Французский жим", "3×10–12"),
            ]),
            ("Спина + бицепс", [
                ("Подтягивания", "4×6–10"),
                ("Тяга штанги в наклоне", "4×6–10"),
                ("Тяга верхнего блока", "3×8–12"),
                ("Подъём штанги на бицепс", "3×10–12"),
                ("Подъём гантелей на бицепс молотом", "3×10–12"),
            ]),
            ("Ноги + плечи", [
                ("Присед со штангой", "4×6–10"),
                ("Румынская тяга", "3×8–10"),
                ("Жим ногами", "3×10–12"),
                ("Жим штанги стоя", "3×8–10"),
                ("Разведение гантелей в стороны", "3×12–15"),
            ]),
        ],
    },
    {
        "key": "glutes3",
        "name": "🦵 Ягодицы и ноги — 3 дня",
        "meta": "низ тела в приоритете · 3 тренировки в неделю",
        "description": (
            "Низ тела получает три дня, верх — минимум, чтобы не отставал. "
            "Ягодицы работают в двух режимах: мостик и отведения бьют их "
            "напрямую, тяга и выпады растягивают под нагрузкой. Верх тут "
            "лёгкий и стоит последним — он не должен съедать силы у ног."
        ),
        "days": [
            ("Ягодицы — тяжёлый день", [
                ("Ягодичный мостик со штангой", "4×8–12"),
                ("Румынская тяга", "4×8–10"),
                ("Болгарские выпады", "3×10–12"),
                ("Отведение ноги в кроссовере", "3×12–15"),
                ("Разведение ног в тренажёре", "3×15–20"),
            ]),
            ("Ноги — квадрицепс", [
                ("Присед со штангой", "4×8–10"),
                ("Жим ногами", "3×10–12"),
                ("Разгибание ног в тренажёре", "3×12–15"),
                ("Сгибание ног в тренажёре", "3×10–12"),
                ("Подъём на носки стоя", "4×15–20"),
            ]),
            ("Ягодицы + верх налегке", [
                ("Выпады с гантелями", "3×12–15"),
                ("Ягодичный мостик", "3×15–20"),
                ("Гиперэкстензия", "3×12–15"),
                ("Тяга верхнего блока", "3×10–12"),
                ("Жим гантелей сидя", "3×10–12"),
                ("Планка", "3×30–60 сек"),
            ]),
        ],
    },
]

# Fast lookup by key for the handlers (callback data carries the key).
PROGRAM_BY_KEY = {p["key"]: p for p in WORKOUT_PROGRAMS}


# ---------- program text localization ----------
#
# Unlike exercises/groups, a program's "key" is already a stable ASCII id
# (it's the same string callback data carries — "fullbody2", "ppl", ...), so
# it doubles as the locale-catalog key with no separate slug table needed.
# `name`/`meta`/`description` are trainer-voice prose (see TONE_OF_VOICE.md
# "## English voice"), not terminology, so — same as exercises/groups — `ru`
# returns the string right here and only other languages go through the
# catalog.


def localized_program_name(key: str, lang: str) -> str:
    program = PROGRAM_BY_KEY.get(key)
    if program is None:
        return key
    if lang == i18n.DEFAULT_LANG:
        return program["name"]
    return i18n.t_in(lang, f"program.{key}.name")


def localized_program_meta(key: str, lang: str) -> str:
    program = PROGRAM_BY_KEY.get(key)
    if program is None:
        return ""
    if lang == i18n.DEFAULT_LANG:
        return program["meta"]
    return i18n.t_in(lang, f"program.{key}.meta")


def localized_program_description(key: str, lang: str) -> str:
    program = PROGRAM_BY_KEY.get(key)
    if program is None:
        return ""
    if lang == i18n.DEFAULT_LANG:
        return program["description"]
    return i18n.t_in(lang, f"program.{key}.description")


def localized_program_day_name(key: str, day_index: int, lang: str) -> str:
    """The routine name for `days[day_index]` — e.g. "Push" for `ppl` day 0.

    Written onto the user's own `routines.name` row the moment the program is
    instantiated (`db.create_routine_from_program`), same as an exercise's
    display name: a snapshot in whatever language was active at creation
    time, never retranslated afterwards.
    """
    program = PROGRAM_BY_KEY.get(key)
    if program is None or not (0 <= day_index < len(program["days"])):
        return ""
    if lang == i18n.DEFAULT_LANG:
        return program["days"][day_index][0]
    return i18n.t_in(lang, f"program.{key}.day.{day_index}.name")
