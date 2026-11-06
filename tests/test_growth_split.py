"""/growth длиннее 4096 символов уходит несколькими сообщениями, а не падает."""

from handlers.admin import _pack_sections


def test_long_report_is_packed_by_sections_under_limit():
    sections = [f"<b>Раздел {i}</b>\n" + "\n".join(f"строка {i}.{j}" for j in range(60)) for i in range(8)]
    text = "\n\n".join(sections)
    assert len(text) > 4096
    chunks = _pack_sections(text, 4096)
    assert len(chunks) > 1
    assert all(0 < len(c) <= 4096 for c in chunks)
    assert "\n\n".join(chunks) == text
    # Раздел не рвётся между сообщениями: открывающий тег и его строки — вместе.
    for chunk in chunks:
        assert chunk.count("<b>") == chunk.count("</b>")


def test_oversized_section_is_split_by_lines():
    text = "\n".join(f"line {i}" for i in range(2000))
    chunks = _pack_sections(text, 4096)
    assert all(len(c) <= 4096 for c in chunks)
    assert "\n".join(chunks) == text


def test_short_report_is_one_message():
    assert _pack_sections("a\n\nb", 4096) == ["a\n\nb"]
