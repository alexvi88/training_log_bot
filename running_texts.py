"""Placeholder-фразы для «тренер думает», подобранные под тему вопроса.

Пока ai_trainer.ask ждёт ответ модели (секунды, иногда десятки секунд — особенно
с tool-calls и веб-поиском под капотом), handlers/ai_trainer.py крутит в
placeholder-сообщении одну из фраз ниже. Раньше пул был один общий на все
случаи — вопрос про питание крутил "гружу знания, как штангу", вопрос про
программу — "остываю от подхода": фраза никак не намекала, о чём вообще спросили.

Здесь фразы разложены по темам, а тема угадывается по ключевым словам-стемам —
тем же приёмом, что и в exercise_mentions.py: текст фолдится в нижний регистр,
"ё" нормализуется в "е" (иначе "объём"/"объем" разъедутся), слово считается
попаданием в тему, если оно начинается с одного из стемов темы. Это не полный
стеммер (see exercise_mentions.find_mentions — там другая задача, сопоставление
двух конкретных названий упражнений друг с другом), а грубая, но дешёвая
классификация: нам не нужна точность, нужно, чтобы самая первая фраза, которую
человек видит ещё до единого tool-call, уже была в тему.

Темы проверяются по порядку в _TOPIC_STEMS — если текст можно отнести к
нескольким (например, «программа на неделю» — и PROGRAM, и WEEKLY_VOLUME), то
побеждает та, что стоит раньше в списке. Фарма и боль/травмы проверяются
первыми: жалоба на боль после жима — это про восстановление, а не про прогресс
в жиме, а «сколько скинул на оземпике» — про препарат, а не про дневник веса
(стем «масс» там тоже найдётся). Если ни один стем не подошёл — DEFAULT_TOPIC,
тот самый универсальный пул, почти не изменившийся с исходной версии.

Важное про сами фразы: пул выбирается ДО первого вызова инструмента, то есть по
догадке из слов вопроса. Поэтому фраза не должна утверждать, что бот уже куда-то
залез или что у человека что-то есть, — иначе на неверно угаданной теме она
врёт про данные (TONE_OF_VOICE.md: утверждение о данных отправляется только
когда данные его подтверждают). Так и вышло на пересланном посте про
семаглутид: «ищу, не слишком ли быстро уходит или приходит вес» — а бот не
смотрел ни в один дневник. Проверенные фразы про конкретный инструмент живут
отдельно, в ai_trainer._TOOL_RUNNING_TEXTS: они показываются, когда инструмент
реально вызван, и утверждать там можно.

Третий регистр персонажа (TONE_OF_VOICE.md: строчные с эмодзи) — единственный
регистр этого модуля, английская версия тоже строчная и с эмодзи, не капс и не
проза. `pool_for` берёт язык из `i18n.get_lang()` — того же контекста, который
handlers/ai_trainer.py уже выставляет к моменту вызова (middleware ставит язык
на весь апдейт), а не из текста вопроса: вопрос может быть на любом языке
(«how much protein do I need»), а показывать плейсхолдер всё равно нужно на
языке интерфейса пользователя. `classify` при этом смотрит и русские, и
английские стемы сразу — англоязычный вопрос обязан попадать в свою тему, а не
скатываться в DEFAULT_TOPIC только потому, что показывать фразу будем
по-английски.

Сами пулы (оба языка) лежат в каталоге (locales/*.json, ключи
`thinking.<тема>.<n>` и `factcheck.thinking.<n>` — см. `_load_pool` ниже), не в
этом файле: тот же приём, что у push_texts.py (`push.<категория>.<n>`), и по
той же причине — так пулы бесплатно попадают под все три слоя
tests/test_i18n_no_leaks.py (совпадение ключей между языками, парсинг ICU,
отсутствие кириллицы в en), а не только под собственные тесты этого модуля.
Раньше английский пул жил рядом с русским прямо в модуле — со своей
дисциплиной, отдельной от push_texts.py и без этой защиты; теперь оба каталога
устроены одинаково.

FACT_CHECK_POOL (используется `handlers/factcheck.py`) — тот же приём, но
доступ не через `pool_for` (там нет темы вопроса, разбор форварда — это всегда
одна и та же ситуация, см. `handlers.factcheck._looks_like_a_forwarded_post`):
`fact_check_pool()` ниже отдаёт пул на языке текущего пользователя, тем же
`i18n.get_lang()`, что и `pool_for`.
"""

import random
import re
from typing import Optional

import i18n

_WORD_RE = re.compile(r"[а-яa-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower().replace("ё", "е"))


PROGRAM = "program"
EXERCISE_PROGRESS = "exercise_progress"
WEEKLY_VOLUME = "weekly_volume"
NUTRITION = "nutrition"
BODYWEIGHT = "bodyweight"
TECHNIQUE = "technique"
RECOVERY = "recovery"
HISTORY = "history"
TODAY = "today"
MOTIVATION = "motivation"
PHARMA = "pharma"
SUPPLEMENTS = "supplements"
EQUIPMENT = "equipment"
SLEEP = "sleep"
WARMUP = "warmup"
DEFAULT_TOPIC = "default"

# Порядок — приоритет при пересечении тем (см. модульный докстринг). Стемы
# смешивают русский и английский в одном кортеже — _tokens уже фолдит любой
# текст в нижний регистр, а вопрос на английском должен попадать в свою тему
# ровно так же, как вопрос на русском (см. докстринг про i18n.get_lang()).
_TOPIC_STEMS: list[tuple[str, tuple[str, ...]]] = [
    # Фарма — раньше веса и питания намеренно: «сколько скинул на оземпике» это
    # вопрос про препарат, а не про дневник взвешиваний, хотя стем «масс» там
    # тоже найдётся. Ровно на этом пул про вес и вылез на пересланном посте про
    # семаглутид.
    (PHARMA, (
        "оземпик", "семаглутид", "тирзепатид", "мунджаро", "лираглутид",
        "стероид", "тестостерон", "анабол", "аас", "сарм", "гормон",
        "тренболон", "станозолол", "нандролон", "фарм", "инсулин",
        "ozempic", "semaglutide", "tirzepatide", "mounjaro", "liraglutide",
        "steroid", "testosterone", "anabolic", "sarm", "hormone",
        "trenbolone", "stanozolol", "nandrolone", "insulin",
    )),
    (RECOVERY, (
        "боли", "больно", "болит", "болел", "болев", "травм", "растяжен",
        "потян", "восстановлен", "перетрен", "устал", "надорв",
        "pain", "hurt", "sore", "injur", "strain", "sprain", "recover",
        "overtrain", "exhaust",
    )),
    (SLEEP, (
        "сон", "сна", "спать", "спл", "выспа", "недосып", "бессонниц", "циркадн",
        "sleep", "insomnia", "circadian",
    )),
    (SUPPLEMENTS, (
        "креатин", "протеин", "гейнер", "бцаа", "bcaa", "добавк", "витамин",
        "омега", "предтрен", "изолят", "казеин", "цитруллин", "бета-алан",
        "creatine", "whey", "preworkout", "casein", "citrulline",
    )),
    (NUTRITION, (
        "питан", "калори", "белк", "углевод", "рацион", "диет", "бжу",
        "nutrition", "calorie", "protein", "carb", "macro",
    )),
    (WARMUP, (
        "разминк", "разминат", "разогрев", "растяжк", "растягив", "заминк",
        "мобильн", "миофасц",
        "warmup", "warm", "stretch", "mobility",
    )),
    (EQUIPMENT, (
        "заменит", "замена", "заменять", "вместо", "дома", "домашн",
        "инвентар", "оборудован", "гантел", "резинк", "турник", "тренажер",
        "replac", "substitut", "instead", "equipment", "dumbbell", "band",
    )),
    (BODYWEIGHT, (
        "сушк", "похуд", "взвеш", "набира", "масс", "вешу",
        "cutting", "bulking", "weigh",
    )),
    (PROGRAM, (
        "программ", "сплит", "мезоцикл", "макроцикл", "периодизац",
        "program", "split", "mesocycle", "macrocycle", "periodiz",
    )),
    (WEEKLY_VOLUME, (
        "объем", "перегруж", "недел", "баланс", "равномер",
        "volume", "overload", "weekly", "balanc",
    )),
    (EXERCISE_PROGRESS, (
        "прогресс", "рекорд", "плато", "максимум", "вырос", "увелич", "1пм", "e1rm",
        "progress", "record", "plateau", "increase",
    )),
    (TECHNIQUE, (
        "техник", "правильн", "ошибк", "выполня",
        "technique", "form", "mistake", "perform",
    )),
    (HISTORY, (
        "истори", "статистик", "раньше", "прошл",
        "histor", "stat", "before", "past",
    )),
    (TODAY, (
        "сегодня", "сейчас", "щас",
        "today", "now",
    )),
    (MOTIVATION, (
        "мотивац", "лень", "вдохнов", "смысл", "надоел",
        "motivat", "lazy", "inspir",
    )),
]

# ---------- пулы фраз ----------
#
# Оба языка загружаются из каталога (locales/*.json, ключи `thinking.<тема>.<n>`)
# — тот же приём, что у push_texts._load_pool, и по той же причине (см.
# модульный докстринг): один вариант — один ключ каталога, который тесты
# полноты каталога проверяют бесплатно.

_TOPICS: tuple[str, ...] = (
    PROGRAM, EXERCISE_PROGRESS, WEEKLY_VOLUME, NUTRITION, BODYWEIGHT, TECHNIQUE,
    RECOVERY, HISTORY, TODAY, MOTIVATION, PHARMA, SUPPLEMENTS, EQUIPMENT, SLEEP,
    WARMUP, DEFAULT_TOPIC,
)


def _load_pool(lang: str, prefix: str) -> list[str]:
    """Пул вариантов по префиксу ключа каталога (`thinking.<тема>.` или
    `factcheck.thinking.`), отсортированный по числовому суффиксу — тот же
    приём, что push_texts._load_pool."""
    catalog = i18n._load_catalog(lang)
    items = [(int(k[len(prefix):]), v) for k, v in catalog.items() if k.startswith(prefix)]
    return [text for _, text in sorted(items)]


# lang -> {тема -> пул}. Ключ каталога — `thinking.<тема>.<n>`.
POOLS: dict[str, list[str]] = {topic: _load_pool("ru", f"thinking.{topic}.") for topic in _TOPICS}
POOLS_EN: dict[str, list[str]] = {topic: _load_pool("en", f"thinking.{topic}.") for topic in _TOPICS}

_POOLS_BY_LANG: dict[str, dict[str, list[str]]] = {"ru": POOLS, "en": POOLS_EN}

# Разбор пересланного поста (handlers/factcheck.py) — ключи `factcheck.thinking.<n>`,
# отдельный префикс от тематических пулов выше: это не тема вопроса, а всегда
# одна и та же ситуация (читаем чужой текст, а не свои данные), см.
# handlers.factcheck._looks_like_a_forwarded_post.
_FACT_CHECK_POOLS_BY_LANG: dict[str, list[str]] = {
    lang: _load_pool(lang, "factcheck.thinking.") for lang in i18n.SUPPORTED
}


def fact_check_pool() -> list[str]:
    """Пул для разбора пересланного поста на языке текущего пользователя —
    тот же выбор по ambient i18n.get_lang(), что и в pool_for()."""
    return _FACT_CHECK_POOLS_BY_LANG.get(i18n.get_lang(), _FACT_CHECK_POOLS_BY_LANG[i18n.DEFAULT_LANG])




def classify(text: str) -> str:
    """Тема вопроса по ключевым словам-стемам, либо DEFAULT_TOPIC, если ни
    один стем не подошёл (в том числе для пустого текста)."""
    tokens = _tokens(text or "")
    if not tokens:
        return DEFAULT_TOPIC
    for topic, stems in _TOPIC_STEMS:
        if any(token.startswith(stem) for token in tokens for stem in stems):
            return topic
    return DEFAULT_TOPIC


def pool_for(text: str) -> list[str]:
    """Пул фраз под тему вопроса, на языке текущего пользователя.

    Язык берём из ambient i18n.get_lang() (тот же приём, что analytics.Rank.name
    и push_texts.pick_text), а не угадываем по тексту вопроса: вопрос может быть
    на любом языке, а плейсхолдер должен звучать на языке интерфейса. По
    умолчанию (язык не поддерживается или контекст не выставлен) отдаём
    русский пул — так же, как i18n делает ru fallback для отсутствующего ключа.
    """
    topic = classify(text)
    return _POOLS_BY_LANG.get(i18n.get_lang(), POOLS)[topic]


def pick(pool: list[str]) -> str:
    return random.choice(pool)


def pick_different(pool: list[str], exclude: Optional[str]) -> str:
    """Случайная фраза из пула, отличная от предыдущей — иначе editText упадёт
    с "message is not modified", да и ротация без этого выглядит нечестно."""
    if len(pool) <= 1:
        return pick(pool)
    choice = exclude
    while choice == exclude:
        choice = pick(pool)
    return choice
