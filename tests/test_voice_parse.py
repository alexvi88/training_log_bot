"""voice_parse.transcript_to_sets_line — spoken set → parser-ready line."""
import pytest

from parser import parse_sets_line
from voice_parse import transcript_to_sets_line


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


def test_output_is_parseable():
    line = transcript_to_sets_line("сто на восемь, девяносто на восемь")
    parsed = parse_sets_line(line)
    assert [(s.weight, s.reps) for s in parsed] == [(100.0, 8), (90.0, 8)]


def test_exercise_name_prefix_is_ignored():
    assert transcript_to_sets_line("жим сто на восемь") == "100 8"
