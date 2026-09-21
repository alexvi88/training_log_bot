"""Экспорт всех подходов пользователя в CSV — «📤 Экспорт CSV» в настройках
бота, и та же дыра #3 в `/v1`: без неё забрать свои данные из приложения
было бы невозможно.

Один расчёт на бота и REST, как и у `workout_card.py`: `db.export_rows_for_user`
уже отдаёт готовые строки, здесь только формат CSV — который у обоих
потребителей обязан быть одним и тем же байт в байт, иначе экспорт из бота и
экспорт из приложения незаметно разошлись бы форматом одного и того же файла.

Заголовки колонок — латиницей и машинные (`started_at`, `exercise`, ...), не
из каталога `i18n`: это имена столбцов для последующего импорта (в том числе
обратно этим же ботом, см. `handlers/csv_import.py`), а не текст, который
показывается как проза.
"""

from __future__ import annotations

import csv
import datetime as dt
import io

import db
import formatting
import timeutil

CSV_HEADER = ["started_at", "exercise", "round_index", "weight", "reps", "rpe"]


async def build_csv(user_id: int) -> bytes:
    """CSV со всеми подходами всех законченных тренировок, старые сначала.

    `utf-8-sig` (BOM) — чтобы Excel, который угадывает кодировку по BOM, а не
    по содержимому, не показал кириллицу в названиях упражнений битой.
    Стандартный `csv.writer` сам берёт значение в кавычки, если в нём есть
    запятая, кавычка или перевод строки (QUOTE_MINIMAL) — второй реализации
    экранирования тут заводить незачем.

    `started_at` в базе — UTC, а импорт (`handlers/csv_import.py:_parse_row_date`)
    читает из этой же колонки календарную дату напрямую, без поправки на
    часовой пояс. Без пересчёта в местное время тренировка, начатая под
    вечер на положительном сдвиге (или под утро на отрицательном), пересекала
    полночь UTC: экспорт-и-обратный-импорт не был обратимым — тот же файл при
    повторной загрузке уезжал на соседний день и не распознавался как дубль
    (`_duplicate_dates` сравнивает по дате), удваивая тренировку вместо
    пропуска. Сдвигаем на tz_offset здесь же, единственном месте, которое
    читают и бот, и REST (см. докстринг модуля).
    """
    user = await db.get_user(user_id)
    rows = await db.export_rows_for_user(user_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    for r in rows:
        local_started_at = timeutil.to_user_local(
            dt.datetime.fromisoformat(r["started_at"]), user
        )
        writer.writerow([
            local_started_at.isoformat(), r["exercise"], r["round_index"], r["weight"], r["reps"],
            "" if r["rpe"] is None else formatting.format_weight(r["rpe"]),
        ])
    return buf.getvalue().encode("utf-8-sig")
