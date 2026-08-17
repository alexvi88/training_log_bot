"""Достижения и звания — это КАТАЛОГИ ДАННЫХ (Achievement.code / Rank.level
лежат в БД и не должны зависеть от языка), но title/description/name читает
человек, поэтому они обязаны разрешаться через i18n в момент рендера, а не
быть зашитыми в CATALOG/RANKS на языке того, кто первым дёрнул модуль после
старта процесса. Этот файл проверяет ровно то, что легко упустить: забытый
перевод одной записи из 45, кириллицу на английском и то, что текст реально
следует за текущим language-контекстом, а не застывает на импорте.
"""

import i18n
from achievements import CATALOG
from analytics import RANKS
from i18n_coverage import has_cyrillic


def test_every_achievement_has_catalog_entries_in_both_languages():
    for a in CATALOG:
        for lang in ("ru", "en"):
            catalog = i18n._load_catalog(lang)
            assert f"achievement.{a.code}.title" in catalog, f"{lang}: нет title для {a.code}"
            assert f"achievement.{a.code}.description" in catalog, f"{lang}: нет description для {a.code}"


def test_every_rank_has_catalog_entries_in_both_languages():
    for rank in RANKS:
        for lang in ("ru", "en"):
            catalog = i18n._load_catalog(lang)
            assert f"rank.{rank.level}.name" in catalog, f"{lang}: нет name для звания {rank.level}"


def test_english_achievements_have_no_cyrillic():
    with i18n.use_lang("en"):
        for a in CATALOG:
            assert not has_cyrillic(a.title), f"{a.code}: кириллица в английском title: {a.title!r}"
            assert not has_cyrillic(a.description), (
                f"{a.code}: кириллица в английском description: {a.description!r}"
            )


def test_english_ranks_have_no_cyrillic():
    with i18n.use_lang("en"):
        for rank in RANKS:
            assert not has_cyrillic(rank.name), f"звание {rank.level}: кириллица в английском name: {rank.name!r}"


def test_achievement_code_does_not_depend_on_language():
    # code лежит в таблице achievements (db.py) — сменой языка он не имеет права
    # шевельнуться, иначе то, что уже записано в БД, перестанет матчиться.
    with i18n.use_lang("ru"):
        codes_ru = [a.code for a in CATALOG]
    with i18n.use_lang("en"):
        codes_en = [a.code for a in CATALOG]
    assert codes_ru == codes_en


def test_rank_level_does_not_depend_on_language():
    # level лежит в users.rank_level_seen (db.py) — та же гарантия, что и у code.
    with i18n.use_lang("ru"):
        levels_ru = [r.level for r in RANKS]
    with i18n.use_lang("en"):
        levels_en = [r.level for r in RANKS]
    assert levels_ru == levels_en


def test_achievement_text_follows_the_current_language_context_not_import_time():
    """Тот самый риск задачи: если бы title/description были обычными полями,
    собранными на импорте, смена языка в рантайме на них бы не подействовала.
    CATALOG — один и тот же объект на протяжении всего теста; меняется только
    language-контекст между обращениями к .title одного и того же элемента."""
    first = next(a for a in CATALOG if a.code == "first")

    with i18n.use_lang("ru"):
        ru_title = first.title
        ru_description = first.description
    with i18n.use_lang("en"):
        en_title = first.title
        en_description = first.description
    with i18n.use_lang("ru"):
        ru_title_again = first.title

    assert ru_title != en_title
    assert ru_description != en_description
    assert ru_title == ru_title_again == "Первый шаг"
    assert en_title == "First Step"


def test_rank_name_follows_the_current_language_context_not_import_time():
    novice = RANKS[0]

    with i18n.use_lang("ru"):
        ru_name = novice.name
    with i18n.use_lang("en"):
        en_name = novice.name
    with i18n.use_lang("ru"):
        ru_name_again = novice.name

    assert ru_name != en_name
    assert ru_name == ru_name_again == "Новичок"
    assert en_name == "Rookie"
