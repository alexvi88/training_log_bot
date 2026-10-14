"""Полная выгрузка дневника одним ZIP-архивом — кнопка «📤 Экспорт» в
настройках бота, «📦 Сначала выгрузить» на экране удаления аккаунта и
`GET /v1/export/archive` у приложения. Один расчёт на бота и REST, как у
`csv_export.py`: архив, который человек забрал из бота и из приложения, обязан
быть одним и тем же.

Зачем не один CSV: в старом `training_log.csv` (`csv_export.build_csv`, его
ручка `/v1/export/csv` живёт для старых сборок) были только подходы
законченных тренировок. Вес тела, программы, заметки, свои упражнения, еда и
идущая тренировка при «Удалить аккаунт» стирались безвозвратно, хотя экран
удаления советовал «сначала выгрузи».

Что в архиве (каждый файл — CSV в `utf-8-sig`, как и старый экспорт):

- `workouts.csv` — по строке на подход; первые шесть колонок те же, что у
  старого файла, так что архивный файл читает и импорт (`handlers/csv_import.py`
  узнаёт и колонки заметок). Заметка к тренировке без подходов и заметка к
  упражнению без подходов — своей строкой с пустыми полями подхода.
- `bodyweight.csv`, `programs.csv`, `exercises.csv` (с псевдонимами).
- `food.csv` и `coach_comments.csv` — только если там что-то есть.
- `README.txt` — что в каком файле, на языке атлета (`users.lang`).

Только то, что ввёл сам атлет, и только из его аккаунта (все запросы — по
своему id, см. `db.iter_export_*`). Комментарии живого тренера к своим
тренировкам атлет видит в приложении — они идут текстом, без имени и id
тренера. Ответы AI-тренера (`workouts.ai_comment`, чат) — не его текст и в
архив не идут; как и фото (они живут в Telegram и на диске сервера).

Память: архив собирается в `SpooledTemporaryFile` (до `_SPOOL_MAX` в памяти,
дальше — на диске), строки из базы читаются порциями, CSV пишется прямо в
поток записи ZIP. Заголовки колонок — машинные, латиницей (как в
`csv_export.py`): это имена столбцов, а не проза.
"""

from __future__ import annotations

import contextlib
import csv
import datetime as dt
import io
import json
import tempfile
import zipfile
from typing import Any, Optional

import csv_export
import db
import formatting
import i18n
import seed_data
import timeutil

WORKOUTS_HEADER = [
    "started_at", "exercise", "round_index", "weight", "reps", "rpe",
    "unit", "finished_at", "status", "title", "workout_note", "exercise_note",
]
BODYWEIGHT_HEADER = ["logged_at", "weight", "unit"]
PROGRAMS_HEADER = [
    "program", "program_description", "program_source", "deload_every_weeks", "program_created_at",
    "day_order", "day", "exercise_order", "exercise", "target", "progression",
]
EXERCISES_HEADER = [
    "exercise", "catalog_name", "muscle_group", "equipment", "unilateral",
    "bodyweight_load", "notes", "description", "aliases", "archived", "created_at",
]
FOOD_HEADER = ["eaten_on", "description", "details", "calories", "protein", "fat", "carbs", "source", "created_at"]
COACH_COMMENTS_HEADER = ["workout_started_at", "written_at", "text"]

# Обязательные разделы — есть в каждом архиве, даже пустые (с одним заголовком):
# человек видит, что раздел выгружен и в нём ничего нет, а не гадает, забыли ли.
REQUIRED_FILES = ("README.txt", "workouts.csv", "bodyweight.csv", "programs.csv", "exercises.csv")
# Разделы «если есть»: заводятся, только когда в базе нашлась хоть одна строка.
OPTIONAL_FILES = ("food.csv", "coach_comments.csv")

_SPOOL_MAX = 8 * 1024 * 1024
_STREAM_CHUNK = 64 * 1024


def _text(value: Any) -> str:
    if value is None:
        return ""
    return csv_export.escape_formula_cell(str(value))


def _num(value: Any) -> str:
    if value is None:
        return ""
    return formatting.format_weight(value)


def _local(ts: Optional[str], user: Any) -> str:
    if not ts:
        return ""
    try:
        return timeutil.to_user_local(dt.datetime.fromisoformat(ts), user).isoformat()
    except ValueError:
        return ts


def archive_filename(user: Any) -> str:
    """`training_log_YYYY-MM-DD.zip` — дата по часам атлета, чтобы две выгрузки
    в разные дни не легли в «Файлы» одним именем."""
    return f"training_log_{timeutil.user_today(user).isoformat()}.zip"


class _CsvEntry:
    """Один CSV внутри ZIP: пишется строками прямо в поток архива."""

    def __init__(self, zf: zipfile.ZipFile, name: str, header: list[str]):
        self._raw = zf.open(name, "w", force_zip64=True)
        self._text = io.TextIOWrapper(self._raw, encoding="utf-8-sig", newline="")
        self._writer = csv.writer(self._text)
        self._writer.writerow(header)
        self.rows = 0

    def write(self, row: list[str]) -> None:
        self._writer.writerow(row)
        self.rows += 1

    def close(self) -> None:
        self._text.flush()
        self._text.detach()
        self._raw.close()


async def _write_workouts(zf: zipfile.ZipFile, user: Any, user_id: int) -> int:
    entry = _CsvEntry(zf, "workouts.csv", WORKOUTS_HEADER)
    unit = user["unit"]
    async for r in db.iter_export_workout_rows(user_id):
        has_set = r["reps"] is not None
        entry.write([
            _local(r["started_at"], user), _text(r["exercise"]),
            "" if r["round_index"] is None else r["round_index"],
            _num(r["weight"]), "" if r["reps"] is None else r["reps"], _num(r["rpe"]),
            unit if has_set else "", _local(r["finished_at"], user), r["status"],
            _text(r["title"]), _text(r["workout_note"]), _text(r["exercise_note"]),
        ])
    entry.close()
    return entry.rows


async def _write_bodyweight(zf: zipfile.ZipFile, user: Any, user_id: int) -> int:
    entry = _CsvEntry(zf, "bodyweight.csv", BODYWEIGHT_HEADER)
    async for r in db.iter_export_bodyweight_rows(user_id):
        entry.write([_local(r["logged_at"], user), _num(r["weight"]), user["unit"]])
    entry.close()
    return entry.rows


async def _write_programs(zf: zipfile.ZipFile, user: Any, user_id: int) -> int:
    entry = _CsvEntry(zf, "programs.csv", PROGRAMS_HEADER)
    async for r in db.iter_export_program_rows(user_id):
        progression = r["progression"] or ""
        if progression:
            # Схема лежит JSON'ом; в таблице — компактно и стабильно по ключам.
            with contextlib.suppress(ValueError):
                progression = json.dumps(json.loads(progression), ensure_ascii=False, sort_keys=True)
        entry.write([
            _text(r["program"]), _text(r["program_description"]), r["program_source"] or "",
            "" if r["deload_every_weeks"] is None else r["deload_every_weeks"],
            _local(r["created_at"], user),
            "" if r["day_id"] is None else r["day_order"], _text(r["day"]),
            "" if r["exercise_order"] is None else r["exercise_order"], _text(r["exercise"]),
            _text(r["target"]), _text(progression),
        ])
    entry.close()
    return entry.rows


async def _write_exercises(zf: zipfile.ZipFile, user: Any, user_id: int, lang: str) -> int:
    aliases = await db.export_exercise_aliases(user_id)
    entry = _CsvEntry(zf, "exercises.csv", EXERCISES_HEADER)
    async for r in db.iter_export_exercise_rows(user_id):
        group = r["group_name"]
        entry.write([
            _text(r["display_name"]), _text(r["original_name"]),
            _text(seed_data.localized_muscle_group_name(group, lang) if group else None),
            _text(r["equipment"]), int(bool(r["unilateral"])), r["bodyweight_load"] or "",
            _text(r["notes"]), _text(r["description"]),
            _text("; ".join(aliases.get(r["id"], []))), int(bool(r["is_archived"])),
            _local(r["created_at"], user),
        ])
    entry.close()
    return entry.rows


async def _write_optional(zf: zipfile.ZipFile, name: str, header: list[str], rows_iter, render) -> int:
    """Раздел «если есть»: файл заводится на первой строке, пустого нет."""
    entry: Optional[_CsvEntry] = None
    async for r in rows_iter:
        if entry is None:
            entry = _CsvEntry(zf, name, header)
        entry.write(render(r))
    if entry is None:
        return 0
    entry.close()
    return entry.rows


def _readme(lang: str, unit: str, present: set[str]) -> str:
    unit_label = i18n.t_in(lang, "unit.kg") if unit == "kg" else unit
    parts = [i18n.t_in(lang, "export.readme.intro", unit=unit_label)]
    parts.append(i18n.t_in(lang, "export.readme.workouts"))
    parts.append(i18n.t_in(lang, "export.readme.bodyweight"))
    parts.append(i18n.t_in(lang, "export.readme.programs"))
    parts.append(i18n.t_in(lang, "export.readme.exercises"))
    if "food.csv" in present:
        parts.append(i18n.t_in(lang, "export.readme.food"))
    if "coach_comments.csv" in present:
        parts.append(i18n.t_in(lang, "export.readme.coach_comments"))
    parts.append(i18n.t_in(lang, "export.readme.outro"))
    # CRLF: README открывают и в «Блокноте», который до сих пор путает LF.
    return "\r\n\r\n".join(p.replace("\n", "\r\n") for p in parts) + "\r\n"


async def build_archive(user_id: int) -> tuple[tempfile.SpooledTemporaryFile, str]:
    """Собрать архив атлета. Возвращает (файл, перемотанный в начало; имя).
    Файл закрывает вызывающий."""
    user = await db.get_user(user_id)
    lang = i18n.normalize(user["lang"] if user and user["lang"] else None)
    out = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX)  # noqa: SIM115 — закрывает вызывающий
    try:
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf, i18n.use_lang(lang):
            await _write_workouts(zf, user, user_id)
            await _write_bodyweight(zf, user, user_id)
            await _write_programs(zf, user, user_id)
            await _write_exercises(zf, user, user_id, lang)
            present: set[str] = set()
            food = await _write_optional(
                zf, "food.csv", FOOD_HEADER, db.iter_export_food_rows(user_id),
                lambda r: [
                    r["eaten_on"], _text(r["description"]), _text(r["details"]),
                    _num(r["calories"]), _num(r["protein"]), _num(r["fat"]), _num(r["carbs"]),
                    r["source"] or "", _local(r["created_at"], user),
                ],
            )
            if food:
                present.add("food.csv")
            comments = await _write_optional(
                zf, "coach_comments.csv", COACH_COMMENTS_HEADER,
                db.iter_export_coach_comment_rows(user_id),
                lambda r: [
                    _local(r["workout_started_at"], user), _local(r["created_at"], user), _text(r["text"]),
                ],
            )
            if comments:
                present.add("coach_comments.csv")
            zf.writestr("README.txt", _readme(lang, user["unit"], present).encode("utf-8-sig"))
    except BaseException:
        out.close()
        raise
    out.seek(0)
    return out, archive_filename(user)


async def build_archive_bytes(user_id: int) -> tuple[bytes, str]:
    """Архив целиком в памяти — для бота (Telegram принимает байты) и тестов."""
    f, name = await build_archive(user_id)
    with f:
        return f.read(), name


def iter_file(f, chunk: int = _STREAM_CHUNK):
    """Отдать файл кусками и закрыть его в конце — для StreamingResponse."""
    try:
        while True:
            data = f.read(chunk)
            if not data:
                return
            yield data
    finally:
        f.close()
