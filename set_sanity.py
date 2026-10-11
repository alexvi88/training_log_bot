"""Подозрительный подход: проверки, после которых тренер переспрашивает ДО записи.

Одна копия логики на бота (`handlers/workout._weight_confirm_prompt`) и на
приложение (`api_v1.log_sets_from_text` с `confirm_suspicious`). Раньше всё это
жило в handlers/workout.py, а `/v1` не спрашивал вовсе: «8 60» молча
становилась 8 кг × 60 повторов и вечным ложным рекордом. Две копии одних
порогов разъехались бы на первой же правке — поэтому вынесено сюда, а бот
зовёт те же функции, что и раньше, через свои прежние имена.

Модуль чистый: ни базы, ни FSM, ни Telegram. Что считать «прошлым разом» и
«уже сегодня» — решает вызывающий (бот берёт из FSM, `/v1` — из базы).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import formatting
import i18n

# Below this fraction (or above this multiple) of the heaviest set this exercise
# has seen — last session или уже сегодня, — a just-logged weight is flagged as a
# likely typo rather than a deliberate change.
#
# Верхний порог был 3.0 и пропускал ровно тот случай, ради которого всё и
# делалось: восемь подходов по 200, потом «500 5» — вопроса не последовало, и
# 500 кг молча стали вечным рекордом упражнения. Прибавка в полтора раза за один
# подход настоящей тренировкой почти не бывает; цена ложного срабатывания — один
# тап «Да», цена пропуска — рекорд, тоннаж и ачивки, которых уже не отобрать
# (см. handlers.workout._weight_confirm_prompt).
#
# Нижний порог, наоборот, ослаблен с 0.4 до 0.15 — из-за разминки. Кто пишет
# разминочные подходы в тот же список (а таких много), на 60кг перед рабочими
# 200 получал «60кг? в прошлый раз 210кг» — вопрос на ровном месте, каждую
# тренировку. Настоящая опечатка в эту сторону — потерянный разряд («14» вместо
# «140», «1 1» вместо «140 6»), то есть десятая часть веса и меньше; 0.15 ловит
# весь этот класс и молчит на разминке, откате и лёгком добивочном подходе.
SUSPICIOUS_WEIGHT_LOW_FRACTION = 0.15
SUSPICIOUS_WEIGHT_HIGH_MULTIPLE = 1.5

# Выше этого числа повторов под весом тренер переспрашивает. Тридцать — это уже
# сильно за пределами силовой работы, а вот типовые промахи ввода попадают сюда
# все: «сто пятьдесят» без повторов (голос слышит одно число и берёт вес с
# прошлого подхода — 100×150), «три подхода по сто на восемь» (3×100), «восемь по
# сто» (8×100). Своим весом не ограничиваем: 50 отжиманий — обычное дело, а веса
# там нет.
REPS_CONFIRM_ABOVE = 30

KIND_WEIGHT = "weight"
KIND_REPS = "reps"


def working_weight_today(today_sets: list[tuple[float, int]]) -> float:
    """Самый тяжёлый вес, который сегодня в этом упражнении встретился минимум
    дважды, — то есть рабочий, а не ступенька разминки.

    Одного подхода мало: разминка на новом упражнении выглядит как 60, потом
    200, и если считать планкой первую же цифру, рабочий подход сам окажется
    «подозрительно тяжёлым» — тренер спросит про нормальный вес ровно там, где
    сравнивать ещё не с чем. Повторённый вес — это уже заявление о том, с чем
    человек сегодня работает.
    """
    counts = Counter(w for w, _r in today_sets if w > 0)
    return max((w for w, times in counts.items() if times >= 2), default=0.0)


def suspicious_weight_warning(
    last_session: list[tuple[float, int, float | None]] | None,
    today_sets: list[tuple[float, int]] | None,
    unit: str = "kg",
) -> str | None:
    """A soft nudge — never blocks logging, unlike parser.MAX_WEIGHT's hard
    ceiling — when the just-logged set's weight (the LAST of `today_sets`)
    looks like a typo next to what this exercise was loaded with, in *either*
    direction: a dropped digit ("1 1" meant "140 6") reads the same as an extra
    one ("1400" meant "140"). Bodyweight exercises (0 kg either time) are
    exempt: a light set there is normal, not a typo.

    Сравнение идёт с самым тяжёлым подходом этого упражнения И в прошлый раз, И
    уже сегодня (сегодняшний — только рабочий, см. working_weight_today).
    """
    if not today_sets:
        return None
    last_weight, _last_reps = today_sets[-1]
    if last_weight <= 0:
        return None
    prev_max_weight = max((w for w, _r, _rpe in last_session or []), default=0.0)
    today_max_weight = working_weight_today(today_sets[:-1])
    baseline = max(prev_max_weight, today_max_weight)
    if baseline <= 0:
        return None
    too_low = last_weight < SUSPICIOUS_WEIGHT_LOW_FRACTION * baseline
    too_high = last_weight > SUSPICIOUS_WEIGHT_HIGH_MULTIPLE * baseline
    if not (too_low or too_high):
        return None
    u = formatting.unit_label(unit)
    # «сегодня», когда планка взялась из этой же тренировки: «в прошлый раз
    # 200кг» под списком, где 200 стоит восемь раз подряд сегодня, читалось бы
    # как ошибка самого тренера.
    since_key = "workout.since_last_time" if baseline == prev_max_weight else "workout.since_today"
    return i18n.t(
        "workout.suspicious_weight_warning",
        weight=formatting.format_weight(last_weight),
        u=u,
        since=i18n.t(since_key),
        baseline=formatting.format_weight(baseline),
    )


def suspicious_reps_warning(weight: float, reps: int) -> str | None:
    """«100 кг × 150 повторов? Похоже на промах» — или None, если всё в порядке.

    e1RM от 100×150 — это 600 кг, и оно становится вечным рекордом упражнения,
    попадает в тоннаж и в Зал славы, откуда его уже не убрать.
    """
    if weight <= 0 or reps <= REPS_CONFIRM_ABOVE:
        return None
    return i18n.t("workout.suspicious_reps_warning", set=formatting.format_set(weight, reps))


@dataclass(frozen=True)
class Suspicion:
    """Что именно насторожило: текст вопроса, вид проверки и номер подхода в
    разобранной строке (по нему `/v1` собирает предложение «поменять местами»)."""

    text: str
    kind: str
    index: int


def find_suspicion(
    last_session: list[tuple[float, int, float | None]] | None,
    resolved,
    unit: str = "kg",
    today_sets: list[tuple[float, int]] | None = None,
) -> Suspicion | None:
    """Первый подозрительный подход строки — или None.

    `resolved` — подходы, как они лягут в базу (голые повторы уже с весом
    прошлого подхода), объекты с `weight`, `reps`, `unit_explicit`.
    `last_session` — подходы прошлой тренировки (None у занесения задним
    числом: «прошлый раз» там случился позже заносимой даты). `today_sets` —
    что уже записано по упражнению в этой тренировке.

    Повторы проверяются и без истории: подозрительный вес виден только на фоне
    прошлого раза, а полторы сотни повторов подозрительны сами по себе.
    """
    done_today = list(today_sets or [])
    for index, ps in enumerate(resolved):
        warning = suspicious_weight_warning(last_session, [*done_today, (ps.weight, ps.reps)], unit)
        if warning:
            return Suspicion(warning, KIND_WEIGHT, index)
        # Вопрос про повторы — про перепутанные местами числа, и он уместен, пока
        # роли чисел заданы порядком. «15 kg / 90 reps» задаёт их словами:
        # спрашивать «не перепутал ли вес и повторы» у человека, который только
        # что написал, где вес и где повторы, — отвечать на незаданный вопрос.
        # Проверка веса остаётся: она про величину на фоне истории.
        if getattr(ps, "unit_explicit", False):
            continue
        warning = suspicious_reps_warning(ps.weight, ps.reps)
        if warning:
            return Suspicion(warning, KIND_REPS, index)
    return None


def swapped(weight: float, reps: int) -> tuple[float, int] | None:
    """«8 60» → (60, 8): вес и повторы, поменянные местами, — или None, если
    так не выходит (дробный «вес» в повторы не превращается, а новые повторы
    сами были бы подозрительными)."""
    if weight <= 0 or weight != int(weight):
        return None
    new_reps = int(weight)
    if new_reps < 1 or new_reps > REPS_CONFIRM_ABOVE:
        return None
    return float(reps), new_reps
