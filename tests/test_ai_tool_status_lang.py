"""Статусы инструментов тренера («смотрю прогресс по упражнению...») — на языке
атлета. Раньше они жили русским словарём в ai_trainer.py, и у англоязычного
атлета переведённые фразы-заполнители чередовались с русскими статусами."""

import re

import pytest

import ai_trainer
import i18n

CYRILLIC = re.compile(r"[А-Яа-яЁё]")
LATIN_WORD = re.compile(r"[A-Za-z]{3,}")


def _tool_names() -> list[str]:
    return [tool["function"]["name"] for tool in ai_trainer.TOOLS]


@pytest.mark.parametrize("name", _tool_names() + ["no_such_tool"])
def test_english_athlete_sees_english_tool_status(name):
    with i18n.use_lang("en"):
        variants = ai_trainer._tool_status_variants(name)
        web = i18n.t("ai.tool_status.web_search")
    assert variants
    for text in [*variants, web]:
        assert not CYRILLIC.search(text), text


@pytest.mark.parametrize("name", _tool_names())
def test_russian_athlete_sees_russian_tool_status(name):
    with i18n.use_lang("ru"):
        variants = ai_trainer._tool_status_variants(name)
    assert variants
    for text in variants:
        assert CYRILLIC.search(text), text
        assert not LATIN_WORD.search(text), text


def test_every_tool_has_its_own_status():
    """Новый инструмент без своей строки получил бы общий «копаюсь в данных» —
    это не ошибка, но и не то, что хотим: заводишь инструмент — заводи статус."""
    default = ai_trainer._tool_status_variants("no_such_tool")
    with i18n.use_lang("ru"):
        missing = [n for n in _tool_names() if ai_trainer._tool_status_variants(n) == default]
    assert not missing, missing
