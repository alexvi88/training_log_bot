"""Блок «Ближайшие» (achievements.nearest_progress + formatting.build_achievements_screen).

Три темы: какие значки попадают в тройку и в каком порядке (по доле
current/target — как analytics.rank_gap ранжирует оси звания), как фраза
согласуется по-русски и по-английски для каждого числового семейства (вес,
тоннаж, тренировки, «X из Y» для остальных счётных), и что булевы/разовые значки туда не
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
    import datetime as dt
    ctx = _ctx(
        total_workouts=1, has_superset=True, has_weekend_pair=True,
        all_weekdays_covered=True, has_dec31=True,
        workout_start_hour=6, workout_date=dt.date(2026, 1, 1),
        workout_duration_seconds=3 * 3600,
    )
    codes = {bp.code for bp in nearest_progress(ctx, earned=set(), limit=50)}
    one_off = {
        "superset1", "weekend_double", "all_weekdays", "early_bird",
        "night_owl", "marathon", "new_year", "dec31",
    }
    assert not codes & one_off


def test_food7_stays_out_of_nearest():
    """«Неделя учёта» — единственный числовой значок вне «Ближайших»: тот же
    список уходит в iOS-приложение, где дневника еды нет."""
    ctx = _ctx(total_workouts=1, food_diary_best_run=6)
    codes = {bp.code for bp in nearest_progress(ctx, earned=set(), limit=50)}
    assert "food7" not in codes


# Код → (поле контекста, порог). Пороги — те же, что в earned_codes; сверка с
# ним ниже, чтобы «X из Y» не разошлось с тем, когда значок реально дают.
_COUNTABLE = {
    "variety20": ("distinct_exercises", 20),
    "variety50": ("distinct_exercises", 50),
    "groups6": ("distinct_groups", 6),
    "vol25": ("max_session_sets", 25),
    "session5t": ("max_session_tonnage_kg", 5_000),
    "combine8": ("max_session_exercises", 8),
    "bw25": ("max_bodyweight_reps", 25),
    "early10": ("early_workouts", 10),
    "bwlog30": ("bodyweight_logs", 30),
}


def test_countable_badges_report_progress_from_their_ctx_field():
    for code, (field, target) in _COUNTABLE.items():
        current = target - 1
        ctx = _ctx(**{field: current})
        by_code = {bp.code: bp for bp in nearest_progress(ctx, earned=set(), limit=50)}
        assert code in by_code, code
        assert by_code[code].current == current, code
        assert by_code[code].target == target, code


def test_countable_targets_match_award_thresholds():
    """Порог в «Ближайших» — ровно тот, на котором earned_codes выдаёт значок."""
    from achievements import earned_codes
    for code, (field, target) in _COUNTABLE.items():
        assert code in earned_codes(_ctx(**{field: target})), code
        assert code not in earned_codes(_ctx(**{field: target - 1})), code


def test_every_numeric_family_code_is_in_catalog_and_phrased():
    """Каждый код семейства есть в CATALOG и получает фразу на обоих языках —
    иначе format_badge_progress упадёт на экране бота."""
    from html import escape

    import formatting
    from achievements import BY_CODE, FAMILY_BY_CODE
    for code in FAMILY_BY_CODE:
        assert code in BY_CODE
        for lang in ("ru", "en"):
            with i18n.use_lang(lang):
                line = formatting.format_badge_progress(_bp(code, 1, 1000))
                assert escape(BY_CODE[code].title) in line
                assert "{" not in line


def test_variety50_ranks_by_fraction_against_other_families():
    # 45/50 разных упражнений (0.9) обгоняет 5/10 тренировок (0.5).
    ctx = _ctx(total_workouts=5, distinct_exercises=45)
    nearest = nearest_progress(ctx, earned={"first", "variety20"}, limit=1)
    assert nearest[0].code == "variety50"


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


def test_variety_family_phrasing_is_x_of_y_ru_en():
    import formatting
    bp = _bp("variety50", current=42, target=50)
    with i18n.use_lang("ru"):
        line = formatting.format_badge_progress(bp)
        assert "Мастер на все руки" in line
        assert "42 из 50" in line
    with i18n.use_lang("en"):
        assert "42 of 50" in formatting.format_badge_progress(bp)


def test_session_tonnage_phrasing_matches_tonnage_ru_en():
    import formatting
    bp = _bp("session5t", current=3_800, target=5_000)  # ещё 1.2 т
    with i18n.use_lang("ru"):
        line = formatting.format_badge_progress(bp)
        assert i18n.t("achievements.nearest_tons", tons="1.2") in line
    with i18n.use_lang("en"):
        line = formatting.format_badge_progress(bp)
        assert i18n.t("achievements.nearest_tons", tons="1.2") in line


def test_bot_screen_still_shows_three_nearest_with_new_families():
    import formatting
    ctx = _ctx(
        total_workouts=9, distinct_exercises=45, distinct_groups=5,
        max_session_sets=20, bodyweight_logs=28,
    )
    assert len(nearest_progress(ctx, earned=set())) == 3
    with i18n.use_lang("ru"):
        text = formatting.build_achievements_screen(set(), ctx)
    assert "45 из 50" in text


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
