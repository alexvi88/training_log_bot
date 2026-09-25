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
    выключен явно и атлет разрешил передавать данные AI
    (common.ai_consent_given).

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
from handlers import csv_import as bot_csv_import
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
    # BOM (U+FEFF) в начале — обычное дело у CSV из Excel/Numbers: бот
    # снимает его декодированием utf-8-sig (handlers.csv_import), а сюда файл
    # приезжает уже строкой, и клиент мог декодировать его как простой utf-8.
    # strip() BOM не считает пробелом, и первая колонка заголовка («\ufeffдата»)
    # не узнавалась — вся строка заголовков принималась за данные.
    text = common.require(body, "csv", str).lstrip("\ufeff")
    if not text.strip():
        raise ApiError(400, "bad_request", "csv must not be empty", key="import.file_empty")
    if len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise ApiError(413, "csv_too_large", f"csv must be at most {MAX_CSV_BYTES} bytes")
    return text


def _parse_workouts(text: str, today: dt.date | None) -> list[dict]:
    """headers/rows/mapping/workouts — целиком через handlers.csv_import, в
    английской локали (см. докстринг модуля), чтобы ошибка при необходимости
    ушла клиенту не русской строкой. `today` — тот же смысл, что в боте
    (timeutil.user_today): дата "в будущем" сравнивается с местным днём
    пользователя, а не с UTC сервера."""
    # Язык человека (выставлен authed_user_id на весь запрос) — для поля
    # `message`, которое покажет приложение; машинное `detail` по-прежнему
    # собирается в английской локали (см. докстринг модуля).
    user_lang = i18n.get_lang()
    with i18n.use_lang("en"):
        headers, data_rows, has_header = _read_table(text)
        if not headers:
            raise ApiError(400, "bad_request", "csv file is empty", key="import.file_empty")
        if not data_rows:
            raise ApiError(400, "bad_request", "csv file has no data rows", key="import.no_data_rows")
        if len(headers) < len(REQUIRED_FIELDS):
            raise ApiError(
                400, "too_few_columns",
                f"found only {len(headers)} column(s), need at least date/exercise/weight/reps",
                key="import.too_few_columns", n=len(headers),
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
            detail = match.group(2) if match else e.message
            raise ApiError(
                400, "invalid_csv", detail,
                human=_localized_parse_error(data_rows, mapping, headers, has_header, today, user_lang),
            ) from e
        if not workouts:
            raise ApiError(400, "no_sets_found", "no row with a set was found", key="import.no_sets_found")
        return workouts


def _localized_parse_error(data_rows, mapping, headers, has_header, today, lang: str) -> str | None:
    """Та же ошибка разбора, но на языке человека: разбор уже упал в
    английской локали ради машинного `detail`, и повторить его под `lang` —
    единственный способ получить «Строка 3: отрицательный вес» без второй
    реализации сообщений. Повтор идёт только на уже упавшем файле."""
    with i18n.use_lang(lang):
        try:
            _build_workout_groups(
                data_rows, mapping,
                first_line=2 if has_header else 1,
                today=today,
                weight_factor=_weight_factor(headers, mapping),
            )
        except ParseError as e:
            return i18n.t("import.file_error", message=e.message)
    return None


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
    # Проверка дублей (_duplicate_dates) и запись (apply_import) не атомарны:
    # два одинаковых запроса подряд (двойной тап, повтор после таймаута)
    # оба видят пустую историю до того, как первый успел закоммитить, и
    # файл записывается дважды — 72 тренировки превращались в 144. Та же
    # бронь, что у кнопки «✅ Загрузить» в боте (общий с ботом _saving: один
    # процесс, один атлет не пишет импорт в двух местах сразу); ответ — тот
    # же 409 import_in_progress, что у import_share.
    if not bot_csv_import._try_claim_saving(user_id):
        raise ApiError(409, "import_in_progress", "an import for this account is already running")
    try:
        return await _do_import_csv(request, user_id)
    finally:
        bot_csv_import._saving.discard(user_id)


async def _do_import_csv(request: Request, user_id: int) -> JSONResponse:
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
        # Сопоставление через модель — не то, о чём человек просил (он жал
        # «Загрузить», а не спрашивал тренера), но названия из его файла
        # уходят стороннему AI. Без согласия (common.ai_consent_given) шаг
        # молча пропускается: имена заводятся как есть циклом ниже — тот же
        # исход, что и когда модель ничего не нашла.
        if common.ai_consent_given(request, user):
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
