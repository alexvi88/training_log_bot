import asyncio
import logging

import pytest

import i18n


@pytest.fixture(autouse=True)
def _reset_i18n():
    # Каждый тест должен видеть чистые кэши каталогов/AST и список WARNING'ов,
    # иначе тесты на "залогировать один раз" будут зависеть от порядка запуска.
    i18n.reload()
    token = i18n.current_lang.set(i18n.DEFAULT_LANG)
    yield
    i18n.current_lang.reset(token)
    i18n.reload()


# --- normalize ---------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    ["ru", "RU", "ru-RU", "uk", "be", "kk", "ky", "hy", "az", "uz", "UZ-uz", None, ""],
)
def test_normalize_to_ru(code):
    assert i18n.normalize(code) == "ru"


@pytest.mark.parametrize("code", ["en", "en-US", "EN", "de", "fr-FR", "es", "zh-Hans"])
def test_normalize_to_en(code):
    assert i18n.normalize(code) == "en"


# --- плюрализация ru/en -------------------------------------------------------


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, "1 подход"),
        (2, "2 подхода"),
        (5, "5 подходов"),
        (11, "11 подходов"),
        (21, "21 подход"),
        (22, "22 подхода"),
        (25, "25 подходов"),
        (101, "101 подход"),
        (111, "111 подходов"),
    ],
)
def test_plural_ru(n, expected):
    assert i18n.t_in("ru", "test.sets", n=n) == expected


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, "1 set"),
        (2, "2 sets"),
        (0, "0 sets"),
    ],
)
def test_plural_en(n, expected):
    assert i18n.t_in("en", "test.sets", n=n) == expected


def test_hash_is_the_number():
    assert i18n.t_in("ru", "test.sets", n=3) == "3 подхода"


# --- select и вложенные плейсхолдеры ------------------------------------------


def test_select_male_female_other():
    assert i18n.t_in("ru", "test.greeting_gendered", g="male") == "Он сделал тренировку"
    assert i18n.t_in("ru", "test.greeting_gendered", g="female") == "Она сделала тренировку"
    assert i18n.t_in("ru", "test.greeting_gendered", g="unknown") == "Атлет сделал тренировку"


def test_nested_placeholder_inside_plural_branch():
    i18n._catalogs["ru"] = {"custom.nested": "{n, plural, one{# подход, {name}} other{# подходов, {name}}}"}
    assert i18n.t_in("ru", "custom.nested", n=1, name="жим") == "1 подход, жим"
    assert i18n.t_in("ru", "custom.nested", n=5, name="жим") == "5 подходов, жим"


def test_simple_substitution():
    assert i18n.t_in("ru", "test.hello", name="Атлет") == "Привет, Атлет!"
    assert i18n.t_in("en", "test.hello", name="Athlete") == "Hello, Athlete!"


# --- ICU-кавычки ---------------------------------------------------------------


def test_icu_quotes_literal_apostrophe():
    i18n._catalogs["ru"] = {"custom.quote": "Атлет''а сила"}
    assert i18n.t_in("ru", "custom.quote") == "Атлет'а сила"


def test_icu_quotes_literal_braces():
    i18n._catalogs["ru"] = {"custom.braces": "Формат: '{'name'}' и '{'n'}'"}
    assert i18n.t_in("ru", "custom.braces") == "Формат: {name} и {n}"


# --- фолбэк языка и отсутствие ключа --------------------------------------------


def test_fallback_to_ru_when_missing_in_en():
    # test.ru_only есть только в locales/ru.json
    assert i18n.t_in("en", "test.ru_only") == "Только в русском каталоге"


def test_missing_key_everywhere_returns_key_itself():
    assert i18n.t_in("ru", "no.such.key.at.all") == "no.such.key.at.all"


def test_missing_key_warns_once(caplog):
    with caplog.at_level(logging.WARNING, logger="i18n"):
        i18n.t_in("ru", "totally.missing")
        i18n.t_in("ru", "totally.missing")
        i18n.t_in("en", "totally.missing")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


# --- отсутствующий параметр -----------------------------------------------------


def test_missing_param_raises_keyerror():
    with pytest.raises(KeyError):
        i18n.t_in("ru", "test.hello")


def test_missing_plural_param_raises_keyerror():
    with pytest.raises(KeyError):
        i18n.t_in("ru", "test.sets")


# --- ValueError на каталоге без 'other' ------------------------------------------


def test_plural_without_other_fallback_raises_value_error():
    i18n._catalogs["ru"] = {"custom.bad": "{n, plural, one{штука}}"}
    with pytest.raises(ValueError):
        i18n.t_in("ru", "custom.bad", n=5)


# --- use_lang / контекст --------------------------------------------------------


def test_use_lang_context_manager_restores_previous():
    i18n.set_lang("ru")
    assert i18n.get_lang() == "ru"
    with i18n.use_lang("en") as previous:
        assert previous == "ru"
        assert i18n.get_lang() == "en"
    assert i18n.get_lang() == "ru"


def test_use_lang_unknown_normalizes_to_default():
    with i18n.use_lang("xx"):
        assert i18n.get_lang() == i18n.DEFAULT_LANG


def test_set_lang_unknown_normalizes_to_default():
    i18n.set_lang("xx")
    assert i18n.get_lang() == i18n.DEFAULT_LANG
    i18n.set_lang("ru")


def test_t_uses_current_lang():
    with i18n.use_lang("en"):
        assert i18n.t("test.hello", name="X") == "Hello, X!"
    with i18n.use_lang("ru"):
        assert i18n.t("test.hello", name="X") == "Привет, X!"


# --- изоляция ContextVar между asyncio-задачами ----------------------------------


def test_context_var_does_not_leak_between_tasks():
    results = {}

    async def set_and_check(lang, key):
        i18n.set_lang(lang)
        # Отдаём управление циклу событий, чтобы вторая задача успела вклиниться —
        # именно тут ContextVar потёк бы, будь он общей переменной модуля.
        await asyncio.sleep(0)
        results[key] = i18n.get_lang()

    async def run_both():
        await asyncio.gather(set_and_check("ru", "a"), set_and_check("en", "b"))

    asyncio.run(run_both())
    assert results == {"a": "ru", "b": "en"}


# --- апостроф в английском тексте ------------------------------------------------
#
# Тон-оф-войс делает сокращения обязательными для английского голоса («don't»,
# «you're», «that's»), поэтому апостроф в каталоге — массовый символ, а не
# редкий случай. По ICU кавычку открывает только апостроф перед `{`, `}`, `#`
# или `|`; трактовка любого апострофа как кавычки съедала текст до следующего
# апострофа или до конца строки.


def _render(template, **params):
    i18n.reload()
    i18n._catalogs["ru"] = {"tmp.key": template}
    return i18n.t_in("ru", "tmp.key", **params)


def test_lone_apostrophe_is_literal():
    assert _render("That's a big jump — extra zero?") == "That's a big jump — extra zero?"


def test_two_apostrophes_do_not_swallow_text_between_them():
    # Худший случай: два сокращения в одной строке. Раньше всё между ними
    # считалось содержимым кавычек, и «t a number, like 80. Don» исчезало.
    assert _render("Didn't a number, like 80. Don't skip") == "Didn't a number, like 80. Don't skip"


def test_apostrophe_before_brace_still_quotes():
    assert _render("literal '{'not a var'}'") == "literal {not a var}"


def test_doubled_apostrophe_is_one_apostrophe():
    assert _render("it''s fine") == "it's fine"


def test_apostrophe_survives_inside_plural_branch():
    template = "{n, plural, one{you're one set in} other{you're # sets in}}"
    assert _render(template, n=1) == "you're one set in"
    assert _render(template, n=5) == "you're 5 sets in"


def test_apostrophe_does_not_break_placeholder_parsing():
    assert _render("Can't find weight in {chunk}", chunk="abc") == "Can't find weight in abc"


# --- пустая ветка ----------------------------------------------------------------


def test_empty_branch_is_not_replaced_by_other():
    # `one{}` — законный приём: в английском так гасят слово целиком.
    # Пустой список веток ложный, и `or` подменял его веткой other.
    template = "{n, plural, one{} other{s}}"
    assert _render(template, n=1) == ""
    assert _render(template, n=2) == "s"
