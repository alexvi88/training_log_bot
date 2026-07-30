"""Finding the user's own exercises inside free-form AI-trainer text.

The trainer writes prose, so an exercise it names comes out inflected and in
whatever case the sentence needs ("убери pull down", "замени на тягу
горизонтального блока"). A plain substring match against `exercises.name`
misses all of that, so names are compared word by word on their stems: two
words match when one is the other with a different Russian ending.

Overlapping matches are all kept: "жим лёжа узким хватом" in the text matches
both that exercise and the "Жим лёжа" inside it, and guessing which one the
trainer meant would silently drop the right card. Both get a button, the more
specific one first, and the user picks. Only the first few survive — a wall of
buttons under an answer is worse than none.
"""

import os
import re
from typing import Any, Optional, Sequence

import db

# Столько кнопок максимум вешаем под ответ: тренер может упомянуть пять
# упражнений в одном абзаце, но клавиатура на пять строк перекрывает сам ответ.
MAX_MENTIONS = 3

_WORD_RE = re.compile(r"[а-яa-z0-9]+")

# Короткие слова сравниваем только целиком: у «жим»/«жир» общий префикс уже
# в две буквы, и на стеммере они бы склеились.
_MIN_STEM_WORD = 4


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower().replace("ё", "е"))


def _same_word(a: str, b: str) -> bool:
    """Одно и то же слово с точностью до русского окончания."""
    if a == b:
        return True
    shorter = min(len(a), len(b))
    if shorter < _MIN_STEM_WORD:
        return False
    if abs(len(a) - len(b)) > 4:
        return False
    common = len(os.path.commonprefix([a, b]))
    return common >= shorter - 2


def _matches_at(haystack: list[str], start: int, needle: list[str]) -> bool:
    if start + len(needle) > len(haystack):
        return False
    return all(_same_word(haystack[start + i], word) for i, word in enumerate(needle))


def find_mentions(
    text: str, exercises: Sequence[Any], limit: int = MAX_MENTIONS
) -> list[Any]:
    """Упражнения из `exercises`, упомянутые в `text`, в порядке появления."""
    if not text:
        return []
    haystack = _tokens(text)
    if not haystack:
        return []

    candidates = []
    for ex in exercises:
        # display_name — это name плюс оснастка/хват («Жим лёжа · гантели»):
        # если тренер назвал упражнение полностью, совпадение длиннее и кнопка
        # встанет выше более общего однофамильца.
        for needle in sorted((_tokens(ex["display_name"]), _tokens(ex["name"])), key=len, reverse=True):
            # Название из одних коротких слов («Пресс») дало бы слишком много
            # ложных срабатываний на обычной прозе.
            if not needle or max(len(w) for w in needle) < _MIN_STEM_WORD:
                continue
            match = next(
                (
                    start
                    for start in range(len(haystack) - len(needle) + 1)
                    if _matches_at(haystack, start, needle)
                ),
                None,
            )
            if match is not None:
                candidates.append((match, len(needle), ex))
                break

    # Пересекающиеся совпадения не схлопываем: на «жим лёжа узким хватом»
    # подходят и он сам, и «Жим лёжа» внутри него — какое из них имел в виду
    # тренер, по тексту не решить, так что показываем оба и выбирает
    # пользователь. Порядок — по месту в тексте, при равном начале выше стоит
    # более конкретное (длинное) название.
    candidates.sort(key=lambda c: (c[0], -c[1]))
    return [ex for _, _, ex in candidates[:limit]]


async def find_in_text(
    user_id: int, text: Optional[str], limit: int = MAX_MENTIONS
) -> list[Any]:
    if not text:
        return []
    exercises = await db.list_user_exercises(user_id)
    return find_mentions(text, exercises, limit=limit)
