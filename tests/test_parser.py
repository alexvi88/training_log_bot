"""parse_single_token (weight/reps/count free-text) and parse_ru_date."""

import datetime as dt
import re

import pytest

from parser import EXAMPLES_HINT, ParsedSet, ParseError, parse_ru_date, parse_single_token

_HINT_RE = re.escape(EXAMPLES_HINT)


# ---------- parse_single_token: bodyweight (bare reps) ----------


def test_bodyweight_bare_reps():
    result = parse_single_token("8")
    assert result == [ParsedSet(weight=0.0, reps=8, weight_omitted=True)]


def test_bodyweight_zero_reps_rejected():
    with pytest.raises(ParseError, match="больше 0"):
        parse_single_token("0")


# ---------- parse_single_token: "x"-style separators ----------


@pytest.mark.parametrize("sep", ["x", "X", "х", "Х", "*", "/"])
def test_x_separator_variants(sep):
    result = parse_single_token(f"100{sep}8")
    assert result == [ParsedSet(weight=100.0, reps=8)]


def test_x_separator_with_count():
    result = parse_single_token("100x8x3")
    assert result == [ParsedSet(weight=100.0, reps=8)] * 3
    assert len(result) == 3


def test_x_separator_tolerates_surrounding_spaces():
    result = parse_single_token("100 x 8 x 3")
    assert result == [ParsedSet(weight=100.0, reps=8)] * 3


# ---------- parse_single_token: space-separated form ----------


def test_space_separator_weight_reps():
    result = parse_single_token("100 8")
    assert result == [ParsedSet(weight=100.0, reps=8)]


def test_space_separator_weight_reps_count():
    result = parse_single_token("100 8 3")
    assert result == [ParsedSet(weight=100.0, reps=8)] * 3


def test_space_separator_collapses_extra_whitespace():
    result = parse_single_token("100   8")
    assert result == [ParsedSet(weight=100.0, reps=8)]


# ---------- parse_single_token: weight formats ----------


def test_decimal_weight_with_dot():
    result = parse_single_token("100.5x8")
    assert result[0].weight == 100.5


def test_decimal_weight_with_comma():
    result = parse_single_token("100,5x8")
    assert result[0].weight == 100.5


def test_plus_prefixed_weight():
    result = parse_single_token("+20 8")
    assert result == [ParsedSet(weight=20.0, reps=8)]


def test_strips_surrounding_whitespace():
    result = parse_single_token("  100x8  ")
    assert result == [ParsedSet(weight=100.0, reps=8)]


# ---------- parse_single_token: validation errors ----------


def test_empty_token_raises():
    with pytest.raises(ParseError, match=_HINT_RE):
        parse_single_token("")


def test_whitespace_only_token_raises():
    with pytest.raises(ParseError, match=_HINT_RE):
        parse_single_token("   ")


def test_garbage_token_raises():
    with pytest.raises(ParseError, match=_HINT_RE):
        parse_single_token("abc")


def test_negative_numbers_are_unparseable():
    with pytest.raises(ParseError, match=_HINT_RE):
        parse_single_token("-5")


def test_zero_reps_rejected_in_weight_form():
    with pytest.raises(ParseError, match="больше 0"):
        parse_single_token("100x0")


def test_count_at_max_boundary_is_accepted():
    result = parse_single_token("100x8x20")
    assert len(result) == 20


def test_count_over_max_rejected():
    with pytest.raises(ParseError, match="Странное количество"):
        parse_single_token("100x8x21")


def test_count_zero_rejected():
    with pytest.raises(ParseError, match="Странное количество"):
        parse_single_token("100x8x0")


# ---------- parse_ru_date ----------


@pytest.mark.parametrize("sep", [".", "-", "/"])
def test_date_separator_variants(sep):
    text = f"14{sep}03{sep}2025"
    assert parse_ru_date(text) == dt.date(2025, 3, 14)


def test_date_accepts_single_digit_day_and_month():
    assert parse_ru_date("1.1.2025") == dt.date(2025, 1, 1)


def test_date_two_digit_year_expands_to_2000s():
    assert parse_ru_date("01.01.05") == dt.date(2005, 1, 1)


def test_date_strips_whitespace():
    assert parse_ru_date("  14.03.2025  ") == dt.date(2025, 3, 14)


def test_date_invalid_calendar_date_raises():
    with pytest.raises(ParseError, match="не существует"):
        parse_ru_date("31.02.2025")


def test_date_garbage_raises():
    with pytest.raises(ParseError, match="Не понял дату"):
        parse_ru_date("not a date")


def test_date_today_is_accepted():
    today = dt.date.today()
    assert parse_ru_date(today.strftime("%d.%m.%Y")) == today


def test_date_in_future_is_rejected():
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    with pytest.raises(ParseError, match="будущем"):
        parse_ru_date(tomorrow.strftime("%d.%m.%Y"))
