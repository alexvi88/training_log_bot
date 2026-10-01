"""Тоннаж в фунтах не превращается в метрические тонны: «7,050lb», не «3.2t».

Килограммы при этом не трогаем — от 1000 кг по-прежнему «3,2 тонны»/«т».
"""
import analytics
import engagement
import formatting
import i18n

NBSP = " "


def test_lb_tonnage_is_grouped_pounds_in_english():
    with i18n.use_lang("en"):
        assert formatting.format_tonnage(7050, "lb") == "7,050lb"
        assert formatting.format_tonnage(800, "lb") == "800lb"
        assert formatting.format_tonnage(7_050_000, "lb") == "7,050,000lb"


def test_lb_tonnage_is_grouped_pounds_in_russian():
    with i18n.use_lang("ru"):
        assert formatting.format_tonnage(7050, "lb") == f"7{NBSP}050lb"


def test_kg_tonnage_still_switches_to_tons():
    with i18n.use_lang("en"):
        assert formatting.format_tonnage(3200, "kg") == "3.2 tons"
        assert formatting.format_tonnage(800, "kg") == "800kg"
    with i18n.use_lang("ru"):
        assert formatting.format_tonnage(3200, "kg").startswith("3.2 тонны")


def test_hall_of_fame_lifetime_line_in_lb_has_no_tons():
    for lang, expected in (("en", "125,000lb"), ("ru", f"125{NBSP}000lb")):
        with i18n.use_lang(lang):
            text = formatting.build_hall_of_fame(
                total_workouts=42, tonnage_kg=125000, tonnage_equivalent=None,
                best_week_streak=1, longest_workout_seconds=60, top_lifts=[], unit="lb",
            )
        assert expected in text
        assert "ton" not in text.lower() and "тонн" not in text


def test_hall_of_fame_lifetime_line_in_kg_keeps_tons():
    with i18n.use_lang("en"):
        text = formatting.build_hall_of_fame(
            total_workouts=42, tonnage_kg=125000, tonnage_equivalent=None,
            best_week_streak=1, longest_workout_seconds=60, top_lifts=[], unit="kg",
        )
    assert "125 tons" in text


def test_push_digest_tonnage_in_lb_is_pounds():
    with i18n.use_lang("en"):
        assert engagement.format_tonnage(7050, "lb") == "7,050lb"
        assert engagement.format_tonnage(4200) == "4.2t"
        assert engagement.format_tonnage(850) == "850kg"


def test_rank_gap_in_lb_is_pounds_and_in_kg_is_tons():
    gap = analytics.RankGap("tonnage", 3000.0)  # кг
    with i18n.use_lang("en"):
        assert formatting.format_rank_gap(gap, "lb") == "6,614lb to go"
        assert formatting.format_rank_gap(gap) == "3t to go"
    with i18n.use_lang("ru"):
        assert formatting.format_rank_gap(gap, "lb") == f"ещё 6{NBSP}614lb"
        assert formatting.format_rank_gap(gap) == "ещё 3т"


def test_rank_ladder_in_lb_has_no_metric_tons():
    with i18n.use_lang("en"):
        text = formatting.build_rank_ladder(
            analytics.RANKS, analytics.RANKS[1], None,
            total_workouts=10, tonnage_kg=1000.0, per_week=2.0, unit="lb",
        )
    assert "2,205lb" in text
    assert "0.0t" not in text and "1.0t" not in text


def test_badge_remaining_in_lb_is_pounds_and_in_kg_is_tons():
    import achievements

    code = next(c for c, f in achievements.FAMILY_BY_CODE.items() if f == "tonnage")
    bp = achievements.BadgeProgress(code, 0.0, 5000.0)
    with i18n.use_lang("en"):
        assert formatting.badge_remaining_text(bp, "lb") == "11,023lb to go"
        assert formatting.badge_remaining_text(bp, "kg") == "5t to go"
