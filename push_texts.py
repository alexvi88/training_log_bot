"""Push notification copy: the 'Привет Атлет' basement-coach voice.

Every category is a pool of interchangeable variants. pick_text() rotates
through a pool per (telegram_id, category) without repeating a variant until
the whole pool has been shown once (a shuffled "bag"), so a user who gets the
same category on two different days doesn't see the same line twice in a row.

Voice rules (see PUSH_IDEAS.md and TONE_OF_VOICE.md's "English voice" for the
full writeup):
  - Every push opens with "ПРИВЕТ АТЛЕТ! " (ru) / "HEY ATHLETE! " (en) —
    capitalized, никогда "боец"/"пользователь" ("fighter"/"user"/"buddy").
  - Jabs are reserved for the skip-milestone categories only, in both languages.
  - Every other category stays supportive. The two positive categories
    (STREAK_MILESTONE, RANK_NEAR) celebrate or point at a close goal — a push
    isn't only for absence, or the coach reads as someone who only ever scolds.

Skip milestones (3/5/7/10/14 days since the last workout) each get their own
pool rather than one shared pool: the wording references the actual day
count ("неделя простоя"/"a full week", "две недели"/"two weeks"), so a day-3
skip must never draw a day-14 line — hence a dedicated category per milestone
instead of one big "skip" bucket the rotation could hand out regardless of
which day fired.

Пулы по языкам хранятся в каталоге (locales/*.json, ключи `push.<category>.<n>`,
см. `_load_pool`/`TEXTS_BY_LANG` ниже) — не в этом файле: так их бесплатно
покрывают все три слоя tests/test_i18n_no_leaks.py (совпадение ключей и
плейсхолдеров между языками, парсинг ICU, отсутствие кириллицы в en), а не
только собственные тесты этого модуля.
"""

import random
import re

import db
import i18n

# Category keys, in daily-job priority order (first eligible one wins).
# NEWBIE_NUDGE sits outside that chain: it fires from a separate walk pool
# (users with zero finished workouts, disjoint from the priority chain's pool).
STREAK_MILESTONE = "streak_milestone"
STREAK_AT_RISK = "streak_at_risk"
SKIP_3 = "skip_3"
SKIP_5 = "skip_5"
SKIP_7 = "skip_7"
SKIP_10 = "skip_10"
SKIP_14 = "skip_14"
WIN_BACK = "win_back"
RANK_NEAR = "rank_near"
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

# A blue whale weighs roughly 100–150 metric tons. The WEEKLY_DIGEST "это
# примерно один синий кит" line is only honest once a user's monthly tonnage
# is actually in that neighborhood — at the low end of a normal training
# month (a few tons) the comparison is just false. Engagement passes `whale`
# as None below this bound (in kg, converted to the user's own unit already),
# which drops the variant from the draw the same way a missing `best_day`
# does — see pick_text.
WHALE_MIN_TONNAGE_KG = 100_000

# Категории пуш-пулов — те же значения, что константы выше, задают ключи
# push.<category>.<n> в каталоге (locales/*.json): плоский JSON "строка ->
# строка" не может хранить список вариантов под одним ключом, а заводить под
# пуши отдельный файл пулов или переходить на JSON-массивы в значениях
# каталога означало бы увести push.* мимо всех трёх слоёв
# test_i18n_no_leaks.py (совпадение ключей между языками, парсинг ICU,
# отсутствие кириллицы в en, совпадение плейсхолдеров) — под них пришлось бы
# писать отдельные копии тех же проверок. Один вариант — один ключ каталога:
# обычная строка, которую эти тесты уже умеют проверять бесплатно.
_CATEGORIES: tuple[str, ...] = (
    STREAK_MILESTONE, STREAK_AT_RISK, SKIP_3, SKIP_5, SKIP_7, SKIP_10, SKIP_14,
    WIN_BACK, RANK_NEAR, PLATEAU, WEEKLY_DIGEST, NEWBIE_NUDGE,
)


def _load_pool(lang: str, category: str) -> list[str]:
    """Пул вариантов для категории — ключи `push.<category>.<n>` каталога,
    отсортированные по числовому суффиксу (порядок среди равнозначных
    вариантов роли не играет, но детерминированный список проще читать в
    тестах и диффах, чем порядок обхода словаря).

    `i18n._load_catalog` — приватная функция того же проекта (не отдельного
    пакета): переиспользуем её дисковый кэш вместо второго парсинга того же
    JSON.
    """
    prefix = f"push.{category}."
    catalog = i18n._load_catalog(lang)
    items = [(int(k[len(prefix):]), v) for k, v in catalog.items() if k.startswith(prefix)]
    return [text for _, text in sorted(items)]


# TEXTS_BY_LANG — пул на каждый поддерживаемый язык. TEXTS остаётся алиасом на
# русский пул ради обратной совместимости с существующим кодом и тестами
# (tests/test_push_texts.py трогает push_texts.TEXTS напрямую, не зная о
# языке вовсе — там всегда работает ambient-дефолт i18n, то есть "ru").
TEXTS_BY_LANG: dict[str, dict[str, list[str]]] = {
    lang: {category: _load_pool(lang, category) for category in _CATEGORIES}
    for lang in i18n.SUPPORTED
}
TEXTS: dict[str, list[str]] = TEXTS_BY_LANG[i18n.DEFAULT_LANG]

CATEGORY_LABELS: dict[str, str] = {
    STREAK_MILESTONE: "Серия: рубеж взят",
    STREAK_AT_RISK: "Серия на кону",
    SKIP_3: "Пропуск (3 дня)",
    SKIP_5: "Пропуск (5 дней)",
    SKIP_7: "Пропуск (7 дней)",
    SKIP_10: "Пропуск (10 дней)",
    SKIP_14: "Пропуск (14 дней)",
    WIN_BACK: "Возвращение",
    RANK_NEAR: "Близко к званию",
    PLATEAU: "Плато",
    WEEKLY_DIGEST: "Аналитика",
    AI_WEEKLY: "AI-дайджест",
    NEWBIE_NUDGE: "Новичок без тренировок",
}


async def pick_text(telegram_id: int, category: str, **format_kwargs: object) -> str:
    """Return the next non-repeating variant for this user+category, formatted with kwargs.

    Draws from a shuffled bag persisted per (telegram_id, category); refills
    and reshuffles once exhausted so every variant is seen before any repeat.

    A placeholder passed as None means "no data for this" — variants using it
    are dropped from the draw rather than filled with a guess. That's how a
    claim about the user's own history ("{best_day} — твой самый продуктивный
    день") stays true: without the data behind it, the line simply isn't sent.

    The bag stores indices into the *full* pool (TEXTS_BY_LANG[lang][category]),
    not into the filtered one, and filters it at draw time instead. Two calls for
    the same category can see different eligible subsets (a WEEKLY_DIGEST draw
    with `best_day` known has one variant more than a draw without it) — if
    the bag were keyed by position in that shifting subset, "index 3" would
    point at a different template each time, and the "no repeat before the
    pool is exhausted" promise would quietly break. Full-pool indices stay
    meaningful no matter which subset is eligible on a given call.

    Which pool is "full" depends on the caller's ambient language
    (`i18n.get_lang()`, the same convention `analytics.Rank.name` uses) — the
    caller wraps the whole daily-job tick for one user in `i18n.use_lang(...)`
    (see engagement._send_daily_pushes), never passes a lang kwarg here. The
    bag itself stays keyed on (telegram_id, category) only, with no language in
    the key: every language's pool for a category is built to the same length
    (enforced when the catalog is assembled), so a stored index always resolves
    to *some* variant even if a user switches language mid-rotation — worst
    case that one draw doesn't line up with "next unseen in this language",
    which is cheaper than a second bag column per language for a switch nobody
    does mid-week.
    """
    lang = i18n.get_lang()
    full_pool = TEXTS_BY_LANG.get(lang, TEXTS_BY_LANG[i18n.DEFAULT_LANG])[category]
    missing = {k for k, v in format_kwargs.items() if v is None}
    eligible = [i for i, t in enumerate(full_pool) if not any("{" + k + "}" in t for k in missing)]
    if not eligible:
        eligible = [i for i, t in enumerate(full_pool) if not _placeholders(t)] or list(range(len(full_pool)))
    if len(eligible) == 1:
        return full_pool[eligible[0]].format(**format_kwargs)

    eligible_set = set(eligible)
    bag = await db.get_rotation_bag(telegram_id, category)
    bag = [i for i in bag if i in eligible_set]
    if not bag:
        bag = list(eligible)
        random.shuffle(bag)
    index = bag.pop(0)
    await db.save_rotation_bag(telegram_id, category, bag)
    return full_pool[index].format(**format_kwargs)


def _placeholders(template: str) -> set[str]:
    return set(re.findall(r"{(\w+)}", template))
