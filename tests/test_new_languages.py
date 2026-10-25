"""Испанский, португальский (бразильский и европейский), немецкий, французский
и турецкий — свои каталоги.

Те же гарантии, что держат ru/en (tests/test_i18n_no_leaks.py), но для языков,
у которых каталог переведён с английского: полнота ключей (включая SEO-сайт),
та же ICU-структура,
что у английской строки, ни одной кириллической буквы, кроме автонима
«Русский», брендовая формула без запятой и описания техники на каждое
упражнение каталога.
"""

import json
import re
from pathlib import Path

import pytest

import exercise_descriptions
import i18n

NEW_LANGS = ("es", "pt", "pt-PT", "de", "fr", "tr")
_LOCALES = Path(__file__).resolve().parent.parent / "locales"
_CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")
_AUTONYMS = ("Русский",)
_BRAND = {"es": "HOLA ATLETA", "pt": "OLÁ ATLETA", "pt-PT": "OLÁ ATLETA", "de": "HEY ATHLET", "fr": "SALUT ATHLÈTE", "tr": "SELAM ATLET"}


def _load(lang: str) -> dict[str, str]:
    with (_LOCALES / f"{lang}.json").open(encoding="utf-8") as f:
        return json.load(f)


def _product(catalog: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in catalog.items() if not k.startswith("test.")}


def _shape(nodes, acc):
    for node in nodes:
        if node[0] == "var":
            acc.add(node[1])
        elif node[0] in ("plural", "select"):
            keys = "" if node[0] == "plural" else ",".join(sorted(node[2]))
            acc.add(f"{node[1]}:{node[0]}:{keys}")
            for branch in node[2].values():
                _shape(branch, acc)
    return acc


def test_supported_languages_have_catalogs():
    assert set(NEW_LANGS) <= set(i18n.SUPPORTED)


@pytest.mark.parametrize("lang", NEW_LANGS)
def test_catalog_has_every_product_key_of_english(lang):
    en, own = _product(_load("en")), _product(_load(lang))
    assert not sorted(set(en) - set(own)), f"нет в {lang}.json"
    assert not sorted(set(own) - set(en)), f"есть только в {lang}.json"


@pytest.mark.parametrize("lang", NEW_LANGS)
def test_catalog_keeps_the_icu_shape_of_english(lang):
    en, own = _load("en"), _product(_load(lang))
    bad = []
    for key, template in own.items():
        try:
            nodes = i18n.parse_nodes(template)
        except ValueError as exc:
            bad.append(f"{key}: не парсится ({exc})")
            continue
        if _shape(nodes, set()) != _shape(i18n.parse_nodes(en[key]), set()):
            bad.append(f"{key}: плейсхолдеры не как у en")
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("lang", NEW_LANGS)
def test_catalog_has_no_cyrillic(lang):
    leaks = {}
    for key, value in _product(_load(lang)).items():
        for autonym in _AUTONYMS:
            value = value.replace(autonym, "")
        if _CYRILLIC.search(value):
            leaks[key] = value
    assert not leaks


@pytest.mark.parametrize("lang", NEW_LANGS)
def test_brand_formula_has_no_comma(lang):
    brand = _BRAND[lang]
    bad = [k for k, v in _load(lang).items() if re.search(re.escape(brand) + r"\s*,|" + brand.replace(" ", r",\s*"), v)]
    assert not bad


@pytest.mark.parametrize("lang", NEW_LANGS)
def test_every_push_names_the_athlete_with_the_brand_formula(lang):
    """Как у en: каждое тело пуша в Telegram (push.<категория>.<n>) открывается
    формулой бренда, если её открывает английский оригинал."""
    en, own = _load("en"), _load(lang)
    brand = _BRAND[lang]
    missing = [
        k for k, v in en.items()
        if k.startswith("push.") and v.startswith("HEY ATHLETE") and brand not in own[k]
    ]
    assert not missing


@pytest.mark.parametrize("lang", NEW_LANGS)
def test_every_catalog_exercise_has_a_technique_text(lang):
    own = exercise_descriptions._other_language_descriptions(lang)
    assert set(own) == set(exercise_descriptions.EXERCISE_DESCRIPTIONS_EN)
    assert not [k for k, v in own.items() if not v.strip() or _CYRILLIC.search(v)]


@pytest.mark.parametrize("lang", NEW_LANGS)
def test_rendering_every_key_does_not_crash(lang):
    """Плюрал и select рендерятся на числах 0, 1, 2, 5, 21 без исключений —
    ветка `other` есть везде, где она нужна."""
    catalog = _product(_load(lang))
    for key, template in catalog.items():
        nodes = i18n.parse_nodes(template)
        params = {name.split(":")[0]: 1 for name in _shape(nodes, set())}
        for n in (0, 1, 2, 5, 21):
            i18n.t_in(lang, key, **{k: (n if v == 1 else v) for k, v in params.items()})


@pytest.mark.parametrize(
    "code, expected", [("pt-PT", "pt-PT"), ("pt_pt", "pt-PT"), ("pt-BR", "pt"), ("pt", "pt"), ("pt-AO", "pt")]
)
def test_portuguese_variants(code, expected):
    assert i18n.normalize(code) == expected


def test_european_portuguese_falls_back_to_brazilian_first(monkeypatch):
    i18n.reload()
    i18n._load_catalog("pt-PT")
    monkeypatch.setitem(i18n._catalogs, "pt-PT", {})
    assert i18n.t_in("pt-PT", "btn.back") == i18n.t_in("pt", "btn.back")
    i18n.reload()
