"""REST `/v1` для импорта истории тренировок из CSV (в боте — флоу
«📥 Импорт CSV», handlers/csv_import.py).

Транспорт поверх уже написанного и покрытого тестами разбора: колонки,
форматы дат, потолки веса/повторов (parser.MAX_WEIGHT/MAX_REPS), группировка
строк в тренировки и резолв упражнений по имени зовутся отсюда, а не пишутся
второй раз (handlers.csv_import._read_table/_auto_detect/_build_workout_groups
и resolve_exercise_names_*/apply_import — часть из них выделены туда именно
ради этого модуля, см. коммит).

Отличия от бота, все — вынужденные, потому что у REST нет интерактивного
чата:
  * колонки должны определиться автоматически (_auto_detect); ручного
    маппинга «какая колонка это вес» здесь нет — файл без узнаваемых
    заголовков отклоняется 400 с кодом unrecognized_columns;
  * ошибка разбора у parser.ParseError/handlers.csv_import собрана в готовую
    локализованную строку ("Строка N: ..." на языке пользователя). Чтобы не
    трогать сам разбор ради машинного формата, сообщение строится в
    английской локали (i18n.use_lang("en")) и номер строки вынимается из
    неё регуляркой — это тот же текст, что увидел бы англоязычный
    пользователь бота, а не отдельная русская строка;
  * матчинг незнакомых названий упражнений через модель
    (resolve_exercise_names_via_ai) — сетевой вызов с побочным эффектом
    (создаёт упражнение в базе), поэтому препросмотр (`/import/csv/preview`)
    его не зовёт вовсе: непопавшие в каталог по точному имени помечены
    «будет создано» безусловно, без обращения к модели. Настоящий импорт
    (`/import/csv`) зовёт его как и бот, если create_missing_exercises не
    выключен явно.

AI-обзор истории после импорта (ai_trainer.import_history_overview) сюда
нарочно не подключён: в боте это отдельное сообщение, отправляемое в чат
фоном уже после ответа на запрос — у REST нет чата, куда его слать.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import api_v1_common as common
import config
import db
import i18n
import timeutil
from handlers.csv_import import (
    REQUIRED_FIELDS,
    _auto_detect,
    _build_workout_groups,
    _duplicate_dates,
    _read_table,
    _weight_factor,
    apply_import,
    resolve_exercise_names_exact,
    resolve_exercise_names_via_ai,
)
from parser import ParseError

ApiError = common.ApiError

# Тот же потолок, что у скачивания документа ботом: Bot API вообще не отдаёт
# файл крупнее этого боту (см. config.MAX_VIDEO_BYTES) — так что для CSV,
# который бот тоже получает как telegram-документ, ставить свой, отдельный
# лимит незачем, он и так упёрся бы в этот же.
MAX_CSV_BYTES = config.MAX_VIDEO_BYTES

_ERR_LINE_RE = re.compile(r"^Line (\d+): (.*)$", re.DOTALL)


def _csv_text(body: dict[str, Any]) -> str:
    text = common.require(body, "csv", str)
    if not text.strip():
        raise ApiError(400, "bad_request", "csv must not be empty")
    if len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise ApiError(413, "csv_too_large", f"csv must be at most {MAX_CSV_BYTES} bytes")
    return text


def _parse_workouts(text: str, today: dt.date | None) -> list[dict]:
    """headers/rows/mapping/workouts — целиком через handlers.csv_import, в
    английской локали (см. докстринг модуля), чтобы ошибка при необходимости
    ушла клиенту не русской строкой. `today` — тот же смысл, что в боте
    (timeutil.user_today): дата "в будущем" сравнивается с местным днём
    пользователя, а не с UTC сервера."""
    with i18n.use_lang("en"):
        headers, data_rows, has_header = _read_table(text)
        if not headers:
            raise ApiError(400, "bad_request", "csv file is empty")
        if not data_rows:
            raise ApiError(400, "bad_request", "csv file has no data rows")
        if len(headers) < len(REQUIRED_FIELDS):
            raise ApiError(
                400, "too_few_columns",
                f"found only {len(headers)} column(s), need at least date/exercise/weight/reps",
            )
        mapping = _auto_detect(headers)
        missing = [f for f in REQUIRED_FIELDS if f not in mapping]
        if missing:
            # Ручного маппинга колонок тут нет (см. докстринг модуля) —
            # заголовки файла должны узнаваться сами по SYNONYMS.
            raise ApiError(
                400, "unrecognized_columns",
                "could not auto-detect column(s): " + ", ".join(missing),
            )
        try:
            workouts = _build_workout_groups(
                data_rows, mapping,
                first_line=2 if has_header else 1,
                today=today,
                weight_factor=_weight_factor(headers, mapping),
            )
        except ParseError as e:
            match = _ERR_LINE_RE.match(e.message)
            if match:
                raise ApiError(400, "invalid_csv", match.group(2)) from e
            raise ApiError(400, "invalid_csv", e.message) from e
        if not workouts:
            raise ApiError(400, "no_sets_found", "no row with a set was found")
        return workouts


def _date_range(workouts: list[dict]) -> dict[str, str] | None:
    if not workouts:
        return None
    dates = sorted(w["date"] for w in workouts)
    return {"from": dates[0], "to": dates[-1]}


async def preview_csv(request: Request) -> JSONResponse:
    """Разобрать CSV и показать, что получится, БЕЗ записи в базу — заливать
    чужую историю вслепую нельзя. Упражнения размечены по точному совпадению
    имени с каталогом пользователя (см. докстринг модуля про
    resolve_exercise_names_exact vs _via_ai)."""
    user_id = await common.authed_user_id(request)
    user = await db.get_user(user_id)
    body = await common.json_body(request)
    text = _csv_text(body)
    workouts = _parse_workouts(text, timeutil.user_today(user))

    all_names = [entry["name"] for w in workouts for entry in w["entries"]]
    resolved, unresolved = await resolve_exercise_names_exact(user_id, all_names)
    exercises = [
        {"name": name, "status": "existing" if name in resolved else "new_will_create"}
        for name in dict.fromkeys(all_names)
    ]
    set_count = sum(len(entry["sets"]) for w in workouts for entry in w["entries"])
    dup = await _duplicate_dates(user_id, workouts, resolved)

    return JSONResponse(
        {
            "workout_count": len(workouts),
            "set_count": set_count,
            "date_range": _date_range(workouts),
            "exercises": exercises,
            "duplicate_dates": sorted(dup),
        }
    )


async def import_csv(request: Request) -> JSONResponse:
    """Настоящий импорт: пишет тренировки в базу и возвращает, сколько
    записалось. Даты, на которые уже есть завершённая тренировка с тем же
    упражнением (см. handlers.csv_import._duplicate_dates), молча
    пропускаются — повторная заливка того же файла не плодит дубли; это то
    же поведение, что у кнопки "✅ Загрузить" в боте (не "Загрузить все")."""
    user_id = await common.authed_user_id(request)
    user = await db.get_user(user_id)
    body = await common.json_body(request)
    text = _csv_text(body)
    create_missing = body.get("create_missing_exercises", True)
    if not isinstance(create_missing, bool):
        raise ApiError(400, "bad_request", "create_missing_exercises must be a boolean")
    workouts = _parse_workouts(text, timeutil.user_today(user))

    all_names = [entry["name"] for w in workouts for entry in w["entries"]]
    resolved, unresolved = await resolve_exercise_names_exact(user_id, all_names)
    if unresolved and create_missing:
        ai_resolved = await resolve_exercise_names_via_ai(user_id, unresolved)
        resolved.update(ai_resolved)
        unresolved = [n for n in unresolved if n not in resolved]
        # Модель не нашла шаблон каталога вовсе — в боте это идёт на ручное
        # разрешение (handlers/exercise_resolve.py), которого у REST нет;
        # заводим упражнение как есть, под именем из файла, без группы мышц,
        # чтобы create_missing_exercises=true не терял тренировки молча.
        for name in unresolved:
            ex_id = await db.create_exercise(user_id, name, None)
            resolved[name] = ex_id
        unresolved = []

    # Тренировки, в которых есть хоть одно неразрешённое имя (только при
    # create_missing_exercises=false), не записываем целиком — иначе часть
    # подходов внутри одной тренировки тихо пропала бы, а дата уже считалась
    # бы «занята импортом» для follow-up загрузки того же файла.
    skipped_exercises = sorted(unresolved)
    importable = [
        w for w in workouts
        if all(entry["name"] in resolved for entry in w["entries"])
    ]

    dup = await _duplicate_dates(user_id, importable, resolved)
    to_import = [w for w in importable if w["date"] not in dup]

    imported, failed = await apply_import(user_id, to_import, resolved)

    # apply_import не говорит, КАКИЕ именно тренировки сорвались (см. её
    # докстринг) — сумма подходов по to_import точна, когда failed == 0
    # (обычный случай), а при сбое чуть завышена на подходы сорвавшейся
    # тренировки; для отчёта клиенту это приемлемо, workouts_failed рядом
    # показывает, что часть не долетела.
    return JSONResponse(
        {
            "workouts_imported": imported,
            "sets_imported": sum(
                len(entry["sets"]) for w in to_import for entry in w["entries"]
            ) if imported else 0,
            "workouts_skipped_duplicate": len(dup),
            "workouts_failed": failed,
            "workouts_skipped_unresolved_exercise": len(workouts) - len(importable),
            "skipped_exercises": skipped_exercises,
        }
    )


routes = [
    Route("/import/csv/preview", preview_csv, methods=["POST"]),
    Route("/import/csv", import_csv, methods=["POST"]),
]
