"""voice_parse.transcript_to_sets_line — spoken set → parser-ready line."""
import pytest

from parser import parse_sets_line
from voice_parse import transcript_to_sets_line, transcript_to_sets_line_with_hint


@pytest.mark.parametrize(
    "text,expected",
    [
        ("сто на восемь", "100 8"),
        ("100 на 8", "100 8"),
        ("100 8", "100 8"),
        ("сто двадцать на пять", "120 5"),
        ("восемьдесят пять на десять", "85 10"),
        ("девяносто на восемь раз", "90 8"),
        ("сто на восемь три подхода", "100 8"),  # set count dropped
        ("двенадцать", "12"),  # bodyweight bare reps
        ("сто на восемь, сто на семь", "100 8, 100 7"),
        ("сто на восемь потом девяносто на восемь", "100 8, 90 8"),
        ("двести на три", "200 3"),
    ],
)
def test_transcripts_map_to_lines(text, expected):
    assert transcript_to_sets_line(text) == expected


def test_no_numbers_returns_none():
    assert transcript_to_sets_line("давай запиши подход") is None
    assert transcript_to_sets_line("") is None


def test_dropped_set_count_is_flagged():
    """Регрессия: «сто на восемь три подхода» молча пишет один подход — вызывающий
    должен узнать об этом и предупредить человека, а не сделать вид, что записал все."""
    line, dropped = transcript_to_sets_line_with_hint("сто на восемь три подхода")
    assert line == "100 8"
    assert dropped is True


def test_no_dropped_hint_for_a_plain_weight_and_reps():
    line, dropped = transcript_to_sets_line_with_hint("сто на восемь")
    assert line == "100 8"
    assert dropped is False


def test_output_is_parseable():
    line = transcript_to_sets_line("сто на восемь, девяносто на восемь")
    parsed = parse_sets_line(line)
    assert [(s.weight, s.reps) for s in parsed] == [(100.0, 8), (90.0, 8)]


def test_exercise_name_prefix_is_ignored():
    assert transcript_to_sets_line("жим сто на восемь") == "100 8"


# ---------- fractional weight: plate-loaded stacks are commonly X.Y kg ----------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("девяносто семь и шесть на восемь", "97.6 8"),
        ("тридцать девять и три на десять", "39.3 10"),
        ("восемьдесят три и шесть на пять", "83.6 5"),
        ("девяносто семь запятая шесть на восемь", "97.6 8"),
        ("девяносто семь точка шесть на восемь", "97.6 8"),
        ("два с половиной на десять", "2.5 10"),
        ("сто и пять на восемь", "100.5 8"),
    ],
)
def test_fractional_weight_transcripts(text, expected):
    assert transcript_to_sets_line(text) == expected


def test_fractional_weight_is_actually_parseable():
    """The whole point: this has to survive parser.parse_sets_line, not just
    look right as a string."""
    line = transcript_to_sets_line("девяносто семь и шесть на восемь")
    parsed = parse_sets_line(line)
    assert [(s.weight, s.reps) for s in parsed] == [(97.6, 8)]


def test_before_the_fix_reps_would_have_been_silently_dropped():
    """Regression guard for the actual failure mode: "и" used to be a plain
    flush boundary, so "97 и 6 на 8" produced two top-level numbers (97, 6)
    *before* "8" was reached, and only the first two numbers of a chunk are
    kept — the real reps count vanished rather than merely mis-parsing the
    weight."""
    line = transcript_to_sets_line("девяносто семь и шесть на восемь")
    assert line == "97.6 8"
    assert "8" in line.split()  # reps still present


def test_bare_half_word_without_a_preceding_number_does_not_crash():
    # "половиной" with nothing to attach to — must not raise or fabricate 0.5.
    assert transcript_to_sets_line("половиной на восемь") == "8"


def test_decimal_marker_falls_back_to_boundary_when_nothing_precedes_it():
    assert transcript_to_sets_line("и восемь") == "8"


def test_stray_word_after_decimal_marker_does_not_crash():
    """"и" followed by something that isn't a plausible fraction digit (an
    exercise name, filler) must fall back gracefully rather than raise."""
    assert transcript_to_sets_line("сто и жим на восемь") == "100 8"
