"""Блок «Ближайшие» (achievements.nearest_progress + formatting.build_achievements_screen).

Три темы: какие значки попадают в тройку и в каком порядке (по доле
current/target — как analytics.rank_gap ранжирует оси звания), как фраза
согласуется по-русски и по-английски для каждого числового семейства (вес,
тоннаж, тренировки, недельная серия), и что булевы/разовые значки туда не
попадают вообще, а без данных блок просто не показывается — ничего не гадаем.
"""
import i18n
from achievements import AchievementContext, BadgeProgress, nearest_progress


def _ctx(**kw):
    base = dict(
        total_workouts=0, lifetime_tonnage_kg=0.0, best_week_streak=0,
        max_weight_kg=0.0, distinct_exercises=0,
    )
    base.update(kw)
    return AchievementContext(**base)


# ---------- отбор тройки ----------

def test_nearest_picks_three_closest_by_fraction():
    # 40/50 тренировок (0.8) обгоняет 130/140кг (0.93 — на самом деле выше,
    # пересчитаем осторожно): считаем долю честно и сверяем порядок по ней.
    ctx = _ctx(total_workouts=40, max_weight_kg=130, lifetime_tonnage_kg=45_000, best_week_streak=3)
    nearest = nearest_progress(ctx, earned=set())
    assert len(nearest) == 3
    fractions = [bp.current / bp.target for bp in nearest]
    assert fractions == sorted(fractions, reverse=True)


def test_nearest_excludes_already_earned_and_already_cleared_tiers():
    ctx = _ctx(total_workouts=100)  # уже покрывает first/w10/w25/w50/w100
    nearest = nearest_progress(ctx, earned={"first", "w10", "w25", "w50", "w100"})
    assert not any(bp.code in {"first", "w10", "w25", "w50", "w100"} for bp in nearest)
    # w200 — следующий порог, ещё не пройденный и не в earned.
    assert any(bp.code == "w200" for bp in nearest)


def test_nearest_limit_is_configurable():
    ctx = _ctx(total_workouts=9, max_weight_kg=95, lifetime_tonnage_kg=9_000, best_week_streak=3)
    assert len(nearest_progress(ctx, earned=set(), limit=1)) == 1
    assert len(nearest_progress(ctx, earned=set(), limit=2)) == 2


def test_nearest_excludes_boolean_and_one_off_badges():
    """superset1/early_bird/marathon/etc. не имеют "текущее/порог" и не должны
    попасть в «Ближайшие», даже когда контекст их почти открывает."""
    ctx = _ctx(
        total_workouts=1, distinct_groups=5, max_session_sets=24,
        max_session_tonnage_kg=4_999, max_session_exercises=7,
        has_superset=True, max_bodyweight_reps=24, early_workouts=9,
        bodyweight_logs=29, food_diary_best_run=6,
    )
    nearest = nearest_progress(ctx, earned=set())
    numeric_families = {"workouts", "weeks", "weight", "tonnage"}
    from achievements import FAMILY_BY_CODE
    assert all(FAMILY_BY_CODE[bp.code] in numeric_families for bp in nearest)


def test_nearest_confirmed_by_data_only_zero_is_excluded():
    """Ноль по оси — это "не знаем", не "0 из 10": семейство целиком выпадает,
    а не показывается с нулевым прогрессом."""
    ctx = _ctx(total_workouts=0, max_weight_kg=0, lifetime_tonnage_kg=0, best_week_streak=0)
    assert nearest_progress(ctx, earned=set()) == []


# ---------- фразы по семействам, RU/EN ----------

def _bp(code, current, target):
    return BadgeProgress(code=code, current=current, target=target)


def test_weight_family_phrasing_ru_en():
    import formatting
    bp = _bp("club140", current=125, target=140)
    with i18n.use_lang("ru"):
        line = formatting.format_badge_progress(bp)
        assert "Клуб 140" in line
        assert "15" in line and "кг" in line
    with i18n.use_lang("en"):
        line = formatting.format_badge_progress(bp)
        assert "15" in line and "kg" in line


def test_weeks_family_phrasing_is_x_of_y_ru_en():
    import formatting
    bp = _bp("streak12", current=4, target=12)
    with i18n.use_lang("ru"):
        line = formatting.format_badge_progress(bp)
        assert "4" in line and "12" in line and "из" in line
    with i18n.use_lang("en"):
        line = formatting.format_badge_progress(bp)
        assert "4" in line and "12" in line and "of" in line


def test_tonnage_family_phrasing_tons_ru_en():
    import formatting
    bp = _bp("ton50", current=46_800, target=50_000)  # ещё 3.2 т
    with i18n.use_lang("ru"):
        line = formatting.format_badge_progress(bp)
        assert "3.2" in line and "т" in line
    with i18n.use_lang("en"):
        line = formatting.format_badge_progress(bp)
        assert "3.2" in line and "t" in line


def test_tonnage_family_phrasing_falls_back_to_kg_under_a_hundred():
    import formatting
    bp = _bp("ton10", current=9_950, target=10_000)  # 50кг — меньше центнера
    with i18n.use_lang("ru"):
        line = formatting.format_badge_progress(bp)
        assert i18n.t("achievements.nearest_tons_weight", w="50кг") in line
        assert "50 т" not in line  # not misread as tons


def test_count_family_phrasing_ru_en():
    import formatting
    bp = _bp("w50", current=42, target=50)
    with i18n.use_lang("ru"):
        line = formatting.format_badge_progress(bp)
        assert "8" in line  # 50 - 42
    with i18n.use_lang("en"):
        line = formatting.format_badge_progress(bp)
        assert "8" in line and "workouts" in line


# ---------- экран целиком ----------

def test_screen_shows_nearest_block_when_ctx_given():
    import formatting
    ctx = _ctx(total_workouts=8)
    text = formatting.build_achievements_screen(set(), ctx)
    assert i18n.t("achievements.nearest_header") in text


def test_screen_has_no_nearest_block_without_ctx():
    import formatting
    text = formatting.build_achievements_screen(set())
    assert i18n.t("achievements.nearest_header") not in text


def test_screen_has_no_nearest_block_for_empty_user():
    """Совсем новый пользователь: ни одной подтверждённой цифры — блока нет,
    а не пустой заголовок или "0 из N"."""
    import formatting
    ctx = _ctx()  # всё по нулям — свежий /start
    text = formatting.build_achievements_screen(set(), ctx)
    assert i18n.t("achievements.nearest_header") not in text


def test_weight_family_remaining_is_shown_in_the_athlete_unit():
    """Пороги клубов считаются в кг, но остаток показывается в единицах атлета:
    у фунтового «Клуб 140 — ещё 40 кг» рядом с «220.5×90» — разнобой."""
    import formatting
    bp = _bp("club140", current=100.0, target=140.0)  # 40 кг ≈ 88 lb
    with i18n.use_lang("ru"):
        assert "ещё 40кг" in formatting.format_badge_progress(bp, "kg")
        assert "ещё 88lb" in formatting.format_badge_progress(bp, "lb")
    with i18n.use_lang("en"):
        assert "88lb to go" in formatting.format_badge_progress(bp, "lb")
