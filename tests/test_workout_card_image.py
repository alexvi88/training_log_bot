"""The shareable workout-card image draws text into a fixed-width figure and
nothing clips it, so an over-long line just ran off the right edge of what people
post. Notes were already wrapped; body lines were not.
"""
import datetime as dt

import charts
import formatting
from formatting import ExerciseBlockView


def test_long_set_line_is_wrapped_not_clipped():
    line = "  " + ", ".join(f"{100 + i * 2.5}×8" for i in range(12))
    assert len(line) > charts._CARD_BODY_WIDTH  # precondition: this used to clip

    chunks = charts._wrap_card_line(line)

    assert len(chunks) > 1
    assert all(len(c) <= charts._CARD_BODY_WIDTH for c in chunks)


def test_wrapped_set_line_keeps_its_indent_on_continuations():
    """Set lines are indented two spaces and styled by that indent, so a
    continuation that started at column 0 would be drawn as an exercise header."""
    line = "  " + ", ".join(f"{100 + i * 2.5}×8" for i in range(12))

    chunks = charts._wrap_card_line(line)

    assert all(c.startswith("  ") for c in chunks)


def test_short_line_is_left_exactly_as_is():
    assert charts._wrap_card_line("  100×8, 95×8") == ["  100×8, 95×8"]
    assert charts._wrap_card_line("Жим лёжа [ГРУДЬ]") == ["Жим лёжа [ГРУДЬ]"]


def test_card_renders_with_content_that_used_to_overflow():
    blocks = [
        ExerciseBlockView(
            group_name="ноги",
            exercise_name="leg press - very long machine name here",
            sets=[(100.0 + i * 2.5, 8) for i in range(12)],
        )
    ]
    title, body, footer, note = formatting.build_workout_card(
        dt.datetime(2026, 7, 26, 13), blocks
    )

    png = charts.render_workout_card(title, body, footer, note)

    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_every_rendered_row_fits_the_card_width():
    """End to end: collapsing plus wrapping means nothing reaches the renderer
    still too wide."""
    blocks = [
        ExerciseBlockView(
            group_name="грудь", exercise_name="chest press - horizontal machine",
            sets=[(83.6, 8)] * 10,
        ),
        ExerciseBlockView(
            group_name="спина", exercise_name="seated row - 1hand - cable",
            sets=[(50.0 + i * 2.5, 10 - i) for i in range(6)],
        ),
    ]
    _title, body, _footer, _note = formatting.build_workout_card(
        dt.datetime(2026, 7, 26, 13), blocks
    )

    for line in body:
        for chunk in charts._wrap_card_line(line):
            assert len(chunk) <= charts._CARD_BODY_WIDTH
