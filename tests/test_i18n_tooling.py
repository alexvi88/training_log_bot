"""Тесты для scripts/i18n_extract.py и scripts/i18n_export_xcstrings.py.

Оба скрипта работают с файлами на диске, поэтому тесты используют tmp_path:
для extract — собирают маленький .py модуль с докстрингом, русским литералом
и f-строкой; для export — маленький locales/*.json каталог с плюрализацией.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import i18n_export_xcstrings  # noqa: E402
import i18n_extract  # noqa: E402

SAMPLE_MODULE = '''"""Модуль обрабатывает вес атлета — докстринг, не текст пользователя."""


def format_weight(value, unit):
    """Докстринг функции — тоже не текст пользователя."""

    class Inner:
        """И докстринг класса туда же."""

        pass

    if value <= 0:
        return "Не понял вес. Напиши число, например 80"
    return f"Записал {value} {unit.name}, атлет"
'''


def test_extract_skips_docstrings_and_handles_fstring(tmp_path):
    module_path = tmp_path / "sample_module.py"
    module_path.write_text(SAMPLE_MODULE, encoding="utf-8")

    results = i18n_extract.extract_from_file(module_path)
    texts = [r.text for r in results]

    # Докстрингов (модуля, функции, класса) быть не должно.
    assert not any("докстринг" in t.lower() for t in texts)

    # Обычный литерал найден как есть.
    assert "Не понял вес. Напиши число, например 80" in texts

    # f-строка превращена в шаблон с плейсхолдерами по простому имени/атрибуту.
    fstring_results = [r for r in results if r.placeholders]
    assert len(fstring_results) == 1
    fstring_result = fstring_results[0]
    assert fstring_result.value == "Записал {value} {unit.name}, атлет"
    assert fstring_result.placeholders == ["value", "unit.name"]
    assert not fstring_result.needs_review


def test_extract_flags_complex_fstring_expression_for_manual_review(tmp_path):
    module_path = tmp_path / "complex_module.py"
    module_path.write_text(
        'def report(a, b):\n    return f"Итого: {a + b} повторов"\n',
        encoding="utf-8",
    )

    results = i18n_extract.extract_from_file(module_path)
    assert len(results) == 1
    assert results[0].needs_review
    assert results[0].placeholders == ["arg1"]
    assert results[0].value == "Итого: {arg1} повторов"


def test_extract_assigns_unique_keys_with_prefix(tmp_path):
    module_path = tmp_path / "dup_module.py"
    module_path.write_text(
        'def f():\n    a = "Отличный подход"\n    b = "Отличный подход"\n    return a, b\n',
        encoding="utf-8",
    )

    results = i18n_extract.extract_from_file(module_path)
    keyed = i18n_extract.assign_keys(results, "screen.workout")
    keys = [key for key, _ in keyed]

    assert keys[0] == "screen.workout.otlichnyi_podhod"
    assert keys[1] == "screen.workout.otlichnyi_podhod_2"
    assert len(set(keys)) == len(keys)


def test_export_writes_flat_and_plural_strings(tmp_path):
    locales_dir = tmp_path / "locales"
    locales_dir.mkdir()
    (locales_dir / "ru.json").write_text(
        json.dumps(
            {
                "test.hello": "Привет, {name}!",
                "test.sets": "{n, plural, one{# подход} few{# подхода} many{# подходов} other{# подхода}}",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (locales_dir / "en.json").write_text(
        json.dumps(
            {
                "test.hello": "Hello, {name}!",
                "test.sets": "{n, plural, one{# set} other{# sets}}",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    i18n_export_xcstrings.LOCALES_DIR = locales_dir
    locales = i18n_export_xcstrings.load_locales()
    catalog_doc, plural_keys, embedded_plural_count = i18n_export_xcstrings.build_catalog(locales, "ru")

    assert catalog_doc["sourceLanguage"] == "ru"
    assert catalog_doc["version"] == "1.0"
    assert plural_keys == 1
    assert embedded_plural_count == 0

    hello = catalog_doc["strings"]["test.hello"]
    assert hello["localizations"]["ru"]["stringUnit"]["value"] == "Привет, {name}!"
    assert hello["localizations"]["en"]["stringUnit"]["value"] == "Hello, {name}!"

    sets_ru = catalog_doc["strings"]["test.sets"]["localizations"]["ru"]
    assert "stringUnit" not in sets_ru
    plural_ru = sets_ru["variations"]["plural"]
    assert plural_ru["one"]["stringUnit"]["value"] == "# подход"
    assert plural_ru["few"]["stringUnit"]["value"] == "# подхода"
    assert plural_ru["many"]["stringUnit"]["value"] == "# подходов"
    assert plural_ru["other"]["stringUnit"]["value"] == "# подхода"

    sets_en = catalog_doc["strings"]["test.sets"]["localizations"]["en"]
    plural_en = sets_en["variations"]["plural"]
    assert set(plural_en) == {"one", "other"}


def test_export_keeps_embedded_plural_flat(tmp_path):
    locales_dir = tmp_path / "locales"
    locales_dir.mkdir()
    (locales_dir / "ru.json").write_text(
        json.dumps(
            {
                "test.mixed": "Ты сделал {n, plural, one{# подход} other{# подхода}} сегодня",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    i18n_export_xcstrings.LOCALES_DIR = locales_dir
    locales = i18n_export_xcstrings.load_locales()
    catalog_doc, plural_keys, embedded_plural_count = i18n_export_xcstrings.build_catalog(locales, "ru")

    assert plural_keys == 0
    assert embedded_plural_count == 1
    mixed = catalog_doc["strings"]["test.mixed"]["localizations"]["ru"]
    assert "variations" not in mixed
    assert mixed["stringUnit"]["value"].startswith("Ты сделал {n, plural,")


def test_export_missing_locales_dir_reports_and_exits(tmp_path, capsys, monkeypatch):
    missing_dir = tmp_path / "no_such_locales"
    i18n_export_xcstrings.LOCALES_DIR = missing_dir
    monkeypatch.setattr(sys, "argv", ["i18n_export_xcstrings.py"])

    exit_code = i18n_export_xcstrings.main()

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "не найден" in captured.err
