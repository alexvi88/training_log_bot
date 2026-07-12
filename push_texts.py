"""Push notification copy: the 'Привет Атлет' basement-coach voice.

Every category is a pool of interchangeable variants. pick_text() rotates
through a pool per (telegram_id, category) without repeating a variant until
the whole pool has been shown once (a shuffled "bag"), so a user who gets the
same category on two different days doesn't see the same line twice in a row.

Voice rules (see PUSH_IDEAS.md for the full writeup):
  - Address is always "АТЛЕТ", capitalized, never "боец"/"пользователь".
  - Jabs are reserved for the SKIP category only.
  - Every other category stays supportive.
"""

import random

import db

# Category keys, in daily-job priority order (first eligible one wins).
STREAK_AT_RISK = "streak_at_risk"
SKIP = "skip"
WIN_BACK = "win_back"
TIMING = "timing"
PLATEAU = "plateau"
WEEKLY_DIGEST = "weekly_digest"
CHALLENGE = "challenge"
FOLLOWUP = "followup"  # transactional, not part of the daily rotation

TEXTS: dict[str, list[str]] = {
    STREAK_AT_RISK: [
        "АТЛЕТ, серия на кону. {weeks} недель подряд, а на этой — пока ноль. До конца недели {days_left}. Серию строил ты — не разваливай сам.",
        "АТЛЕТ, {weeks} недель без срывов висят на волоске. Одна тренировка — и цепь держится. Выбор за тобой.",
        "АТЛЕТ, сутки на то, чтобы серия не превратилась в «когда-то тренировался». {weeks} недель — не шутка.",
    ],
    SKIP: [
        # day 2-3
        "АТЛЕТ, пару дней тишины. Отдых или отмазки — сам разберёшься. Я просто считаю дни.",
        "АТЛЕТ, второй-третий день без зала. Штанга пока не в обиде. Пока.",
        # day 4-6
        "АТЛЕТ, пятый день без зала. Мышцы ещё помнят, как работать. Привычка — уже забывает.",
        "АТЛЕТ, четвёртый-шестой день тишины. Самое время это прервать, а не привыкать.",
        # day 7
        "АТЛЕТ, неделя простоя. Штанга не обидится — она железная. А форма обидится, и быстро.",
        "АТЛЕТ, ровно неделя. Не буду читать нотации — просто напомню, где дверь в зал.",
        # day 10
        "АТЛЕТ, десятый день. Это уже не пауза, это привычка не приходить. Ломаем её сегодня?",
        # day 14
        "АТЛЕТ, две недели. Я не читаю нотации, я считаю дни. Зал на месте. Ты — где?",
        "АТЛЕТ, четырнадцать дней тишины. Хватит уже — приходи, разберёмся на месте.",
    ],
    WIN_BACK: [
        "АТЛЕТ, три недели — и это ок. Возвращаться после паузы умеют все. Не оправдывайся — приходи, сегодня полегче, без геройства.",
        "АТЛЕТ, твой прошлый рекорд всё ещё здесь и ждёт, когда ты придёшь его побить. Он терпеливый.",
        "АТЛЕТ, начни с лёгкого. Первый шаг после паузы — самый ценный. Разомнёмся вместе.",
    ],
    TIMING: [
        "АТЛЕТ, обычно в это время ты уже под грифом. Тело помнит расписание. А ты?",
        "АТЛЕТ, твой рабочий день — сегодня. Ты знаешь, что делать.",
        "АТЛЕТ, час до твоего обычного окна. Успей поесть — и вперёд.",
    ],
    PLATEAU: [
        "АТЛЕТ, {exercise} третью тренировку на одном весе — и каждый раз выше 12 повторов. Это не плато, это ты придерживаешь вес. Добавь.",
        "АТЛЕТ, {exercise}: 12+ повторов три тренировки подряд на одном весе. Тело готово к большему — осталось загрузить.",
        "АТЛЕТ, {exercise} явно стал легче — три сессии за 12 повторов, а вес всё тот же. Скромничаешь?",
    ],
    WEEKLY_DIGEST: [
        "АТЛЕТ, за месяц ты поднял суммарно {tonnage} — это примерно один синий кит. Зайди, покажу разбивку.",
        "АТЛЕТ, на этой неделе — {week_count}. Заходи, гляну, что подросло.",
        "АТЛЕТ, понедельник — твой самый продуктивный день по истории. Держим планку?",
    ],
    CHALLENGE: [
        "АТЛЕТ, задача недели: три тренировки за семь дней. Погнали.",
        "АТЛЕТ, мини-квест: побей любой свой рекорд на этой неделе. Приз — самоуважение. Котируется дорого.",
    ],
    FOLLOWUP: [
        "АТЛЕТ, теперь вода, белок и сон — тренировка кончается не в зале, а на кухне.",
        "АТЛЕТ, работа сделана, но не до конца — без нормального восстановления сегодняшний труд наполовину впустую. Попей воды.",
        "АТЛЕТ, плюс один в журнал. Теперь дай телу то, что оно заслужило: еду, воду и сон.",
    ],
}

CATEGORY_LABELS: dict[str, str] = {
    STREAK_AT_RISK: "Серия на кону",
    SKIP: "Пропуск",
    WIN_BACK: "Возвращение",
    TIMING: "Тайминг",
    PLATEAU: "Плато",
    WEEKLY_DIGEST: "Аналитика",
    CHALLENGE: "Челлендж",
    FOLLOWUP: "После тренировки",
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
