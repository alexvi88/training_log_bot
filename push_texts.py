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
# NEWBIE_NUDGE sits outside that chain: it fires from a separate walk pool
# (users with zero finished workouts, disjoint from the priority chain's pool).
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
# AI-generated weekly digest (text isn't in TEXTS — it's produced per user by the
# model; this key only tags the push for dedup/logging).
AI_WEEKLY = "ai_weekly"
NEWBIE_NUDGE = "newbie_nudge"

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
        "ПРИВЕТ АТЛЕТ, {weeks} недель — это уже расписание, а не случайность. На этой неделе тренировок 0, времени — {days_left}. Закрой серию.",
        "ПРИВЕТ АТЛЕТ, до конца недели {days_left}, а серия в {weeks} недель ещё не закрыта. Одна тренировка решает всё.",
    ],
    SKIP_3: [
        "ПРИВЕТ АТЛЕТ, третий день без зала. Отдых или отмазки — сам разберёшься. Я просто считаю дни.",
        "ПРИВЕТ АТЛЕТ, три дня тишины. Пока не повод для лекций — но я заметил.",
        "ПРИВЕТ АТЛЕТ, три дня без штанги. Не трагедия, но и не режим чемпиона.",
        "ПРИВЕТ АТЛЕТ, третий день пошёл. Зал не переехал — ты просто не зашёл.",
    ],
    SKIP_5: [
        "ПРИВЕТ АТЛЕТ, пятый день без зала. Мышцы ещё помнят, как работать. Привычка — уже забывает.",
        "ПРИВЕТ АТЛЕТ, пять дней тишины подряд. Самое время это прервать, а не привыкать.",
        "ПРИВЕТ АТЛЕТ, пятый день без зала подряд. Ещё немного — и это станет привычкой не ходить.",
        "ПРИВЕТ АТЛЕТ, пять дней — не срыв, но уже тенденция. Останови её сегодня.",
    ],
    SKIP_7: [
        "ПРИВЕТ АТЛЕТ, неделя простоя. Штанга не обидится — она железная. А форма обидится, и быстро.",
        "ПРИВЕТ АТЛЕТ, ровно неделя тишины. Не буду читать нотации — просто напомню, где дверь в зал.",
        "ПРИВЕТ АТЛЕТ, неделя без единого подхода. Форма не ждёт — она уходит первой.",
        "ПРИВЕТ АТЛЕТ, семь дней тишины — ровно неделя. Пора возвращать разговор в зал.",
    ],
    SKIP_10: [
        "ПРИВЕТ АТЛЕТ, десятый день без тренировки. Это уже не пауза, это привычка не приходить. Ломаем её сегодня?",
        "ПРИВЕТ АТЛЕТ, десять дней без зала. Ещё пара — и придётся заново учиться приходить.",
        "ПРИВЕТ АТЛЕТ, десятый день простоя. Я не тороплю — просто напоминаю, что дверь открыта.",
    ],
    SKIP_14: [
        "ПРИВЕТ АТЛЕТ, две недели. Я не читаю нотации, я считаю дни. Зал на месте. Ты — где?",
        "ПРИВЕТ АТЛЕТ, четырнадцать дней тишины. Хватит уже — приходи, разберёмся на месте.",
        "ПРИВЕТ АТЛЕТ, две недели без единой тренировки. Дальше — уже не пауза, а новая привычка. Какая тебе нужна?",
    ],
    WIN_BACK: [
        "ПРИВЕТ АТЛЕТ, три недели — и это ок. Возвращаться после паузы умеют все. Не оправдывайся — приходи, сегодня полегче, без геройства.",
        "ПРИВЕТ АТЛЕТ, твой прошлый рекорд всё ещё здесь и ждёт, когда ты придёшь его побить. Он терпеливый.",
        "ПРИВЕТ АТЛЕТ, начни с лёгкого. Первый шаг после паузы — самый ценный. Разомнёмся вместе.",
        "ПРИВЕТ АТЛЕТ, три недели — долгая пауза, но не приговор. Зайди хоть на 20 минут, без плана.",
        "ПРИВЕТ АТЛЕТ, возвращение не требует геройства. Меньший вес, меньше подходов — главное начать.",
    ],
    TIMING: [
        "ПРИВЕТ АТЛЕТ, обычно в это время ты уже под грифом. Тело помнит расписание. А ты?",
        "ПРИВЕТ АТЛЕТ, твой рабочий день — сегодня. Ты знаешь, что делать.",
        "ПРИВЕТ АТЛЕТ, час до твоего обычного окна. Успей поесть — и вперёд.",
        "ПРИВЕТ АТЛЕТ, это твой обычный час. Тело уже настроилось — не заставляй его ждать зря.",
        "ПРИВЕТ АТЛЕТ, по расписанию — сейчас. Пропускать свой же ритм обидно.",
    ],
    PLATEAU: [
        "ПРИВЕТ АТЛЕТ, {exercise} третью тренировку на одном весе — и каждый раз выше 12 повторов. Это не плато, это ты придерживаешь вес. Добавь.",
        "ПРИВЕТ АТЛЕТ, {exercise}: 12+ повторов три тренировки подряд на одном весе. Тело готово к большему — осталось загрузить.",
        "ПРИВЕТ АТЛЕТ, {exercise} явно стал легче — три сессии за 12 повторов, а вес всё тот же. Скромничаешь?",
        "ПРИВЕТ АТЛЕТ, {exercise} держится на месте три тренировки подряд при 12+ повторах. Значит, вес пора двигать.",
        "ПРИВЕТ АТЛЕТ, три сессии {exercise} с запасом повторов — тело просит нагрузку побольше.",
    ],
    WEEKLY_DIGEST: [
        "ПРИВЕТ АТЛЕТ, за месяц ты поднял суммарно {tonnage} — это примерно один синий кит. Зайди, покажу разбивку.",
        "ПРИВЕТ АТЛЕТ, на этой неделе — {week_count}. Заходи, гляну, что подросло.",
        "ПРИВЕТ АТЛЕТ, понедельник — твой самый продуктивный день по истории. Держим планку?",
        "ПРИВЕТ АТЛЕТ, за 30 дней суммарный тоннаж — {tonnage}. На этой неделе — {week_count}. Разбор — в приложении.",
        "ПРИВЕТ АТЛЕТ, тоннаж за месяц — {tonnage}, а на этой неделе набралось {week_count}. Заходи, посмотри цифры целиком.",
    ],
    NEWBIE_NUDGE: [
        "ПРИВЕТ АТЛЕТ, аккаунт готов, а дневник пока пустой. Первая запись — самая простая: зайди и залогируй хоть один подход.",
        "ПРИВЕТ АТЛЕТ, зарегистрировался — уже полдела. Вторая половина ждёт в зале. Погнали?",
        "ПРИВЕТ АТЛЕТ, я тут, дневник открыт, а тренировок в нём пока ноль. Начни с чего угодно — подсчитаю остальное сам.",
        "ПРИВЕТ АТЛЕТ, дневник ждёт первую запись. Не обязательно идеальную — просто первую.",
        "ПРИВЕТ АТЛЕТ, регистрация есть, тренировки — пока нет. Исправим это сегодня?",
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
    AI_WEEKLY: "AI-дайджест",
    NEWBIE_NUDGE: "Новичок без тренировок",
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
