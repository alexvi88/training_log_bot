"""QR-кодировщик (qr_code.py) против эталонов библиотеки `qrcode`.

Эталоны сняты офлайн (`qrcode` 8.2, уровень M, байтовый режим, маска задана
явно) и лежат в `tests/qr_fixtures.json`: зависимость в прод и в CI не нужна,
а сверка побитная — вместе с форматом, версией и выравниванием.
"""

import json
import pathlib
import re

import pytest

import qr_code

_FIXTURES = json.loads((pathlib.Path(__file__).parent / "qr_fixtures.json").read_text(encoding="utf-8"))


def _rows(fixture):
    size = len(fixture["rows"])
    return [[c == "1" for c in bin(int(h, 16))[2:].zfill(size)] for h in fixture["rows"]]


@pytest.mark.parametrize("fixture", _FIXTURES, ids=lambda f: f"{f['text'][:20]}-m{f['mask']}")
def test_matrix_matches_reference_bit_for_bit(fixture):
    assert qr_code.matrix(fixture["text"], mask=fixture["mask"]) == _rows(fixture)


def test_auto_mask_is_one_of_the_references():
    for fixture in _FIXTURES:
        if fixture["auto"]:
            assert qr_code.matrix(fixture["text"]) == _rows(fixture)


def test_auto_mask_has_the_lowest_penalty():
    text = "https://apps.apple.com/app/id6815211995"
    scores = [qr_code._penalty(qr_code.matrix(text, mask=m)) for m in range(8)]
    assert qr_code._penalty(qr_code.matrix(text)) == min(scores)


def test_versions_grow_with_length_and_overflow_is_an_error():
    assert len(qr_code.matrix("A")) == 21
    assert len(qr_code.matrix("z" * 200)) == 57
    with pytest.raises(ValueError):
        qr_code.matrix("z" * 300)


def test_svg_is_well_formed_and_square():
    svg = qr_code.svg("https://apps.apple.com/app/id6815211995")
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 37 37"')
    assert svg.endswith("</svg>")
    path = re.search(r'd="([^"]+)"', svg).group(1)
    # Число тёмных модулей в path совпадает с матрицей.
    dark = sum(int(w) for w in re.findall(r"h(\d+)", path))
    assert dark == sum(map(sum, qr_code.matrix("https://apps.apple.com/app/id6815211995")))
