"""Блокер, найденный независимым аудитом уже после «готово».

Про данные, а не про литералы, и про ШОВ: перевод существовал, один вход к
нему научили, соседний остался.

Шаблоны каталога в клавиатуре упражнений. Шаблон — общая строка на всех
(`exercises.is_template=1`), персональной копии с переведённым именем у него
нет, в базе имя навсегда русское. Каталожный браузер по группе локализацию
получил, а три экрана ПОИСКА (живая тренировка, правка прошлой, добавление в
программу) ходят в общую keyboards.exercises_keyboard — и англоязычный на
запрос «bench» получал «📋 Жим штанги лёжа».

Храповик по кириллическим литералам этого не ловит: русский приезжает из базы.
"""

import re

import i18n
import keyboards
import seed_data

CYRILLIC = re.compile("[А-Яа-яЁё]")


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def test_catalog_templates_in_the_picker_are_localized():
    """Шаблон приходит в клавиатуру сырой строкой из базы — язык выбирается
    здесь, иначе каждый новый экран с шаблонами протечёт заново."""
    templates = [
        {"id": i, "display_name": name}
        for i, name in enumerate(("Жим штанги лёжа", "Присед со штангой"), start=1)
    ]
    with i18n.use_lang("en"):
        markup = keyboards.exercises_keyboard([], templates=templates, prefix="exm")
    leaked = [label for label in _labels(markup) if CYRILLIC.search(label)]
    assert not leaked, f"русские имена шаблонов у англоязычного: {leaked}"

    with i18n.use_lang("ru"):
        markup_ru = keyboards.exercises_keyboard([], templates=templates, prefix="exm")
    assert any("Жим штанги лёжа" in label for label in _labels(markup_ru))


def test_user_exercises_are_never_translated():
    """Своё упражнение — данные пользователя: как назвал, так и показываем, на
    любом языке интерфейса."""
    own = [{"id": 1, "display_name": "Жим штанги лёжа"}]
    with i18n.use_lang("en"):
        markup = keyboards.exercises_keyboard(own, templates=[], prefix="exm")
    assert any("Жим штанги лёжа" in label for label in _labels(markup))


def test_every_catalog_template_has_an_english_name():
    """Сквозная проверка: перевод есть у всех ста, а не у тех, что попались."""
    for _group, name in seed_data.EXERCISE_TEMPLATES:
        localized = seed_data.localized_exercise_name(name, "en")
        assert localized, name
        assert not CYRILLIC.search(localized), f"{name} → {localized!r}"

