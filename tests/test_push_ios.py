"""push_ios.py: короткие (title, body) под лимиты APNs — главная защита от
обрезания, а не рефакторинг ради рефакторинга.

Тесты специально считают длину с ХУДШИМИ реалистичными значениями плейсхолдеров
(см. _WORST_PARAMS), а не с короткими примерами: пуш обязан не обрезаться даже
на самой длинной фразе/звании/имени упражнения, которые реально может отдать
engagement.py, а не только на счастливом случае из одного слова.
"""

import re

import pytest

import i18n
import push_ios
import push_texts

# asyncio_mode = auto (pyproject.toml) детектит async def сам — pytestmark
# тут не нужен, а на синхронных тестах этого файла (длина каталога) он бы
# только сыпал PytestWarning (см. tests/test_i18n_no_leaks.py, тот же приём).

# Худшие правдоподобные значения плейсхолдеров, которыми push_ios.ios_alert
# реально вызывается из engagement.py:
#   weeks       — "52 недели"/"52 weeks" (STREAK_MILESTONE_WEEKS доходит до 52
#                 в engagement.py; "недели" — самое длинное русское слово ветки)
#   days_left   — самый длинный вариант push.days_left.* в каждом языке
#   missing     — "3 тренировки"/"3 workouts" — короткая фраза-числительное
#   rank        — эмодзи + самое длинное название звания (analytics.RANKS):
#                 "Ветеран подвала"/"Basement Veteran"
#   exercise    — длиннее, чем push_ios._PARAM_CLIP_LIMIT позволит вставить —
#                 сам клип и должен победить, а не шаблон
#   tonnage     — "1234.5т"/"1234.5t" с запасом на большие числа
#   week_count  — "7 тренировок"/"7 workouts"
_WORST_PARAMS = {
    "ru": {
        "weeks": "52 недели",
        "days_left": "сегодня и завтра",
        "missing": "3 тренировки",
        "rank": "🦾 Ветеран подвала",
        "exercise": "Румынская становая тяга на одной ноге с гантелями",
        "tonnage": "1234.5т",
        "week_count": "7 тренировок",
    },
    "en": {
        "weeks": "52 weeks",
        "days_left": "today and tomorrow",
        "missing": "3 workouts",
        "rank": "🦾 Basement Veteran",
        "exercise": "Single-leg Romanian deadlift with dumbbells",
        "tonnage": "1234.5t",
        "week_count": "7 workouts",
    },
}


def _params_for(template: str, lang: str) -> dict:
    """Same substitution ios_alert would actually perform: exercise/rank go
    through push_ios._clip_param first (ios_alert always clips them — see
    push_ios._CLIPPED_PARAMS), the rest are used as-is. Testing the raw
    template against an unclipped worst-case value would fail on a case that
    can never happen in production and prove nothing."""
    names = set(re.findall(r"{(\w+)}", template))
    return {
        name: (
            push_ios._clip_param(_WORST_PARAMS[lang][name])
            if name in push_ios._CLIPPED_PARAMS
            else _WORST_PARAMS[lang][name]
        )
        for name in names
    }


def test_every_category_has_a_title_and_a_body_pool_in_both_languages():
    for category in push_ios.CATEGORIES:
        for lang in i18n.SUPPORTED:
            assert push_ios.TITLES_BY_LANG[lang][category]
            assert push_ios.BODY_POOLS_BY_LANG[lang][category]


def test_titles_fit_the_apns_title_budget():
    for category in push_ios.CATEGORIES:
        for lang in i18n.SUPPORTED:
            title = push_ios.TITLES_BY_LANG[lang][category]
            assert len(title) <= push_ios.TITLE_LIMIT, (category, lang, title, len(title))


def test_bodies_fit_the_apns_body_budget_even_with_worst_case_params():
    """The actual anti-truncation guarantee: every stored body template, filled
    with the longest realistic value for each of its placeholders, still fits
    inside BODY_LIMIT. A template that only fits with a short/lucky value would
    still get cut on a real push — that's exactly the bug this module exists to
    prevent, so the test must fail on that case, not just on an obviously huge
    string."""
    for category in push_ios.CATEGORIES:
        for lang in i18n.SUPPORTED:
            for template in push_ios.BODY_POOLS_BY_LANG[lang][category]:
                params = _params_for(template, lang)
                rendered = template.format(**params)
                assert len(rendered) <= push_ios.BODY_LIMIT, (
                    category, lang, template, rendered, len(rendered)
                )


def test_missing_short_pool_raises_instead_of_falling_back_to_the_long_text():
    """A category with no push.ios.* catalog entry must be a hard failure, not
    a silent fallback to push_texts' long Telegram copy — that fallback is
    exactly the truncation bug this module removes. We can't add a real fifth
    category without touching push_texts.py, so we point the loader at a
    category we know has no push.ios.* entry (its own long-form category name
    with a suffix nothing defines)."""
    with pytest.raises(KeyError):
        push_ios._load_title("ru", "does_not_exist")
    with pytest.raises(KeyError):
        push_ios._load_body_pool("ru", "does_not_exist")


async def test_ios_alert_substitutes_placeholders(fresh_db, user_id):
    title, body = await push_ios.ios_alert(
        user_id, push_texts.RANK_NEAR, "ru", rank="🦾 Ветеран подвала", missing="2 тренировки",
    )
    assert title == push_ios.TITLES_BY_LANG["ru"][push_texts.RANK_NEAR]
    assert "{rank}" not in body and "{missing}" not in body
    assert "Ветеран подвала" in body
    assert "2 тренировки" in body


async def test_ios_alert_clips_an_overlong_exercise_name(fresh_db, user_id):
    long_name = "Румынская становая тяга на одной ноге с гантелями и паузой"
    _title, body = await push_ios.ios_alert(
        user_id, push_texts.PLATEAU, "ru", exercise=long_name,
    )
    assert long_name not in body
    assert len(body) <= push_ios.BODY_LIMIT


async def test_ios_alert_unknown_language_falls_back_to_default(fresh_db, user_id):
    title, body = await push_ios.ios_alert(user_id, push_texts.WIN_BACK, "fr")
    assert title == push_ios.TITLES_BY_LANG[i18n.DEFAULT_LANG][push_texts.WIN_BACK]
    assert body in push_ios.BODY_POOLS_BY_LANG[i18n.DEFAULT_LANG][push_texts.WIN_BACK]


async def test_ai_weekly_reuses_the_weekly_digest_short_pool(fresh_db, user_id):
    """push_texts.AI_WEEKLY has no static Telegram copy (it's a model
    completion) and no push.ios.ai_weekly.* keys of its own — see push_ios.py's
    module docstring for why it aliases WEEKLY_DIGEST's short pool instead."""
    assert push_ios.TITLES_BY_LANG["ru"][push_texts.AI_WEEKLY] == (
        push_ios.TITLES_BY_LANG["ru"][push_texts.WEEKLY_DIGEST]
    )
    title, body = await push_ios.ios_alert(
        user_id, push_texts.AI_WEEKLY, "ru", tonnage="1.2т", week_count="3 тренировки",
    )
    assert title == push_ios.TITLES_BY_LANG["ru"][push_texts.WEEKLY_DIGEST]
    assert body in [
        t.format(tonnage="1.2т", week_count="3 тренировки")
        for t in push_ios.BODY_POOLS_BY_LANG["ru"][push_texts.WEEKLY_DIGEST]
    ]


async def test_body_pool_rotation_does_not_repeat_before_exhausted(fresh_db, user_id):
    pool = push_ios.BODY_POOLS_BY_LANG["ru"][push_texts.WIN_BACK]
    seen = [await push_ios._pick_ios_body(user_id, push_texts.WIN_BACK, "ru") for _ in range(len(pool))]
    assert sorted(seen) == sorted(pool)


async def test_ios_rotation_bag_is_isolated_from_telegram_rotation(fresh_db, user_id):
    """The ios pool and the push_texts pool for the same category are
    different sizes/content — sharing a rotation bag index would desync
    immediately. Exhausting the Telegram-side WIN_BACK bag must not perturb
    the ios-side bag for the same user+category."""
    tg_pool = push_texts.TEXTS[push_texts.WIN_BACK]
    for _ in range(len(tg_pool)):
        await push_texts.pick_text(user_id, push_texts.WIN_BACK)

    ios_pool = push_ios.BODY_POOLS_BY_LANG["ru"][push_texts.WIN_BACK]
    seen = [
        await push_ios._pick_ios_body(user_id, push_texts.WIN_BACK, "ru") for _ in range(len(ios_pool))
    ]
    assert sorted(seen) == sorted(ios_pool)
