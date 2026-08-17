"""search_terms: стемминг и синонимы поиска упражнений.

Проверено прогоном человеческих запросов по живому каталогу: морфология
работала и до этих тестов («приседания» находят «Присед», «жим лёжа гантели» —
«Жим гантелей лёжа»), а спотыкались синонимы — «махи в стороны», «разводка»,
«икры». Стемминг такое не лечит по определению: слова не однокоренные.
"""

import search_terms

# ---------- синонимы: человек называет упражнение своим словом ----------


def _find(names, query):
    groups = search_terms.query_groups(query)
    if not groups:
        return []
    return [
        n for n in names
        if all(any(v in search_terms.fold(n) for v in group) for group in groups)
    ]


CATALOG = [
    "Разведение гантелей в стороны", "Махи в кроссовере", "Подъём на носки стоя",
    "Скручивания", "Шраги со штангой", "Гиперэкстензия", "Отжимания на брусьях",
    "Подтягивания", "Сгибание запястий со штангой", "Сгибание ног в тренажёре",
    "Румынская тяга", "Жим штанги лёжа", "Присед со штангой",
    "Сведение рук в тренажёре «бабочка»",
]


def test_movement_called_by_another_word_is_found():
    """Стемминг тут бессилен: «махи» и «разведение» не однокоренные."""
    assert "Разведение гантелей в стороны" in _find(CATALOG, "махи в стороны")
    assert "Разведение гантелей в стороны" in _find(CATALOG, "разводка")
    assert "Сведение рук в тренажёре «бабочка»" in _find(CATALOG, "бабочка")


def test_muscle_name_instead_of_movement_is_found():
    """Человек ищет по мышце, а в каталоге движение: «икры» → «Подъём на носки»."""
    assert "Подъём на носки стоя" in _find(CATALOG, "икры")
    assert "Шраги со штангой" in _find(CATALOG, "трапеции")
    assert "Гиперэкстензия" in _find(CATALOG, "поясница")
    assert "Сгибание запястий со штангой" in _find(CATALOG, "предплечья")
    assert "Скручивания" in _find(CATALOG, "пресс")


def test_two_word_muscle_name_needs_the_phrase_table():
    """«Бицепс бедра» по словам не ловится: в «Сгибании ног» нет ни «бицепса»,
    ни «бедра». Поэтому фразы проверяются до разбиения на слова."""
    found = _find(CATALOG, "бицепс бедра")
    assert "Сгибание ног в тренажёре" in found
    assert "Румынская тяга" in found


def test_synonyms_do_not_break_plain_queries():
    """Основа всегда остаётся в группе — синонимы добавляют варианты, не подменяют."""
    assert _find(CATALOG, "жим лёжа") == ["Жим штанги лёжа"]
    assert _find(CATALOG, "приседания") == ["Присед со штангой"]
    assert _find(CATALOG, "подтягивания") == ["Подтягивания"]


def test_imprecise_muscle_phrases_are_left_out_on_purpose():
    """«Задняя дельта» через подстроку «наклон» вытаскивала жимы на наклонной
    скамье — грудь вместо плеч. Неверный ответ первым в выдаче хуже, чем
    отсутствие синонима: человек переформулирует, а не запишет не то."""
    assert _find(CATALOG, "задняя дельта") == []


# ---------- английский: стемминг и синонимы (независимо от языка интерфейса) ----------

EN_CATALOG = [
    "Barbell Squat", "Barbell Curl", "Crunches", "Plank", "Barbell Shrug",
    "Leg Extension", "Lying Leg Curl", "Romanian Deadlift", "Barbell Wrist Curl",
    "Dips", "Standing Barbell Overhead Press", "Barbell Bench Press",
    "Barbell Glute Bridge", "Hyperextension", "Dumbbell Curl", "Leg Press",
]


def test_english_plural_stemming_finds_the_singular_catalog_name():
    """«squats»/«curls» — обычный английский плюрал, тот же принцип, что и
    русское «приседания» → «Присед»."""
    assert _find(EN_CATALOG, "squats") == ["Barbell Squat"]
    assert "Barbell Curl" in _find(EN_CATALOG, "curls")


def test_english_ing_stemming_finds_the_bare_verb():
    assert "Barbell Bench Press" in _find(EN_CATALOG, "pressing")


def test_press_double_s_is_not_mistaken_for_a_plural():
    """«press» уже оканчивается на «ss» — это не форма множественного числа,
    срезать до «pres» незачем и рискованно."""
    assert search_terms.stem("press") == "press"


def test_english_muscle_name_instead_of_movement_is_found():
    """«abs»/«quads»/«hamstrings» — то же самое отсутствие мышцы в названии
    каталога, что и в русских «икры»/«пресс»/«трапеции»."""
    assert "Crunches" in _find(EN_CATALOG, "abs")
    assert "Plank" in _find(EN_CATALOG, "core")
    assert _find(EN_CATALOG, "traps")[0] == "Barbell Shrug"
    assert "Leg Extension" in _find(EN_CATALOG, "quads")
    assert "Leg Press" in _find(EN_CATALOG, "quads")
    assert "Lying Leg Curl" in _find(EN_CATALOG, "hamstrings")
    assert "Barbell Wrist Curl" in _find(EN_CATALOG, "forearms")


def test_english_bare_movement_word_synonyms_are_not_too_broad():
    """«quads»/«hamstrings» не должны цеплять любой жим/сгибание в каталоге —
    та же ловушка, которую русское «квадрицепс» обходит фразой «жим ногами»,
    а не голым «жим»."""
    assert "Barbell Bench Press" not in _find(EN_CATALOG, "quads")
    assert "Standing Barbell Overhead Press" not in _find(EN_CATALOG, "quads")
    assert "Barbell Curl" not in _find(EN_CATALOG, "hamstrings")
    assert "Barbell Wrist Curl" not in _find(EN_CATALOG, "hamstrings")


def test_english_two_word_phrase_needs_the_phrase_table():
    """«military press»/«hip thrust» — устойчивые словосочетания зала, которых
    в названии каталога нет ни одним из отдельных слов совпадения."""
    assert _find(EN_CATALOG, "military press") == ["Standing Barbell Overhead Press"]
    assert "Barbell Glute Bridge" in _find(EN_CATALOG, "hip thrust")
    assert _find(EN_CATALOG, "low back") == ["Hyperextension"]


def test_english_synonyms_do_not_break_plain_queries():
    assert _find(EN_CATALOG, "bench press") == ["Barbell Bench Press"]
    assert _find(EN_CATALOG, "dumbbell curl") == ["Dumbbell Curl"]


# ---------- поиск двуязычный независимо от языка интерфейса ----------


def test_both_alphabets_are_recognised_in_the_same_catalog():
    """Один и тот же вызов должен понимать и русское, и английское название —
    как voice_parse.py уже понимает числительные на обоих языках сразу,
    независимо от языка интерфейса пользователя (см. шапку voice_parse.py)."""
    mixed = CATALOG + EN_CATALOG
    assert _find(mixed, "приседания") == ["Присед со штангой"]
    assert _find(mixed, "squats") == ["Barbell Squat"]
