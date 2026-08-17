"""English speech-to-set parsing (задание: голосовой ввод молчал по-английски),
plюс регрессия на то, что русский голос по-прежнему разбирается как раньше, и
что тексты ошибок parser.py на lang="en" не содержат кириллицы.

voice_parse распознаёт оба словаря числительных всегда, без переключения по
i18n.get_lang() — см. docstring voice_parse.py. Поэтому здесь не нужен
i18n.use_lang() для самого voice_parse; use_lang нужен только там, где мы
проверяем parser.py (его тексты ошибок реально берутся из каталога по
текущему языку).
"""

import re

import pytest

import i18n
from parser import ParseError, parse_bodyweight, parse_ru_date, parse_sets_line
from voice_parse import transcript_to_sets_line, transcript_to_sets_line_with_hint

_CYRILLIC_RE = re.compile("[а-яёА-ЯЁ]")


@pytest.fixture(autouse=True)
def _reset_i18n():
    i18n.reload()
    yield
    i18n.reload()


# ---------- English gym speech -> parser-ready line ----------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("one hundred eight", "108"),
        ("one hundred for eight", "100 8"),
        ("two twenty five for five", "225 5"),
        ("two twenty five by eight", "225 8"),
        ("bodyweight twelve", "12"),
        ("body weight twelve", "12"),
        ("bw twelve", "12"),
        ("one thirty for ten", "130 10"),
        ("one oh five for eight", "105 8"),
        ("a hundred and five for eight", "105 8"),
        ("twenty-five for ten", "25 10"),
        ("two twenty five by eight three sets", "225 8"),  # trailing count dropped
        ("two hundred and twenty five times five", "225 5"),
        ("ninety seven point six for eight", "97.6 8"),
    ],
)
def test_english_transcripts_map_to_lines(text, expected):
    assert transcript_to_sets_line(text) == expected


def test_english_output_is_parseable():
    line = transcript_to_sets_line("two twenty five for five")
    parsed = parse_sets_line(line)
    assert [(s.weight, s.reps) for s in parsed] == [(225.0, 5)]


def test_english_dropped_set_count_is_flagged():
    line, dropped = transcript_to_sets_line_with_hint("one hundred for eight three sets")
    assert line == "100 8"
    assert dropped is True


def test_english_then_splits_into_separate_sets():
    assert transcript_to_sets_line("one hundred for eight then ninety for eight") == "100 8, 90 8"


def test_english_exercise_name_prefix_is_ignored():
    assert transcript_to_sets_line("bench press one hundred for eight") == "100 8"


# ---------- Russian speech still parses exactly as before (regression) ----------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("сто на восемь", "100 8"),
        ("сто двадцать на пять", "120 5"),
        ("сто на восемь три подхода", "100 8"),
        ("двенадцать", "12"),
        ("сто на восемь потом девяносто на восемь", "100 8, 90 8"),
        ("девяносто семь и шесть на восемь", "97.6 8"),
    ],
)
def test_russian_transcripts_are_unaffected(text, expected):
    assert transcript_to_sets_line(text) == expected


def test_russian_with_stray_english_word_still_works():
    # A Russian-speaking lifter dropping in an English exercise name is exactly
    # the case the "always try both" design is meant to survive.
    assert transcript_to_sets_line("bench press сто на восемь") == "100 8"


# ---------- parser.py error texts under lang="en" have no Cyrillic ----------


def _all_english_parse_errors() -> list[str]:
    messages: list[str] = []
    calls = [
        lambda: parse_sets_line(""),
        lambda: parse_sets_line("abc"),
        lambda: parse_sets_line("100 0"),
        lambda: parse_sets_line("100 99999"),
        lambda: parse_sets_line("99999 8"),
        lambda: parse_sets_line("100x8x99"),
        lambda: parse_sets_line("100x8@99"),
        lambda: parse_bodyweight("abc"),
        lambda: parse_bodyweight("50000"),
        lambda: parse_ru_date("not a date"),
        lambda: parse_ru_date("31.02.2025"),
        lambda: parse_ru_date("01.01.2999"),
    ]
    for call in calls:
        with pytest.raises(ParseError) as excinfo:
            call()
        messages.append(excinfo.value.message)
    return messages


def test_parser_errors_in_english_have_no_cyrillic():
    with i18n.use_lang("en"):
        for message in _all_english_parse_errors():
            assert not _CYRILLIC_RE.search(message), message


def test_parser_errors_in_russian_still_have_cyrillic():
    # Опора теста выше: если бы каталог молча остался пустым для lang="en" и
    # i18n падал в ru-fallback, предыдущий тест зелёным бы прошёл по ошибке.
    with i18n.use_lang("ru"):
        for message in _all_english_parse_errors():
            assert _CYRILLIC_RE.search(message), message


def test_date_error_names_the_expected_format_in_english():
    # Формат дд.мм.гггг сохранён для обоих языков (не угадываем мм.дд по
    # языку — см. комментарий у parse_ru_date), поэтому подсказка в английской
    # ошибке обязана явно называть порядок day/month/year.
    with i18n.use_lang("en"), pytest.raises(ParseError) as excinfo:
        parse_ru_date("not a date")
    assert "DD.MM.YYYY" in excinfo.value.message
