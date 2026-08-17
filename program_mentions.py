"""Finding the user's saved programs inside free-form AI-trainer text.

Тренер перечисляет программы прозой («Две «Вики» — дубликаты друг друга, PPL
и «от 02.08» — тоже почти близнецы»), а человек после такого ответа хочет
открыть ту, о которой речь. Раньше он получал инструкцию «⚙️ Программы →
выбирай нужную» — то есть ответ знал, о чём говорит, а кнопка под ним об этом
не знала.

Совпадение ищется тем же посложным стеммером, что и для упражнений (см.
exercise_mentions): имена программ люди пишут как придётся и склоняют их в
тексте так же. Отличие одно — имя вроде «Программа от 02.08» состоит из
слов, которые в ответе тренера встречаются на каждом шагу, поэтому имя из
одних общих слов кнопкой не становится: ложная ссылка на чужую программу
хуже, чем её отсутствие.
"""

from typing import Any

import db
import exercise_mentions

# Кнопок-ссылок на программы под одним ответом: их лимит общий с упоминаниями
# упражнений (см. keyboards.ai_trainer_keyboard), а сам ответ обычно про одну-
# две программы — на большее это уже оглавление, а не ссылка по тексту.
MAX_PROGRAM_MENTIONS = 2

# Слова, из которых имя не может состоять целиком: «Программа от 02.08» иначе
# ловилось бы в любом абзаце, где тренер произнёс слово «программа». Английский
# набор — то же самое для англоязычного атлета («Program 8/2», «Leg day»): без
# него фильтр не срабатывает вовсе на английском тексте, и защита от ложных
# совпадений программ пропадает молча — ровно тот же разрыв, что был бы у
# search_terms без английского стемминга (см. его шапку и voice_parse.py: искать
# нужно на обоих языках сразу, независимо от языка интерфейса).
_TOO_COMMON = {
    "программа", "программе", "программы", "прога", "день", "дни", "тренировка", "тренировки",
    "program", "programs", "routine", "routines", "day", "days", "workout", "workouts", "session", "sessions",
}


async def list_targets(user_id: int) -> list[dict[str, Any]]:
    """Сохранённые программы в виде, который понимает exercise_mentions.

    `kind`/`id` — то, чем открывается программа: многодневка своим экраном
    (`rt:prg:`), одиночная — карточкой дня (`rt:view:`).
    """
    targets: list[dict[str, Any]] = []
    for row in await db.list_programs(user_id):
        targets.append(
            {"kind": "program", "id": row["id"], "name": row["program_name"],
             "display_name": row["program_name"]}
        )
    for routine in await db.list_standalone_routines(user_id):
        targets.append(
            {"kind": "routine", "id": routine["id"], "name": routine["name"],
             "display_name": routine["name"]}
        )
    return targets


def _is_distinctive(name: str) -> bool:
    return any(word not in _TOO_COMMON for word in exercise_mentions._tokens(name))


async def find_in_text(
    user_id: int, text: str | None, limit: int = MAX_PROGRAM_MENTIONS
) -> list[dict[str, Any]]:
    if not text:
        return []
    targets = [t for t in await list_targets(user_id) if _is_distinctive(t["name"])]
    if not targets:
        return []
    return exercise_mentions.find_mentions(text, targets, limit=limit)
