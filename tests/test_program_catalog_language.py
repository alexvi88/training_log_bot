"""Каталог готовых программ показывается на языке пользователя.

Этот файл про конкретный класс ошибки, а не про перевод как таковой. Функции
`seed_data.localized_program_*` и ключи `program.*` в каталоге появились раньше,
чем их вызовы: один агент сделал данные, другой правил хендлер и решил, что
каталог программ — не его забота. В итоге перевод существовал, тесты на паритет
ключей проходили, а англоязычный в «Готовых программах» видел русское всё —
названия, meta, описания, имена дней и состав.

Храповик по кириллическим литералам такое не ловит: в `handlers/routines.py`
литералов действительно не осталось, русский приезжал ДАННЫМИ из `seed_data`.
Поэтому проверка тут сквозная — через то, что реально уходит на экран.
"""

import re

import i18n
import keyboards
import seed_data

CYRILLIC = re.compile("[А-Яа-яЁё]")


def test_every_catalog_program_has_english_text():
    """Название, meta и описание — у всех программ, а не у большинства."""
    with i18n.use_lang("en"):
        for program in seed_data.WORKOUT_PROGRAMS:
            key = program["key"]
            for label, value in (
                ("name", seed_data.localized_program_name(key, "en")),
                ("meta", seed_data.localized_program_meta(key, "en")),
                ("description", seed_data.localized_program_description(key, "en")),
            ):
                assert value, f"{key}: пустой {label}"
                assert not CYRILLIC.search(value), f"{key}: русский {label} — {value!r}"


def test_every_program_day_has_an_english_name():
    for program in seed_data.WORKOUT_PROGRAMS:
        key = program["key"]
        for i in range(len(program["days"])):
            name = seed_data.localized_program_day_name(key, i, "en")
            assert name, f"{key}: день {i} без английского имени"
            assert not CYRILLIC.search(name), f"{key}: русское имя дня — {name!r}"


def test_program_targets_lose_the_russian_seconds_suffix():
    """Схема подходов почти везде language-neutral — цифры и «×». Исключение
    одно: «3×30–60 сек» у планки, и три русские буквы в хвосте строки глаз
    пропускает легче всего."""
    assert seed_data.localized_target("3×30–60 сек", "en") == "3×30–60 sec"
    assert seed_data.localized_target("3×30–60 сек", "ru") == "3×30–60 сек"
    # Схема без единицы не трогается ни на одном языке.
    assert seed_data.localized_target("3×8–12", "en") == "3×8–12"
    assert seed_data.localized_target(None, "en") is None


def test_no_catalog_program_target_keeps_cyrillic_in_english():
    for program in seed_data.WORKOUT_PROGRAMS:
        for _day_name, exercises in program["days"]:
            for _ex, target in exercises:
                shown = seed_data.localized_target(target, "en")
                assert not CYRILLIC.search(shown or ""), f"русская схема: {shown!r}"


def test_catalog_keyboard_labels_have_no_cyrillic_in_english():
    """Кнопка выбора программы берёт имя из данных каталога, где оно навсегда
    русское — язык обязан выбираться на рендере."""
    with i18n.use_lang("en"):
        markup = keyboards.programs_catalog_keyboard(seed_data.WORKOUT_PROGRAMS)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    leaked = [label for label in labels if CYRILLIC.search(label)]
    assert not leaked, f"русские названия программ на кнопках: {leaked}"


def test_russian_still_reads_from_the_catalog_source():
    """Регрессия в обратную сторону: русский берётся прямо из WORKOUT_PROGRAMS,
    без похода в каталог локалей, — то есть перевод не мог его подменить."""
    for program in seed_data.WORKOUT_PROGRAMS:
        key = program["key"]
        assert seed_data.localized_program_name(key, "ru") == program["name"]
        assert seed_data.localized_program_meta(key, "ru") == program["meta"]
