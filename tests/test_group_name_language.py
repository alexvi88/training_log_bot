"""Названия групп мышц переводятся в ОДНОЙ точке — formatting.format_group.

Группы мышц никогда не форкают в аккаунт: это глобальные строки
(`muscle_groups.user_id IS NULL`), и в базе имя навсегда остаётся русским
пресетом. Значит выбрать язык можно только на рендере — и если делать это у
вызывающих, каждый новый вызов молча показывает англоязычному русское слово.
Ровно так и случилось в трёх местах сразу: в клавиатуре выбора группы, в панели
недельного объёма и в теге рядом с названием упражнения.

Этот файл стережёт две вещи: что перевод живёт внутри format_group (а не у
вызывающих) и что своя группа пользователя через него проходит нетронутой.
"""

import re

import formatting
import i18n
import keyboards
import seed_data

CYRILLIC = re.compile("[А-Яа-яЁё]")


def test_format_group_localizes_preset_groups():
    for canonical in seed_data._MUSCLE_GROUP_SLUGS:
        with i18n.use_lang("ru"):
            assert formatting.format_group(canonical) == canonical.upper()
        with i18n.use_lang("en"):
            shown = formatting.format_group(canonical)
            assert not CYRILLIC.search(shown), f"русская группа у англоязычного: {shown!r}"


def test_format_group_tag_localizes_too():
    """Тег в квадратных скобках рядом с названием упражнения идёт через
    format_group, так что перевод обязан доставаться и ему — иначе «Bench Press
    [ГРУДЬ]»."""
    with i18n.use_lang("en"):
        for canonical in seed_data._MUSCLE_GROUP_SLUGS:
            assert not CYRILLIC.search(formatting.format_group_tag(canonical))


def test_custom_user_group_passes_through_untouched():
    """Своя группа пользователя слага не имеет — это его данные, и переводить их
    нам нечем и незачем. Кириллица тут законна на любом языке."""
    with i18n.use_lang("en"):
        assert formatting.format_group("Предплечья") == "ПРЕДПЛЕЧЬЯ"
        assert formatting.format_group("Forearms") == "FOREARMS"


def test_group_picker_keyboard_has_no_cyrillic_for_english_user():
    """Сквозная проверка того самого места, где протечка и жила: клавиатура
    зовёт format_group напрямую, своей локализации у неё нет и быть не должно."""
    groups = [
        {"id": i, "name": name} for i, name in enumerate(seed_data._MUSCLE_GROUP_SLUGS, start=1)
    ]
    with i18n.use_lang("en"):
        markup = keyboards.groups_keyboard(groups, prefix="exm")
    labels = [b.text for row in markup.inline_keyboard for b in row]
    leaked = [label for label in labels if CYRILLIC.search(label)]
    assert not leaked, f"русские названия групп в клавиатуре: {leaked}"
