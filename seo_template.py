"""Шаблон дневника тренировок для страницы `/<язык>/templates/workout-log`:
файл .xlsx и два .csv, которые наш импорт (handlers/csv_import.py) читает
без единого вопроса.

Почему без вопросов: заголовки колонок — ровно те слова, что импорт узнаёт
сам (`csv_import.SYNONYMS`): дата, название тренировки, упражнение, номер
подхода, вес с единицей в скобках, повторы, RPE и заметка. Единица в
заголовке («Вес (кг)») — сигнал `_file_weight_unit`: вес не перепутается с
фунтами. Имена упражнений в примере и в списке на третьем листе — каталог
бота на языке страницы (`seed_data.localized_exercise_name`): такое имя
импорт сводит с каталогом точно, без «какое это упражнение?». Это держит
тест: шаблон, заполненный примером, проходит весь разбор импорта.

Файлы:
- `workout-log.xlsx` — три листа: пустой «Дневник» с заголовками (колонка
  даты в формате ГГГГ-ММ-ДД, чтобы «Сохранить как CSV» дал однозначную дату),
  «Пример» — две заполненные тренировки, «Упражнения» — каталог по группам.
  На колонке упражнения — выпадающий список из каталога, но своё название
  тоже можно вписать (ошибку Excel не показывает).
- `workout-log.csv` — только заголовки.
- `workout-log-example.csv` — пример целиком: можно сразу загрузить в бота
  и посмотреть, как ложится импорт (и отменить его).

Бот принимает CSV, поэтому лист сохраняют в CSV (как — написано на
странице). Файл .xlsx собирается вручную, без openpyxl: это zip с
несколькими XML. Время внутри zip — фиксированное, так байты файла (и его
ETag) не меняются от запуска к запуску.

Строки — из каталога (`seo.template.*`) в момент запроса, на импорте
модуля ничего не вычисляется.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import zipfile
from functools import lru_cache
from xml.sax.saxutils import escape

import i18n
import seed_data

COLUMNS = ("date", "workout", "exercise", "set", "weight", "reps", "rpe", "note")
_WIDTHS = (13, 22, 34, 9, 11, 10, 8, 30)
_ZIP_TIME = (2026, 10, 16, 0, 0, 0)

# Пример: две тренировки. (дней назад от EXAMPLE_END, ключ названия,
# [(идентичность упражнения, [(вес, повторы, rpe|None)], заметка|None)]).
EXAMPLE_END = dt.date(2026, 10, 14)
_EXAMPLE = (
    (2, "a", (
        ("Присед со штангой", ((60, 8, None), (80, 5, None), (80, 5, None), (80, 5, 8)), "squat"),
        ("Жим штанги лёжа", ((60, 8, None), (70, 6, None), (70, 6, 8.5)), None),
        ("Тяга верхнего блока", ((50, 10, None), (50, 10, None), (55, 8, 9)), None),
    )),
    (0, "b", (
        ("Румынская тяга", ((70, 8, None), (80, 8, None), (80, 8, 8)), None),
        ("Жим штанги стоя", ((35, 8, None), (40, 6, None), (40, 5, 9)), "press"),
        ("Подтягивания", ((0, 8, None), (0, 7, None), (0, 6, None)), None),
    )),
)


def headers(lang: str) -> list[str]:
    return [i18n.t_in(lang, f"seo.template.col.{c}") for c in COLUMNS]


def _num(value: float) -> str:
    return f"{value:g}"


def example_rows(lang: str) -> list[list[str]]:
    """Строки примера как текст ячеек: дата ГГГГ-ММ-ДД, вес — число (вес тела
    у подтягиваний — пустая ячейка: импорт пишет такой подход весом тела)."""
    rows: list[list[str]] = []
    for days_ago, title_key, exercises in _EXAMPLE:
        day = (EXAMPLE_END - dt.timedelta(days=days_ago)).isoformat()
        title = i18n.t_in(lang, f"seo.template.example.workout_{title_key}")
        for identity, sets, note_key in exercises:
            name = seed_data.localized_exercise_name(identity, lang)
            for i, (weight, reps, rpe) in enumerate(sets, start=1):
                note = i18n.t_in(lang, f"seo.template.example.note_{note_key}") if note_key and i == 1 else ""
                rows.append([day, title, name, str(i), _num(weight) if weight else "", str(reps),
                             _num(rpe) if rpe else "", note])
    return rows


def catalog_rows(lang: str) -> list[tuple[str, str]]:
    """(группа, упражнение) каталога на языке страницы — без скрытых шаблонов."""
    out = []
    for group, identity in seed_data.EXERCISE_TEMPLATES:
        if seed_data.is_catalog_hidden(identity):
            continue
        out.append((seed_data.localized_muscle_group_name(group, lang), seed_data.localized_exercise_name(identity, lang)))
    return out


def _csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    for row in rows:
        writer.writerow(row)
    # BOM — чтобы Excel открыл кириллицу как UTF-8; импорт снимает его сам.
    return ("﻿" + buf.getvalue()).encode("utf-8")


@lru_cache(maxsize=8)
def blank_csv(lang: str) -> bytes:
    return _csv([headers(lang)])


@lru_cache(maxsize=8)
def example_csv(lang: str) -> bytes:
    return _csv([headers(lang)] + example_rows(lang))


# ---------- xlsx ----------


def _col(i: int) -> str:
    return "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[i]


def _excel_serial(iso: str) -> int:
    return (dt.date.fromisoformat(iso) - dt.date(1899, 12, 30)).days


def _cell(ref: str, value: str, style: int = 0, kind: str = "str") -> str:
    s = f' s="{style}"' if style else ""
    if kind == "num":
        return f'<c r="{ref}"{s}><v>{value}</v></c>'
    return f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'


# Стили (styles.xml, cellXfs): 0 — обычный, 1 — заголовок, 2 — дата ГГГГ-ММ-ДД.
_HEADER, _DATE = 1, 2


def _sheet(rows: list[str], widths: tuple[int, ...], *, freeze: bool = True, validation: str = "") -> str:
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths))
    pane = ('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            "</sheetView></sheetViews>") if freeze else ""
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"{pane}<cols>{cols}</cols><sheetData>{''.join(rows)}</sheetData>{validation}</worksheet>")


def _row(n: int, cells: list[str]) -> str:
    return f'<row r="{n}">{"".join(cells)}</row>'


def _log_rows(lang: str, data: list[list[str]], blank_rows: int = 0) -> list[str]:
    out = [_row(1, [_cell(f"{_col(i)}1", h, _HEADER) for i, h in enumerate(headers(lang))])]
    for r, values in enumerate(data, start=2):
        cells = []
        for i, value in enumerate(values):
            ref = f"{_col(i)}{r}"
            if not value:
                continue
            if COLUMNS[i] == "date":
                cells.append(_cell(ref, str(_excel_serial(value)), _DATE, "num"))
            elif COLUMNS[i] in ("set", "weight", "reps", "rpe"):
                cells.append(_cell(ref, value, 0, "num"))
            else:
                cells.append(_cell(ref, value))
        out.append(_row(r, cells))
    # Пустые строки с форматом даты в первой колонке: введённая дата сразу
    # показывается как ГГГГ-ММ-ДД и так же уходит в CSV.
    start = len(data) + 2
    for r in range(start, start + blank_rows):
        out.append(_row(r, [f'<c r="A{r}" s="{_DATE}"/>']))
    return out


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/></numFmts>'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><color rgb="FF1B0D06"/><name val="Calibri"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFFFB35C"/><bgColor indexed="64"/></patternFill></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def _sheet_name(lang: str, key: str) -> str:
    # Имя листа в Excel — до 31 символа и без []:*?/\.
    name = i18n.t_in(lang, f"seo.template.sheet.{key}")
    return "".join(ch for ch in name if ch not in "[]:*?/\\")[:31]


@lru_cache(maxsize=8)
def xlsx(lang: str) -> bytes:
    log_name, example_name, catalog_name = (_sheet_name(lang, k) for k in ("log", "example", "catalog"))
    catalog = catalog_rows(lang)
    catalog_xml_rows = [_row(1, [_cell("A1", i18n.t_in(lang, "seo.template.col.exercise"), _HEADER),
                                 _cell("B1", i18n.t_in(lang, "seo.template.col.group"), _HEADER)])]
    catalog_xml_rows += [_row(i, [_cell(f"A{i}", name), _cell(f"B{i}", group)])
                         for i, (group, name) in enumerate(catalog, start=2)]
    last = len(catalog) + 1
    ref = f"'{catalog_name}'!$A$2:$A${last}"
    validation = (
        '<dataValidations count="1"><dataValidation type="list" allowBlank="1" showErrorMessage="0" '
        f'showInputMessage="0" sqref="C2:C2000"><formula1>{escape(ref)}</formula1></dataValidation></dataValidations>'
    )
    sheets = (
        (log_name, _sheet(_log_rows(lang, [], blank_rows=200), _WIDTHS, validation=validation)),
        (example_name, _sheet(_log_rows(lang, example_rows(lang)), _WIDTHS)),
        (catalog_name, _sheet(catalog_xml_rows, (40, 18))),
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
        + "".join(f'<sheet name="{escape(name, {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
                  for i, (name, _x) in enumerate(sheets, start=1))
        + "</sheets></workbook>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                  f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1))
        + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/></Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                  'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                  for i in range(1, len(sheets) + 1))
        + "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    parts = [("[Content_Types].xml", content_types), ("_rels/.rels", root_rels), ("xl/workbook.xml", workbook),
             ("xl/_rels/workbook.xml.rels", rels), ("xl/styles.xml", _STYLES)]
    parts += [(f"xl/worksheets/sheet{i}.xml", xml) for i, (_n, xml) in enumerate(sheets, start=1)]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts:
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data.encode("utf-8"))
    return buf.getvalue()


FILES = {
    "workout-log.xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", xlsx),
    "workout-log.csv": ("text/csv; charset=utf-8", blank_csv),
    "workout-log-example.csv": ("text/csv; charset=utf-8", example_csv),
}
