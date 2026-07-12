"""Push notification copy: the 'Привет Атлет' basement-coach voice.

Every category is a pool of interchangeable variants. pick_text() rotates
through a pool per (telegram_id, category) without repeating a variant until
the whole pool has been shown once (a shuffled "bag"), so a user who gets the
same category on two different days doesn't see the same line twice in a row.

Voice rules (see PUSH_IDEAS.md for the full writeup):
  - Every push opens with "ПРИВЕТ АТЛЕТ, " — capitalized, never "боец"/"пользователь".
  - Jabs are reserved for the skip-milestone categories only.
  - Every other category stays supportive.

Skip milestones (3/5/7/10/14 days since the last workout) each get their own
pool rather than one shared pool: the wording references the actual day
count ("неделя простоя", "две недели"), so a day-3 skip must never draw a
day-14 line — hence a dedicated category per milestone instead of one big
"skip" bucket the rotation could hand out regardless of which day fired.
"""

import random

import db

# Category keys, in daily-job priority order (first eligible one wins).
STREAK_AT_RISK = "streak_at_risk"
SKIP_3 = "skip_3"
SKIP_5 = "skip_5"
SKIP_7 = "skip_7"
SKIP_10 = "skip_10"
SKIP_14 = "skip_14"
WIN_BACK = "win_back"
TIMING = "timing"
PLATEAU = "plateau"
WEEKLY_DIGEST = "weekly_digest"

SKIP_MILESTONE_DAYS = (3, 5, 7, 10, 14)
SKIP_CATEGORY_BY_DAY: dict[int, str] = {
    3: SKIP_3,
    5: SKIP_5,
    7: SKIP_7,
    10: SKIP_10,
    14: SKIP_14,
}

TEXTS: dict[str, list[str]] = {
    STREAK_AT_RISK: [
        "ПРИВЕТ АТЛЕТ, серия на кону. {weeks} недель подряд, а на этой — пока ноль. До конца недели {days_left}. Серию строил ты — не разваливай сам.",
        "ПРИВЕТ АТЛЕТ, {weeks} недель без срывов висят на волоске. Одна тренировка — и цепь держится. Выбор за тобой.",
        "ПРИВЕТ АТЛЕТ, сутки на то, чтобы серия не превратилась в «когда-то тренировался». {weeks} недель — не шутка.",
    ],
    SKIP_3: [
        "ПРИВЕТ АТЛЕТ, третий день без зала. Отдых или отмазки — сам разберёшься. Я просто считаю дни.",
        "ПРИВЕТ АТЛЕТ, три дня тишины. Пока не повод для лекций — но я заметил.",
    ],
    SKIP_5: [
        "ПРИВЕТ АТЛЕТ, пятый день без зала. Мышцы ещё помнят, как работать. Привычка — уже забывает.",
        "ПРИВЕТ АТЛЕТ, пять дней тишины подряд. Самое время это прервать, а не привыкать.",
    ],
    SKIP_7: [
        "ПРИВЕТ АТЛЕТ, неделя простоя. Штанга не обидится — она железная. А форма обидится, и быстро.",
        "ПРИВЕТ АТЛЕТ, ровно неделя тишины. Не буду читать нотации — просто напомню, где дверь в зал.",
    ],
    SKIP_10: [
        "ПРИВЕТ АТЛЕТ, десятый день без тренировки. Это уже не пауза, это привычка не приходить. Ломаем её сегодня?",
    ],
    SKIP_14: [
        "ПРИВЕТ АТЛЕТ, две недели. Я не читаю нотации, я считаю дни. Зал на месте. Ты — где?",
        "ПРИВЕТ АТЛЕТ, четырнадцать дней тишины. Хватит уже — приходи, разберёмся на месте.",
    ],
    WIN_BACK: [
        "ПРИВЕТ АТЛЕТ, три недели — и это ок. Возвращаться после паузы умеют все. Не оправдывайся — приходи, сегодня полегче, без геройства.",
        "ПРИВЕТ АТЛЕТ, твой прошлый рекорд всё ещё здесь и ждёт, когда ты придёшь его побить. Он терпеливый.",
        "ПРИВЕТ АТЛЕТ, начни с лёгкого. Первый шаг после паузы — самый ценный. Разомнёмся вместе.",
    ],
    TIMING: [
        "ПРИВЕТ АТЛЕТ, обычно в это время ты уже под грифом. Тело помнит расписание. А ты?",
        "ПРИВЕТ АТЛЕТ, твой рабочий день — сегодня. Ты знаешь, что делать.",
        "ПРИВЕТ АТЛЕТ, час до твоего обычного окна. Успей поесть — и вперёд.",
    ],
    PLATEAU: [
        "ПРИВЕТ АТЛЕТ, {exercise} третью тренировку на одном весе — и каждый раз выше 12 повторов. Это не плато, это ты придерживаешь вес. Добавь.",
        "ПРИВЕТ АТЛЕТ, {exercise}: 12+ повторов три тренировки подряд на одном весе. Тело готово к большему — осталось загрузить.",
        "ПРИВЕТ АТЛЕТ, {exercise} явно стал легче — три сессии за 12 повторов, а вес всё тот же. Скромничаешь?",
    ],
    WEEKLY_DIGEST: [
        "ПРИВЕТ АТЛЕТ, за месяц ты поднял суммарно {tonnage} — это примерно один синий кит. Зайди, покажу разбивку.",
        "ПРИВЕТ АТЛЕТ, на этой неделе — {week_count}. Заходи, гляну, что подросло.",
        "ПРИВЕТ АТЛЕТ, понедельник — твой самый продуктивный день по истории. Держим планку?",
    ],
}

CATEGORY_LABELS: dict[str, str] = {
    STREAK_AT_RISK: "Серия на кону",
    SKIP_3: "Пропуск (3 дня)",
    SKIP_5: "Пропуск (5 дней)",
    SKIP_7: "Пропуск (7 дней)",
    SKIP_10: "Пропуск (10 дней)",
    SKIP_14: "Пропуск (14 дней)",
    WIN_BACK: "Возвращение",
    TIMING: "Тайминг",
    PLATEAU: "Плато",
    WEEKLY_DIGEST: "Аналитика",
}


async def pick_text(telegram_id: int, category: str, **format_kwargs: object) -> str:
    """Return the next non-repeating variant for this user+category, formatted with kwargs.

    Draws from a shuffled bag persisted per (telegram_id, category); refills
    and reshuffles once exhausted so every variant is seen before any repeat.
    """
    pool = TEXTS[category]
    if len(pool) == 1:
        return pool[0].format(**format_kwargs)

    bag = await db.get_rotation_bag(telegram_id, category)
    if not bag:
        bag = list(range(len(pool)))
        random.shuffle(bag)
    index = bag.pop(0)
    await db.save_rotation_bag(telegram_id, category, bag)
    return pool[index].format(**format_kwargs)
