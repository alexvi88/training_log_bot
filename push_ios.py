"""Short (title, body) copy for iOS push notifications — sized for APNs
banners, not for a Telegram caption.

`push_texts.py` writes for a Telegram photo caption: long, no hard budget
worth mentioning, and no title at all. An APNs banner has both: a one-line
title and a body that gets cut to ~2 lines when collapsed. Handing it a
`push_texts` string means either a title-less banner or a body chopped
mid-word by the OS — the exact defect this module exists to prevent. So this
is a SEPARATE, short catalog (`push.ios.<category>.title` /
`push.ios.<category>.<n>`), not a trim of the long one.

No fallback to the long Telegram text is provided on purpose. A category
without a short pool is a bug in this module, and `tests/test_push_ios.py`
must catch it at test time — silently falling back to `push_texts` would
just reintroduce the truncation this module is here to remove.

Length limits (TITLE_LIMIT/BODY_LIMIT below) are our own editorial budget,
not a published Apple API constant — APNs' payload limit is a 4096-byte
JSON size, not a character count on `alert.title`/`alert.body`, and how much
of either actually renders depends on device width, Dynamic Type size, and
whether the banner is collapsed or expanded. We hold to the conservative end
of Apple's own Human Interface Guidelines / observed banner behavior so the
same copy reads fully on the narrowest common device, and a test enforces
the budget on every catalog string in both languages so a future addition
that runs long fails CI instead of shipping cut off.
"""

from __future__ import annotations

import random
import re

import db
import i18n
import push_texts

# One line on a banner/lock-screen title at default Dynamic Type, on the
# narrowest iPhone still sold (iPhone SE class) is roughly 30-35 Latin
# characters; Cyrillic glyphs render slightly wider on average on iOS system
# fonts, so we hold to the low end of that range for both languages at once
# rather than keeping a separate, more generous budget for English only.
TITLE_LIMIT = 32

# A collapsed banner shows about 2 lines of body (~55 characters/line at
# default size); an expanded banner or a Notification Center entry can show
# up to 4. We budget for the COLLAPSED case, since that's what most people
# actually read before it disappears — a body that only makes sense once
# pulled open isn't meaningfully different from one that got cut off.
BODY_LIMIT = 110

# Variable-length data (an exercise name, at most config.MAX_EXERCISE_NAME_LENGTH
# = 60 chars) gets clipped to this many characters BEFORE it goes into a
# template — bounding the one slot that can run long, rather than truncating
# the whole assembled sentence after the fact (which is the failure mode this
# module exists to avoid). 24 leaves every template above comfortably inside
# BODY_LIMIT even for the longest real exercise name today.
_PARAM_CLIP_LIMIT = 24

# push_ios has no static pool of its own for the AI-generated weekly digest
# (push_texts.AI_WEEKLY): that push's Telegram body is a free-form model
# completion, not a catalog string, and generating a *second*, short
# model completion just for the iOS banner is out of scope here. The iOS
# banner for that Sunday slot reuses the WEEKLY_DIGEST short copy instead —
# same event (the week's numbers are ready), same params (tonnage, workout
# count) are already computed by engagement.py on that code path regardless
# of whether the AI digest text ended up being used for Telegram.
_CATALOG_CATEGORY: dict[str, str] = {push_texts.AI_WEEKLY: push_texts.WEEKLY_DIGEST}

# Разовые релизные анонсы (announcements.py) — не часть дневной ротации
# push_texts (там у каждой рассылки свой ключ, announcement.key, произвольный
# и заводится с каждым релизом), поэтому у неё нет и не будет отдельного
# push_texts-пула на каждый ключ: банер один и тот же для ЛЮБОГО анонса
# ("в дневнике что-то новое, глянь в приложении"), а сам текст релиза остаётся
# только в телеграмной версии. Категория — своя, объявлена здесь же, а не в
# push_texts.py: она не участвует в дневной цепочке приоритетов и не должна
# заводить там пустой пул.
ANNOUNCEMENT = "announcement"

# Every category push_ios can produce a banner for: push_texts' own rotation
# categories, plus AI_WEEKLY (aliased above) and ANNOUNCEMENT.
CATEGORIES: tuple[str, ...] = push_texts._CATEGORIES + (push_texts.AI_WEEKLY, ANNOUNCEMENT)


def _catalog_category(category: str) -> str:
    return _CATALOG_CATEGORY.get(category, category)


def _load_title(lang: str, category: str) -> str:
    key = f"push.ios.{_catalog_category(category)}.title"
    catalog = i18n._load_catalog(lang)
    if key not in catalog:
        raise KeyError(
            f"push_ios: нет короткого заголовка {key!r} — заведи push.ios.{category}.title "
            f"в locales/{lang}.json, а не подставляй длинный текст push_texts"
        )
    return catalog[key]


def _load_body_pool(lang: str, category: str) -> list[str]:
    """Пул коротких вариантов тела — та же схема нумерованных ключей
    (`push.ios.<category>.<n>`), что push_texts._load_pool использует для
    push.<category>.<n>, только под отдельным префиксом `push.ios.`."""
    prefix = f"push.ios.{_catalog_category(category)}."
    catalog = i18n._load_catalog(lang)
    items = [
        (int(k[len(prefix):]), v)
        for k, v in catalog.items()
        if k.startswith(prefix) and k[len(prefix):].isdigit()
    ]
    if not items:
        raise KeyError(
            f"push_ios: нет короткого пула push.ios.{category}.* — заведи хотя бы один "
            f"вариант в locales/{lang}.json, а не подставляй длинный текст push_texts"
        )
    return [text for _, text in sorted(items)]


# Собираются один раз на импорт модуля — как и push_texts.TEXTS_BY_LANG,
# ради того же эффекта: категория без короткого пула валит импорт (а значит и
# сбор тестов) сразу, а не тихо в рантайме на первом же пуше этой категории.
TITLES_BY_LANG: dict[str, dict[str, str]] = {
    lang: {category: _load_title(lang, category) for category in CATEGORIES}
    for lang in i18n.SUPPORTED
}
BODY_POOLS_BY_LANG: dict[str, dict[str, list[str]]] = {
    lang: {category: _load_body_pool(lang, category) for category in CATEGORIES}
    for lang in i18n.SUPPORTED
}


def _clip_param(value: str, limit: int = _PARAM_CLIP_LIMIT) -> str:
    """Обрезать ОДНО значение плейсхолдера (имя упражнения, звание) до вставки
    в короткий шаблон, а не резать уже собранную фразу целиком постфактум —
    ровно та разница, ради которой существует этот модуль. Режем по границе
    пробела, если она есть в пределах лимита, чтобы не разорвать слово
    посередине."""
    if len(value) <= limit:
        return value
    cut = value[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + "…"


# Плейсхолдеры, чьё значение может быть произвольной длины (имя упражнения,
# введённое пользователем; строка звания) и потому клипуются перед
# форматированием. Остальные плейсхолдеры (weeks/days_left/missing/tonnage/
# week_count) уже собраны короткими фразами в push_texts/engagement и в
# клипе не нуждаются.
_CLIPPED_PARAMS = ("exercise", "rank")


def _placeholders(template: str) -> set[str]:
    return set(re.findall(r"{(\w+)}", template))


async def _pick_ios_body(telegram_id: int, category: str, lang: str, **format_kwargs: object) -> str:
    """Тот же неповторяющийся «мешок» вариантов, что push_texts.pick_text, но
    над ОТДЕЛЬНЫМ пулом (BODY_POOLS_BY_LANG вместо push_texts.TEXTS_BY_LANG).

    Не вызывает push_texts.pick_text напрямую: та функция жёстко читает
    `push_texts.TEXTS_BY_LANG[lang][category]` без параметра, которым можно
    было бы указать другой каталог/префикс — менять её сигнатуру ради
    единственного вызывающего значило бы усложнять push_texts.py, а он вне
    области этой задачи. Вместо этого — copy той же самой логики над своим
    пулом.

    Ключ мешка в БД (`db.push_rotation`, тот же движок get/save_rotation_bag)
    намеренно НЕ совпадает с телеграмным: `f"ios:{category}"` вместо
    `category`. Пулы разной длины и разного состава (ios-пул короче и не
    знает part про best_day/whale), так что общий индекс тут же разошёлся бы
    с «следующий непоказанный вариант» уже на первой выдаче — де-факто это
    была бы отдельная последовательность, притворяющаяся общей.
    """
    pool = BODY_POOLS_BY_LANG[lang][category]
    missing = {k for k, v in format_kwargs.items() if v is None}
    eligible = [i for i, t in enumerate(pool) if not any("{" + k + "}" in t for k in missing)]
    if not eligible:
        eligible = [i for i, t in enumerate(pool) if not _placeholders(t)] or list(range(len(pool)))
    clean_kwargs = {k: v for k, v in format_kwargs.items() if v is not None}
    if len(eligible) == 1:
        return pool[eligible[0]].format(**clean_kwargs)

    bag_category = f"ios:{category}"
    eligible_set = set(eligible)
    bag = [i for i in await db.get_rotation_bag(telegram_id, bag_category) if i in eligible_set]
    if not bag:
        bag = list(eligible)
        random.shuffle(bag)
    index = bag.pop(0)
    await db.save_rotation_bag(telegram_id, bag_category, bag)
    return pool[index].format(**clean_kwargs)


async def ios_alert(telegram_id: int, category: str, lang: str, **params: object) -> tuple[str, str]:
    """(title, body) для APNs alert — оба в пределах TITLE_LIMIT/BODY_LIMIT
    для любого допустимого значения params (см. тест на длину:
    tests/test_push_ios.py).

    `category` — один из push_ios.CATEGORIES (push_texts._CATEGORIES плюс
    AI_WEEKLY). Неизвестная категория или отсутствующий короткий пул для неё
    — KeyError при импорте модуля (см. TITLES_BY_LANG/BODY_POOLS_BY_LANG
    выше), а не тут: до вызова этой функции дело в проде не доходит, если
    каталог неполон.
    """
    resolved_lang = lang if lang in i18n.SUPPORTED else i18n.DEFAULT_LANG
    clipped = {
        k: (_clip_param(v) if k in _CLIPPED_PARAMS and isinstance(v, str) else v)
        for k, v in params.items()
    }
    title = TITLES_BY_LANG[resolved_lang][category]
    body = await _pick_ios_body(telegram_id, category, resolved_lang, **clipped)
    return title, body
