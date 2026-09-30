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
  * имена упражнений предпросмотр и настоящий импорт резолвят ОДНОЙ
    функцией, handlers.csv_import.match_exercise_names, — без модели и без
    записи: точное имя → шаблон каталога и форк атлета от него по
    идентичности → похожее своё упражнение (порядок слов, пропущенное
    «штанги», словоформы, синонимы поиска) → однозначный шаблон. Раньше
    предпросмотр сверял только точные имена, а импорт — через модель или
    никак, и «Жим лёжа» при своём «Жим штанги лёжа» обещал «Завести новое»,
    а потом либо уезжал не туда, либо раскалывал историю на два упражнения.
    Что предпросмотр показал (или что атлет выбрал в exercise_mapping) —
    то импорт и делает. Модель (resolve через
    ai_trainer.match_exercise_names_to_catalog, с согласием атлета —
    api_v1_ai.has_ai_consent) зовётся только на коммите и только для имён,
    которые предпросмотр честно назвал новыми: она подбирает новому
    упражнению группу мышц и фото из каталога, но не сливает его со своим
    упражнением атлета — туда, куда предпросмотр не обещал.

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

import ai_limits
import ai_trainer
import api_v1_ai
import api_v1_common as common
import config
import db
import formatting
import i18n
import search_terms
import seed_data
import text_import
import timeutil
from handlers import csv_import as bot_csv_import
from handlers.csv_import import (
    REQUIRED_FIELDS,
    _auto_detect,
    _build_workout_groups,
    _duplicate_dates,
    _file_source,
    _file_weight_unit,
    _read_table,
    _weight_factor,
    apply_import,
    match_exercise_names,
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


def _body_file_unit(body: dict[str, Any]) -> str | None:
    """`file_unit` запроса: единица, в которой записан вес в файле ("kg"/"lb").
    Нет поля или null — единица определяется по заголовку файла."""
    value = body.get("file_unit")
    if value is None:
        return None
    if value not in ("kg", "lb"):
        raise ApiError(400, "bad_request", "file_unit must be 'kg' or 'lb'")
    return value


def _parse_workouts(
    text: str, today: dt.date | None, account_unit: str, file_unit: str | None = None,
) -> tuple[list[dict], dict]:
    """headers/rows/mapping/workouts — целиком через handlers.csv_import, в
    английской локали (см. докстринг модуля), чтобы ошибка при необходимости
    ушла клиенту не русской строкой. `today` — тот же смысл, что в боте
    (timeutil.user_today): дата "в будущем" сравнивается с местным днём
    пользователя, а не с UTC сервера.

    Второе значение — что разбор решил сам (пропущенные строки, как прочитан
    формат «a/b/гггг»), см. _build_workout_groups(stats=...)."""
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
        stats: dict = {
            "source": _file_source(headers, has_header),
            "file_unit_detected": _file_weight_unit(headers, mapping),
        }
        try:
            workouts = _build_workout_groups(
                data_rows, mapping,
                first_line=2 if has_header else 1,
                today=today,
                weight_factor=_weight_factor(headers, mapping, account_unit, file_unit),
                stats=stats,
                account_unit=account_unit,
            )
        except ParseError as e:
            match = _ERR_LINE_RE.match(e.message)
            detail = match.group(2) if match else e.message
            raise ApiError(
                400, "invalid_csv", detail,
                human=_localized_parse_error(
                    data_rows, mapping, headers, has_header, today, user_lang, account_unit,
                    file_unit,
                ),
            ) from e
        if not workouts:
            raise ApiError(400, "no_sets_found", "no row with a set was found", key="import.no_sets_found")
        return workouts, stats


# Порог правдоподобия веса. Просто и без справочника упражнений: рабочий вес
# свыше 200 кг в файле «в килограммах» почти всегда фунты, принятые за кг
# (225 lb — обычный жим); а файл «в фунтах», где самый тяжёлый вес меньше
# 45 lb (≈20 кг, пустой гриф), почти всегда килограммы. Чип только предлагает
# перепроверить — ничего не блокирует, штанга на 200+ кг бывает настоящей.
PLAUSIBLE_MAX_KG = 200
PLAUSIBLE_MIN_TOP_LB = 45


def _weight_warning(workouts: list[dict], account_unit: str, file_unit: str) -> dict | None:
    """Самый тяжёлый подход файла, если он выглядит нереалистично для
    выбранной единицы: {"kind": "maybe_lb"|"maybe_kg", "exercise", "weight"
    (в единице файла), "unit" (единица файла)}. Иначе None."""
    top_name, top_stored = None, 0.0
    for w in workouts:
        for entry in w["entries"]:
            for weight, _reps, _rpe in entry["sets"]:
                if weight > top_stored:
                    top_name, top_stored = entry["name"], weight
    if top_name is None:
        return None
    kg = formatting.to_kg(top_stored, account_unit)
    shown = round(kg if file_unit == "kg" else kg * config.LB_PER_KG, 1)
    if file_unit == "kg" and kg > PLAUSIBLE_MAX_KG:
        kind = "maybe_lb"
    elif file_unit == "lb" and shown < PLAUSIBLE_MIN_TOP_LB:
        kind = "maybe_kg"
    else:
        return None
    return {"kind": kind, "exercise": top_name, "weight": shown, "unit": file_unit}


MAX_PREVIEW_WORKOUTS = 500


def _preview_workouts(workouts: list[dict], account_unit: str) -> list[dict]:
    """Компактные строки предпросмотра: по упражнению — число подходов и
    самый тяжёлый подход (вес в обеих единицах, чтобы приложение показало
    «100 кг (220 lb)» без своей математики). Новые даты первыми."""
    rows = []
    for w in sorted(workouts, key=lambda x: x["date"], reverse=True)[:MAX_PREVIEW_WORKOUTS]:
        entries = []
        for entry in w["entries"]:
            top = max(entry["sets"], key=lambda s: (s[0], s[1]))
            kg = formatting.to_kg(top[0], account_unit)
            entries.append({
                "name": entry["name"],
                "sets": len(entry["sets"]),
                "top_weight_kg": round(kg, 1),
                "top_weight_lb": round(kg * config.LB_PER_KG, 1),
                "top_reps": top[1],
            })
        rows.append({"date": w["date"], "entries": entries})
    return rows


def _skipped_fields(stats: dict) -> dict[str, int]:
    """Сколько строк файла разбор пропустил сам и почему — только добавленные
    поля, прежние ключи ответа не меняются (их декодирует приложение):
    разминка (Strong «W», Hevy set_type=warmup) и строки без нагрузки (вес и
    повторы — ноль или пусто: кардио, планка). Ни то, ни другое не ошибка
    файла, но «в файле 300 строк, а подходов 240» без объяснения выглядит
    как потеря данных."""
    return {
        "rows_skipped": stats.get("rows_skipped", 0),
        "rows_skipped_warmup": stats.get("rows_skipped_warmup", 0),
        "rows_skipped_no_load": stats.get("rows_skipped_no_load", 0),
    }


def _localized_parse_error(
    data_rows, mapping, headers, has_header, today, lang: str, account_unit: str,
    file_unit: str | None = None,
) -> str | None:
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
                weight_factor=_weight_factor(headers, mapping, account_unit, file_unit),
                account_unit=account_unit,
            )
        except ParseError as e:
            return i18n.t("import.file_error", message=e.message)
    return None


def _date_range(workouts: list[dict]) -> dict[str, str] | None:
    if not workouts:
        return None
    dates = sorted(w["date"] for w in workouts)
    return {"from": dates[0], "to": dates[-1]}


async def _candidate_rows(matches: dict) -> dict[int, Any]:
    """id → строка упражнения для всех кандидатов предпросмотра — одним
    проходом, чтобы отдать клиенту их показанные имена."""
    ids = {i for m in matches.values() for i in m.candidates}
    rows = {}
    for ex_id in sorted(ids):
        row = await db.get_exercise(ex_id)
        if row is not None:
            rows[ex_id] = row
    return rows


async def _validated_mapping(body: dict[str, Any], user_id: int) -> dict[str, int | None] | None:
    """`exercise_mapping` коммита: имя из файла → id упражнения человека, или
    null — «заведи новое, даже если нашлось похожее». Нет поля — None: тогда
    импорт делает ровно то, что показал предпросмотр. Чужой, шаблонный,
    архивный или несуществующий id — 400 целиком, до любой записи: тихо
    пропущенная строка залила бы историю не в то упражнение."""
    raw = body.get("exercise_mapping")
    if raw is None:
        return None
    bad = ApiError(400, "invalid_exercise_mapping", "exercise_mapping has an unknown or foreign exercise id")
    # Тот же выбор списком пар [{"name", "exercise_id"}]: iOS-кодировщик
    # переписывает ключи словаря в snake_case («Bench Press» → «bench _press»),
    # а имена из файла трогать нельзя.
    if isinstance(raw, list):
        try:
            pairs = [(item["name"], item.get("exercise_id")) for item in raw]
        except (TypeError, KeyError, AttributeError):
            raise ApiError(400, "bad_request", "exercise_mapping items need name and exercise_id") from None
    elif isinstance(raw, dict):
        pairs = list(raw.items())
    else:
        raise ApiError(400, "bad_request", "exercise_mapping must be an object or a list")
    mapping: dict[str, int | None] = {}
    for name, ex_id in pairs:
        if not isinstance(name, str):
            raise bad
        if ex_id is None:
            mapping[name] = None
            continue
        if not isinstance(ex_id, int) or isinstance(ex_id, bool):
            raise bad
        ex = await db.get_exercise(ex_id)
        if ex is None or ex["user_id"] != user_id or ex["is_template"] or ex["is_archived"]:
            raise bad
        mapping[name] = ex_id
    return mapping


def _resolved_by_choice(matches: dict, mapping: dict[str, int | None] | None) -> dict[str, int]:
    """Имя → упражнение: решение предпросмотра, поверх которого — выбор
    человека. Точное совпадение имени не переспрашивается (предпросмотр его
    на выбор не выносит). Если клиент прислал exercise_mapping, он показал
    выбор по каждому неточному имени: имя, которого в нём нет, человек
    оставил на «Завести новое» (так кодирует выбор приложение — пустой пункт
    просто не отправляется), и подсказку за него не подставляем."""
    resolved = {n: m.exercise_id for n, m in matches.items() if m.exercise_id is not None}
    if mapping is None:
        return resolved
    for name, match in matches.items():
        if match.exact:
            continue
        chosen = mapping.get(name)
        if chosen is None:
            resolved.pop(name, None)
        else:
            resolved[name] = chosen
    return resolved


async def preview_csv(request: Request) -> JSONResponse:
    """Разобрать CSV и показать, что получится, БЕЗ записи в базу — заливать
    чужую историю вслепую нельзя. Упражнения размечены тем же резолвом, что у
    настоящего импорта (match_exercise_names, см. докстринг модуля)."""
    user_id = await common.authed_user_id(request)
    user = await db.get_user(user_id)
    body = await common.json_body(request)
    text = _csv_text(body)
    requested_unit = _body_file_unit(body)
    workouts, stats = _parse_workouts(
        text, timeutil.user_today(user), user["unit"], requested_unit,
    )
    # Единица, которой файл прочитан: выбор человека → заголовок → единица аккаунта.
    file_unit = requested_unit or stats["file_unit_detected"] or user["unit"]

    all_names = [entry["name"] for w in workouts for entry in w["entries"]]
    matches = await match_exercise_names(user_id, all_names)
    resolved = _resolved_by_choice(matches, None)
    exercises = [
        {
            "name": name,
            "status": "existing" if name in resolved else "new_will_create",
            "exercise_id": resolved.get(name),
        }
        for name in matches
    ]
    set_count = sum(len(entry["sets"]) for w in workouts for entry in w["entries"])
    dup = await _duplicate_dates(user_id, workouts, resolved)
    rows = await _candidate_rows(matches)

    return JSONResponse(
        {
            "workout_count": len(workouts),
            "set_count": set_count,
            "date_range": _date_range(workouts),
            "exercises": exercises,
            # Имена без точного совпадения: клиент даёт выбрать своё
            # упражнение (commit → exercise_mapping). suggested_exercise_id —
            # уверенное совпадение (импорт без exercise_mapping положит
            # подходы именно туда) или null — тогда заведётся новое;
            # candidates — похожие свои упражнения, лучшие первыми, для
            # выбора при неоднозначности.
            "unrecognized_exercises": [
                {
                    "name": n,
                    "suggested_exercise_id": m.exercise_id,
                    "candidates": [
                        {"exercise_id": i, "name": rows[i]["display_name"]}
                        for i in m.candidates if i in rows
                    ],
                }
                for n, m in matches.items() if not m.exact
            ],
            "duplicate_dates": sorted(dup),
            # Как прочитан вес: source — "strong" | "hevy" | "other";
            # file_unit_detected — что сказал заголовок ("kg"/"lb"/null);
            # file_unit — единица, которой файл прочитан (выбор человека,
            # иначе заголовок, иначе единица аккаунта); account_unit — в чём
            # вес ляжет в историю. weight_warning — см. _weight_warning.
            "source": stats["source"],
            "file_unit_detected": stats["file_unit_detected"],
            "file_unit": file_unit,
            "account_unit": user["unit"],
            "weight_warning": _weight_warning(workouts, user["unit"], file_unit),
            "workouts": _preview_workouts(workouts, user["unit"]),
            "workouts_truncated": len(workouts) > MAX_PREVIEW_WORKOUTS,
            **_skipped_fields(stats),
            # Как прочитаны даты вида «a/b/гггг»: "mdy" — колонка доказала
            # американский формат, "dmy" — европейский или (при
            # date_order_ambiguous=true) не доказала ничего, и остался
            # прежний д/м; null — таких дат в файле нет. Неоднозначный случай
            # стоит показать человеку рядом с date_range: «03/04/2024» легло
            # 3 апреля, а в его приложении это могло быть 4 марта.
            "date_order": stats.get("date_order"),
            "date_order_ambiguous": stats.get("date_order_ambiguous", False),
        }
    )


async def _create_new_exercises(
    request: Request, user_id: int, names: list[str], matches: dict, resolved: dict[str, int],
) -> None:
    """Завести упражнения, которые предпросмотр назвал новыми, — под именем
    из файла, дописывая их в resolved.

    Однозначный шаблон каталога (match.template_name) решён ещё
    предпросмотром — новое упражнение привязывается к нему (группа, фото,
    техника) без модели. Остальные — модели, если атлет согласен передавать
    данные AI: она подбирает шаблон по смыслу. Шаблон, чья идентичность у
    атлета уже есть (или только что заведена этим же импортом), второй раз
    не привязывается — два упражнения с одной идентичностью и есть дубль,
    который раскалывает историю; вместо этого имя ложится в уже заведённое
    этим импортом, а своё давнее — только если его выбрал человек или
    предпросмотр. Кого не узнал никто — в группу «Другое» (без группы
    упражнения не бывает — его не было бы видно в «Моих упражнениях»),
    чтобы create_missing_exercises=true не терял тренировки молча."""
    owned = {
        search_terms.fold(r["original_name"]) for r in await db.list_active_exercises_with_identity(user_id)
    }
    created: dict[str, int] = {}

    async def link(name: str, template_name: str) -> bool:
        key = search_terms.fold(template_name)
        if key in created:
            resolved[name] = created[key]
            return True
        if key in owned:
            return False
        ex_id = await db.create_exercise_matching_catalog_name(user_id, name, template_name)
        if ex_id is None:
            return False
        created[key] = ex_id
        resolved[name] = ex_id
        return True

    # Несколько имён файла на один новый шаблон — одно упражнение; первым
    # заводится то, что написано ровно как шаблон, — его имя и останется.
    ordered = sorted(names, key=lambda n: not _spelled_as(n, matches[n].template_name))
    rest = []
    for name in ordered:
        template_name = matches[name].template_name
        if not (template_name and await link(name, template_name)):
            rest.append(name)
    if rest and await api_v1_ai.has_ai_consent(request, user_id):
        aliases = await ai_trainer.match_exercise_names_to_catalog(user_id, rest)
        templates = await db.find_global_templates_by_names(list(set(aliases.values())))
        for name, catalog_name in aliases.items():
            template = templates.get(catalog_name)
            if template is not None:
                await link(name, template["name"])
    other_group_id = None
    for name in rest:
        if name in resolved:
            continue
        if other_group_id is None:
            other_group_id = await db.other_muscle_group_id()
        resolved[name] = await db.create_exercise(user_id, name, other_group_id)


def _spelled_as(name: str, template_name: str | None) -> bool:
    if not template_name:
        return False
    folded = search_terms.fold(name.strip())
    return folded == search_terms.fold(template_name) or any(
        folded == search_terms.fold(seed_data.localized_exercise_name(template_name, lang))
        for lang in i18n.SUPPORTED
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
    mapping = await _validated_mapping(body, user_id)
    workouts, stats = _parse_workouts(
        text, timeutil.user_today(user), user["unit"], _body_file_unit(body),
    )

    all_names = [entry["name"] for w in workouts for entry in w["entries"]]
    # Тот же резолв, что показал предпросмотр, и выбор человека поверх него.
    matches = await match_exercise_names(user_id, all_names)
    resolved = _resolved_by_choice(matches, mapping)
    unresolved = [n for n in matches if n not in resolved]
    if unresolved and create_missing:
        await _create_new_exercises(request, user_id, unresolved, matches, resolved)
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
            **_skipped_fields(stats),
        }
    )


# Разбор текста — один на атлета за раз: второй тап по «Разобрать» (или
# повтор после таймаута) иначе заплатил бы модели за тот же текст дважды.
_converting: set[int] = set()


async def convert_text(request: Request) -> JSONResponse:
    """Произвольный текст (заметки, тетрадь, чат) → CSV для /import/csv.

    Ничего не пишет: модель собирает только черновик строк «дата, упражнение,
    вес, повторы» (text_import.py), а приложение отправляет полученный `csv`
    в обычные /import/csv/preview и /import/csv — предпросмотр, дубли и
    сопоставление с каталогом у текста те же, что у файла Hevy/Strong.

    В отличие от CSV модель здесь не необязательный шаг, а весь разбор:
    без согласия на передачу данных AI — 403 `ai_consent_required`, как у
    ручек тренера. Личной квоты нет (разбор нужен раз при переезде, а не
    каждый день), от расхода держат потолок длины текста и суточный
    HARD-стоп по деньгам (ai_limits.hard_stop_block, как у комментария к
    тренировке).

    `undated_sets` — сколько подходов модель нашла, но не смогла привязать
    к дню: в CSV без даты их не положить, и приложение говорит о них, а не
    теряет молча.
    """
    user_id = await common.authed_user_id(request)
    body = await common.json_body(request)
    text = common.require(body, "text", str).strip()
    if not text:
        raise ApiError(400, "bad_request", "text must not be empty", key="import.text_empty")
    if len(text) > text_import.MAX_TEXT_CHARS:
        raise ApiError(
            413, "text_too_large", f"text must be at most {text_import.MAX_TEXT_CHARS} characters",
            key="import.text_too_large",
        )
    await api_v1_ai._require_ai_consent(request, user_id)
    if not ai_trainer.is_configured():
        raise ApiError(503, "not_configured", "ai trainer is not configured")
    if user_id in _converting:
        raise ApiError(409, "import_in_progress", "a text import for this account is already being parsed")
    block = await ai_limits.hard_stop_block()
    if block is not None:
        raise ApiError(429, "spend_limit_exceeded", "daily AI spend limit reached", human=block.user_text)

    user = await db.get_user(user_id)
    _converting.add(user_id)
    try:
        result = await text_import.extract_sets(user_id, text, timeutil.user_today(user))
    except Exception as e:
        raise ApiError(502, "text_import_failed", "model failed to parse the text") from e
    finally:
        _converting.discard(user_id)

    if not result.rows:
        if result.undated:
            raise ApiError(400, "no_sets_found", "sets found but none has a date", key="import.text_no_dates")
        raise ApiError(400, "no_sets_found", "no set was found in the text", key="import.text_no_sets")
    return JSONResponse(
        {
            "csv": text_import.rows_to_csv(result.rows, user["unit"]),
            "set_count": len(result.rows),
            "undated_sets": result.undated,
        }
    )


routes = [
    Route("/import/csv/preview", preview_csv, methods=["POST"]),
    Route("/import/csv", import_csv, methods=["POST"]),
    Route("/import/text/convert", convert_text, methods=["POST"]),
]
