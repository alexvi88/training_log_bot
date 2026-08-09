"""Finding exercises — the user's own and the catalog's — inside free-form
AI-trainer text.

The trainer writes prose, so an exercise it names comes out inflected and in
whatever case the sentence needs ("убери pull down", "замени на тягу
горизонтального блока"). A plain substring match against `exercises.name`
misses all of that, so names are compared word by word on their stems: two
words match when one is the other with a different Russian ending.

Catalog templates the user hasn't added yet are matched too ("Из каталога на
плечи бери эти: ...") — see find_in_text, which excludes any template the
user already owns under the same display name so it doesn't duplicate the
user's own card with a second "add it" button.

Two matches that overlap in the text are kept together only when they're
truly the same exercise — same `name`, just different equipment/attachment
("Жим лёжа · штанга" vs "Жим лёжа · гантели"): which variant the trainer
meant can't be told from the text alone, so both get a button, more specific
first, and the user picks. Any other overlap is between two unrelated
exercises whose words just happen to collide ("pull down" sitting inside
"abs - pull down block"), and only the longer/more specific match survives —
showing a card for an exercise the trainer never actually meant is worse than
not showing one at all.
"""

import os
import re
from typing import Any, Optional, Sequence

import db

# Столько кнопок максимум вешаем под ответ: тренер может упомянуть пять
# упражнений в одном абзаце, но клавиатура на пять строк перекрывает сам ответ.
MAX_MENTIONS = 3

# Верхняя граница на то, сколько упоминаний вообще держим ради пролистывания
# (см. keyboards.ai_trainer_keyboard) — id всех найденных упражнений едут в
# callback_data кнопок-стрелок, а у Telegram там жёсткий лимит в 64 байта.
MAX_MENTIONS_TOTAL = 6

_WORD_RE = re.compile(r"[а-яa-z0-9]+")

# Короткие слова сравниваем только целиком: у «жим»/«жир» общий префикс уже
# в две буквы, и на стеммере они бы склеились.
_MIN_STEM_WORD = 4

# Однословные названия упражнений, которые тем же словом — обычная лексика.
# Живой прогон поймал «планку»: тренер написал «до нижней планки не хватает»
# (планка = порог, идиома) и получил кнопку на упражнение «Планка», которого
# в разговоре не было вовсе. Список нарочно короткий и растёт по фактам, а не
# по догадке — угадывать заранее, какое ещё название совпадёт с обычным
# словом, значит резать заодно и настоящие упоминания.
_AMBIGUOUS_SINGLE_WORDS = {"планка"}


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
    # У четырёх-пятибуквенного слова на окончание остаётся одна буква
    # («тяга»/«тяги»): разреши две — и «плюс» сойдётся с «планкой» на общих
    # «пл», а «приём» с «приседом» на «при».
    max_ending = 1 if shorter <= _MIN_STEM_WORD + 1 else 2
    return common >= shorter - max_ending


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
            if len(needle) == 1 and needle[0] in _AMBIGUOUS_SINGLE_WORDS:
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

    # Совпадения, которые пересекаются в тексте, схлопываем в кластеры — но
    # оставляем оба (все) совпадения кластера только если это буквально одно
    # и то же упражнение (одинаковый `name`, отличается только оснастка/хват).
    # Для любого другого пересечения оставляем один, самый длинный/конкретный
    # матч — иначе случайное совпадение слов между двумя разными упражнениями
    # («pull down» внутри «abs - pull down block») давало бы кнопку на
    # упражнение, которое тренер вообще не упоминал.
    def _overlaps(a: tuple[int, int, Any], b: tuple[int, int, Any]) -> bool:
        a_start, a_len, _ = a
        b_start, b_len, _ = b
        return a_start < b_start + b_len and b_start < a_start + a_len

    n = len(candidates)
    parent = list(range(n))

    def _find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def _union(i: int, j: int) -> None:
        ri, rj = _find(i), _find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if _overlaps(candidates[i], candidates[j]):
                _union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(_find(i), []).append(i)

    kept = []
    for idxs in clusters.values():
        cluster = [candidates[i] for i in idxs]
        names = {c[2]["name"] for c in cluster}
        if len(names) == 1:
            kept.extend(cluster)
        else:
            kept.append(max(cluster, key=lambda c: (c[1], -c[0])))

    # Порядок — по месту в тексте, при равном начале выше стоит более
    # конкретное (длинное) название.
    kept.sort(key=lambda c: (c[0], -c[1]))
    return [ex for _, _, ex in kept[:limit]]


async def find_in_text(
    user_id: int, text: Optional[str], limit: int = MAX_MENTIONS
) -> list[Any]:
    if not text:
        return []
    own = await db.list_user_exercises(user_id)
    owned_names = {ex["display_name"].strip().lower() for ex in own}
    templates = [
        t for t in await db.list_all_exercise_templates()
        if t["display_name"].strip().lower() not in owned_names
    ]
    return find_mentions(text, list(own) + templates, limit=limit)
