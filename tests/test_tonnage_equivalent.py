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
    assert formatting.format_tonnage_equivalent(200, seed=0) == "Это как 2 сенбернара 🐺"
    assert formatting.format_tonnage_equivalent(750, seed=0) == "Это как 9 сенбернаров 🐺"
    assert formatting.format_tonnage_equivalent(125000, seed=1) == "Это как 25 слонов 🐘"


def test_tonnage_in_pounds_never_becomes_tons():
    """Tonnage arrives in the user's own unit; kg switches to tons at 1000 kg,
    lb stays in grouped pounds at any size."""
    assert formatting.format_tonnage(20000, "kg").startswith("20 тонн")
    assert formatting.format_tonnage(20000, "lb") == "20\u00a0000 lb"


def test_sub_ton_totals_stay_in_the_users_own_unit():
    assert formatting.format_tonnage(800, "lb") == "800 lb"
    assert formatting.format_tonnage(800, "kg") == "800кг"


def test_equivalents_count_real_objects_not_inflated_pounds():
    """The comparison objects weigh what they weigh, so counting pounds against
    them inflated every lb user's comparison by 2.2×."""
    kg = formatting.format_tonnage_equivalent(2000, seed=0, unit="kg")
    lb = formatting.format_tonnage_equivalent(2000, seed=0, unit="lb")
    assert kg is not None and lb is not None

    kg_count = int(kg.split()[2])
    lb_count = int(lb.split()[2])
    assert kg_count > lb_count
