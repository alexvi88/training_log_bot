"""format_tonnage_equivalent — the playful "N объектов" comparison clause."""
import formatting


def test_small_tonnage_returns_none():
    assert formatting.format_tonnage_equivalent(50) is None
    assert formatting.format_tonnage_equivalent(0) is None


def test_typical_session_names_a_believable_count():
    line = formatting.format_tonnage_equivalent(6400, seed=0)
    assert line is not None
    assert line.startswith("Это как ")


def test_seed_rotates_the_chosen_object():
    seen = {formatting.format_tonnage_equivalent(6400, seed=s) for s in range(6)}
    # Several distinct comparisons are reachable by varying the seed.
    assert len(seen) >= 2


def test_count_is_always_in_a_sane_range():
    for kg in (200, 500, 1500, 6400, 20000, 80000):
        line = formatting.format_tonnage_equivalent(kg, seed=kg)
        assert line is not None
        count = int(line.split("Это как ")[1].split(" ")[0])
        assert 1 <= count <= 40


def test_declension_matches_count():
    assert formatting.format_tonnage_equivalent(200, seed=0) == "Это как 2 сенбернара 🐺."
    assert formatting.format_tonnage_equivalent(750, seed=0) == "Это как 9 сенбернаров 🐺."
    assert formatting.format_tonnage_equivalent(125000, seed=1) == "Это как 25 слонов 🐘."
