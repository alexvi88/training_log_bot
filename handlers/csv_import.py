"""§A3 — CSV import (round-trip with the §9 export): дата, упражнение, вес, повторы[, подход]."""

import csv
import datetime as dt
import io
import re
from typing import Optional

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import achievement_sync
import ai_trainer
import db
import formatting
import keyboards
import ui
from fsm import ImportFlow
from parser import MAX_REPS, MAX_WEIGHT, ParseError, parse_ru_date
from state_scaffold import clear_state_keep_ai

router = Router(name="csv_import")

REQUIRED_FIELDS = ["date", "exercise", "weight", "reps"]
FIELD_LABELS = {"date": "дата", "exercise": "упражнение", "weight": "вес", "reps": "повторы", "round": "номер подхода"}
# Кандидаты в разделители: свой экспорт пишет запятую, но «Сохранить как CSV»
# в русском Excel даёт «;» (и запятую внутри дробей), а Google Sheets — табы.
DELIMITERS = ",;\t|"
# start_time/exercise_title/weight_kg/set_index/set_type — колонки родного
# экспорта Hevy: самого частого источника миграции. Без них файл оттуда не
# автоопределялся ни по одному из четырёх обязательных полей и упирался в
# ручной маппинг с нуля.
SYNONYMS = {
    "date": {"дата", "date", "started_at", "start_time"},
    "exercise": {"упражнение", "exercise", "exercise_title"},
    "weight": {"вес", "weight", "weight_kg"},
    "reps": {"повторы", "reps"},
    "round": {"подход", "раунд", "round", "set", "round_index", "set_index"},
    "rpe": {"rpe", "рпе"},
}


@router.callback_query(F.data == "settings:import")
async def import_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ImportFlow.awaiting_file)
    await ui.safe_edit(
        callback,
        "📥 Пришли CSV-файл с колонками «дата, упражнение, вес, повторы».\n\n"
        "Также подойдёт импорт из Hevy: в Hevy — ⚙️ Settings → Export & Import "
        "Data, экспорт придёт на почту файлом CSV — скачай его и пришли мне сюда.",
        reply_markup=keyboards.cancel_keyboard("imp:cancel"),
    )
    await callback.answer()


def _auto_detect(headers: list[str]) -> dict[str, int]:
    lowered = [h.strip().lower() for h in headers]
    mapping: dict[str, int] = {}
    for field, names in SYNONYMS.items():
        for idx, h in enumerate(lowered):
            if h in names:
                mapping[field] = idx
                break
    return mapping


def _sniff_delimiter(text: str) -> str:
    """Чем в этом файле разделены колонки.

    Раньше разделитель был жёстко зашит в запятую, и файл из русского Excel
    («02.01.2025;Жим лёжа;100,5;8») выглядел как одна колонка: человек проходил
    четыре шага маппинга и получал «не понял дату» — про «;» ни слова.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()][:20]
    sample = "\n".join(lines)
    try:
        return csv.Sniffer().sniff(sample, delimiters=DELIMITERS).delimiter
    except csv.Error:
        pass
    # Sniffer сдаётся на коротких файлах (одна строка данных — обычное дело при
    # «проверю на маленьком примере»), поэтому берём самый частый в первой строке.
    first = lines[0] if lines else ""
    counts = {d: first.count(d) for d in DELIMITERS}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def _looks_like_data(row: list[str]) -> bool:
    """Похожа ли строка на данные, а не на заголовки.

    Признак — читаемая дата в любой из ячеек: названия колонок датами не бывают.
    Без этой проверки первая строка файла без заголовков уходила в headers и
    первая тренировка исчезала молча (два подхода в файле → импортирован один).
    """
    for cell in row:
        try:
            _parse_row_date(cell)
        except ParseError:
            continue
        return True
    return False


def _read_table(text: str) -> tuple[list[str], list[list[str]], bool]:
    """(заголовки, строки данных, была ли в файле строка заголовков).

    У файла без заголовков колонки безымянные, поэтому подписываем их номерами —
    спросить «какая колонка это вес» всё равно нужно, а терять первую строку нет.
    """
    reader = csv.reader(io.StringIO(text), delimiter=_sniff_delimiter(text))
    rows = [r for r in reader if r and any(c.strip() for c in r)]
    if not rows:
        return [], [], False
    # Дата решает: в строке заголовков её не бывает, а в строке данных она есть
    # всегда — иначе импортировать всё равно нечего.
    if not _looks_like_data(rows[0]):
        return rows[0], rows[1:], True
    width = max(len(r) for r in rows)
    return [f"Колонка {i + 1}" for i in range(width)], rows, False


async def _ask_next_mapping(event, state: FSMContext) -> bool:
    """Returns True if a mapping question was asked, False if mapping is complete."""
    data = await state.get_data()
    pending = list(data.get("imp_pending_fields") or [])
    if not pending:
        return False
    field = pending[0]
    headers = data["imp_headers"]
    await state.set_state(ImportFlow.mapping_columns)
    kb = keyboards.csv_column_options_keyboard(headers, prefix=f"impcol:{field}")
    # "шаг N из M" so the flow has a visible end — REQUIRED_FIELDS minus the ones
    # auto-detected from the header row.
    total = len(data.get("imp_mapping_total") or pending)
    step = total - len(pending) + 1
    text = (
        f"Шаг {step} из {total}. Какая колонка соответствует полю «{FIELD_LABELS[field]}»?\n"
        f"Колонки файла: {', '.join(headers)}"
    )
    # Без строки заголовков «Колонка 2» ни о чём не говорит — показываем первую
    # строку данных, по ней видно, где что.
    if not data.get("imp_has_header", True):
        sample = data.get("imp_sample_row") or []
        text = (
            "В файле нет строки заголовков — спрошу про колонки по номерам.\n\n"
            + text
            + (f"\nПервая строка: {' | '.join(sample)}" if sample else "")
        )
    if isinstance(event, CallbackQuery):
        await ui.safe_edit(event, text, reply_markup=kb)
    else:
        await event.answer(text, reply_markup=kb)
    return True


@router.message(StateFilter(ImportFlow.awaiting_file), F.document)
async def import_file_received(message: Message, state: FSMContext):
    document = message.document
    if not document.file_name.lower().endswith(".csv"):
        await message.reply("Нужен файл с расширением .csv")
        return
    buf = await message.bot.download(document)
    raw = buf.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")

    headers, data_rows, has_header = _read_table(text)
    if not headers:
        await message.reply("Файл пустой.")
        return
    if not data_rows:
        await message.reply("В файле нет строк с данными.")
        return
    if len(headers) < len(REQUIRED_FIELDS):
        # Обычно это не «файл из одной колонки», а неугаданный разделитель —
        # лучше сказать это сразу, чем после четырёх шагов маппинга.
        await message.reply(
            f"Нашёл всего {len(headers)} {formatting.plural_ru(len(headers), ('колонку', 'колонки', 'колонок'))}, "
            "а нужны хотя бы дата, упражнение, вес и повторы.\n"
            "Проверь разделитель: запятая, «;» или табуляция."
        )
        return

    mapping = _auto_detect(headers)
    pending = [f for f in REQUIRED_FIELDS if f not in mapping]
    await state.update_data(
        imp_headers=headers, imp_rows=data_rows, imp_mapping=mapping, imp_pending_fields=pending,
        imp_mapping_total=len(pending), imp_answered_fields=[],
        imp_has_header=has_header, imp_sample_row=data_rows[0],
    )
    if not await _ask_next_mapping(message, state):
        await _finish_mapping(message, state)


@router.message(StateFilter(ImportFlow.awaiting_file))
async def import_file_missing(message: Message, state: FSMContext):
    await message.reply("Пришли CSV-файл документом (не текстом).")


@router.callback_query(StateFilter(ImportFlow.mapping_columns), F.data.startswith("impcol:"))
async def import_column_picked(callback: CallbackQuery, state: FSMContext):
    _, field, idx_str = callback.data.split(":")
    data = await state.get_data()
    mapping = dict(data["imp_mapping"])
    mapping[field] = int(idx_str)
    pending = [f for f in data.get("imp_pending_fields") or [] if f != field]
    answered = list(data.get("imp_answered_fields") or []) + [field]
    await state.update_data(
        imp_mapping=mapping, imp_pending_fields=pending, imp_answered_fields=answered
    )
    if not await _ask_next_mapping(callback, state):
        await _finish_mapping(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(ImportFlow.mapping_columns), F.data == "imp:mapback")
async def import_mapping_back(callback: CallbackQuery, state: FSMContext):
    """Undo the last column choice and ask it again — a mistap here used to be
    unrecoverable."""
    data = await state.get_data()
    answered = list(data.get("imp_answered_fields") or [])
    if not answered:
        await callback.answer("Это первый вопрос — выйти можно кнопкой «Отмена»")
        return
    field = answered.pop()
    mapping = {k: v for k, v in dict(data["imp_mapping"]).items() if k != field}
    pending = [field] + list(data.get("imp_pending_fields") or [])
    await state.update_data(
        imp_mapping=mapping, imp_pending_fields=pending, imp_answered_fields=answered
    )
    await _ask_next_mapping(callback, state)
    await callback.answer()


# Hevy пишет дату/время английским месяцем-аббревиатурой («7 Aug 2026, 08:27»,
# без ведущего нуля у дня) — своего формата в parse_ru_date/ISO для этого нет.
# Список руками, а не через locale/strptime («%b»): locale контейнера решает,
# на каком языке страптайм ждёт месяц, и «Aug» на сервере с русской локалью
# незаметно перестал бы разбираться.
_MONTH_ABBR_EN = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# Hevy пишет дату на языке телефона, а не только по-английски — на русской
# локали это "10 авг. 2026, 19:21" (с точкой после сокращения месяца, которой
# нет у английского варианта).
_MONTH_ABBR_RU = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
_MONTH_ABBR = {**_MONTH_ABBR_EN, **_MONTH_ABBR_RU}
_HEVY_DATE_RE = re.compile(
    r"^(?P<d>\d{1,2}) (?P<mon>[A-Za-zА-Яа-яЁё]{3})\.? (?P<y>\d{4})(?:,\s*\d{1,2}:\d{2})?$"
)


def _parse_row_date(text: str) -> dt.date:
    text = text.strip()
    try:
        return parse_ru_date(text)
    except ParseError:
        pass
    match = _HEVY_DATE_RE.match(text)
    if match and match["mon"].lower() in _MONTH_ABBR:
        try:
            return dt.date(int(match["y"]), _MONTH_ABBR[match["mon"].lower()], int(match["d"]))
        except ValueError:
            raise ParseError(f"не понял дату «{text}»") from None
    try:
        if "t" in text.lower():
            return dt.datetime.fromisoformat(text).date()
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        raise ParseError(f"не понял дату «{text}»") from None


def _parse_number(text: str) -> float:
    """Дробное из ячейки: «100.5», «100,5» и «1 000» — одно и то же число.

    Запятая как десятичный разделитель — норма для русской локали Excel, а
    пробел там же приезжает разрядным разделителем.
    """
    return float(text.replace(",", ".").replace(" ", "").replace("\xa0", ""))


def _parse_count(text: str, label: str) -> int:
    """Целое из ячейки, терпимое к «8.0».

    Таблицы хранят числа float'ами и охотно пишут «8.0» в повторах — на этом
    раньше падал весь импорт целиком («не разобрал вес/повторы»), хотя восемь
    повторов тут читаются однозначно. А вот «8.5» — уже настоящая ошибка.
    """
    try:
        value = _parse_number(text)
    except ValueError:
        raise ParseError(f"не понял {label} «{text}»") from None
    if value != int(value):
        raise ParseError(f"{label}: «{text}» — не целое число")
    return int(value)


def _build_workout_groups(rows: list[list[str]], mapping: dict[str, int], first_line: int = 2) -> list[dict]:
    groups: dict[str, dict[str, list[tuple]]] = {}
    name_order: dict[str, list[str]] = {}
    date_order: list[str] = []

    for line_no, row in enumerate(rows, start=first_line):
        if not row or all(not c.strip() for c in row):
            continue
        try:
            date_val = _parse_row_date(row[mapping["date"]])
            name = row[mapping["exercise"]].strip()
            weight_text = row[mapping["weight"]].strip()
            try:
                weight = _parse_number(weight_text) if weight_text else 0.0
            except ValueError:
                raise ParseError(f"не понял вес «{weight_text}»") from None
            reps = _parse_count(row[mapping["reps"]].strip(), "повторы")
            round_val = None
            if "round" in mapping:
                round_text = row[mapping["round"]].strip()
                round_val = _parse_count(round_text, "номер подхода") if round_text else None
            rpe_val = None
            if "rpe" in mapping and mapping["rpe"] < len(row):
                rpe_text = row[mapping["rpe"]].strip()
                if rpe_text:
                    try:
                        rpe_val = _parse_number(rpe_text)
                    except ValueError:
                        raise ParseError(f"не понял RPE «{rpe_text}»") from None
                    if not (0 < rpe_val <= 10):
                        raise ParseError(f"RPE вне диапазона 1-10: «{rpe_text}»")
        except ParseError as e:
            raise ParseError(f"Строка {line_no}: {e.message}") from None
        except IndexError:
            # Обрезанная строка: раньше это тоже было «не разобрал вес/повторы»,
            # хотя искать нужно не число, а недостающую колонку.
            raise ParseError(f"Строка {line_no}: колонок меньше, чем нужно (в строке {len(row)})") from None
        except ValueError:
            raise ParseError(f"Строка {line_no}: не разобрал вес/повторы") from None

        if not name:
            raise ParseError(f"Строка {line_no}: пустое название упражнения")
        if reps <= 0:
            raise ParseError(f"Строка {line_no}: повторы должны быть больше 0")
        # Same ceilings the typed-set parser enforces, and for the same reason:
        # an impossible set imported here is silently permanent — it becomes the
        # exercise's all-time record, joins lifetime tonnage, and unlocks weight
        # clubs that are never revoked. A stray column or a units mix-up in
        # someone else's export is exactly how that gets in.
        if reps > MAX_REPS:
            raise ParseError(f"Строка {line_no}: слишком много повторов ({reps})")
        if weight < 0:
            raise ParseError(f"Строка {line_no}: отрицательный вес ({weight_text})")
        if weight > MAX_WEIGHT:
            raise ParseError(f"Строка {line_no}: слишком большой вес ({weight_text})")

        date_iso = date_val.isoformat()
        if date_iso not in groups:
            groups[date_iso] = {}
            name_order[date_iso] = []
            date_order.append(date_iso)
        if name not in groups[date_iso]:
            groups[date_iso][name] = []
            name_order[date_iso].append(name)
        groups[date_iso][name].append((round_val, weight, reps, rpe_val))

    workouts = []
    for date_iso in date_order:
        entries = []
        for name in name_order[date_iso]:
            rows_for_ex = groups[date_iso][name]
            if all(r[0] is not None for r in rows_for_ex):
                rows_for_ex = sorted(rows_for_ex, key=lambda r: r[0])
            entries.append({"name": name, "sets": [[w, r, rpe] for _, w, r, rpe in rows_for_ex]})
        workouts.append({"date": date_iso, "entries": entries})
    return workouts


async def _finish_mapping(event, state: FSMContext) -> None:
    data = await state.get_data()

    async def _back_to_file(text: str) -> None:
        await state.set_state(ImportFlow.awaiting_file)
        kb = keyboards.cancel_keyboard("imp:cancel")
        if isinstance(event, CallbackQuery):
            await ui.safe_edit(event, text, reply_markup=kb)
        else:
            await event.answer(text, reply_markup=kb)

    try:
        workouts = _build_workout_groups(
            data["imp_rows"], data["imp_mapping"], first_line=2 if data.get("imp_has_header", True) else 1
        )
    except ParseError as e:
        await _back_to_file(f"Ошибка в файле: {e.message}\nИсправь файл и пришли заново.")
        return
    if not workouts:
        # Раньше пустой результат доезжал до подтверждения «0 тренировки» с
        # кнопкой «✅ Загрузить», которая рапортовала «Импортировано 0 тренировок».
        await _back_to_file("Не нашёл ни одной строки с подходами.\nПроверь файл и пришли заново.")
        return

    await state.update_data(imp_workouts=workouts, imp_resolved={})
    all_names = [entry["name"] for w in workouts for entry in w["entries"]]
    resolved: dict[str, int] = {}
    unresolved: list[str] = []
    user_id = event.from_user.id
    for name in dict.fromkeys(all_names):
        ex = await db.find_exercise_by_name(user_id, name)
        if ex:
            resolved[name] = ex["id"]
        else:
            unresolved.append(name)

    # Импорт часто приносит чужие названия (Hevy пишет по-английски: "Bench
    # Press (Barbell)"), которые не совпадут с русским каталогом ни разу — но
    # часто означают ровно то же движение. Модель переводит их в точные
    # имена каталога на лету; совпавшее заводится под СВОИМ именем (тем, что
    # было в файле), а фото и описание техники подтягиваются от шаблона —
    # человек не должен терять привычное название истории только потому, что
    # оно нашлось в каталоге под другим языком.
    if unresolved:
        # Совпадение через модель — сетевой вызов, секунды, а импорт из Hevy
        # почти всегда приносит нулевой процент точных совпадений (имена
        # английские против русского каталога) и молчит всё это время — без
        # знака, что файл вообще читается, выглядит как зависший бот.
        progress_text = "⏳ Сверяю названия упражнений с каталогом, момент..."
        if isinstance(event, CallbackQuery):
            await event.message.answer(progress_text)
        else:
            await event.answer(progress_text)
        aliases = await ai_trainer.match_exercise_names_to_catalog(user_id, unresolved)
        for name, catalog_name in aliases.items():
            ex_id = await db.create_exercise_matching_catalog_name(user_id, name, catalog_name)
            if ex_id is not None:
                resolved[name] = ex_id
        unresolved = [n for n in unresolved if n not in aliases]

    await state.update_data(imp_resolved=resolved)

    if unresolved:
        from handlers.exercise_resolve import start as start_resolve
        await start_resolve(event, state, unresolved)
    else:
        await show_confirmation(event, state)


async def on_exercises_resolved(event, state: FSMContext) -> None:
    data = await state.get_data()
    resolved = dict(data.get("imp_resolved") or {})
    resolved.update(data.get("resolve_resolved") or {})
    await state.update_data(imp_resolved=resolved)
    await show_confirmation(event, state)


async def _duplicate_dates(
    user_id: int, workouts: list[dict], resolved: Optional[dict[str, int]] = None
) -> set[str]:
    """Даты из файла, на которые у человека уже есть завершённая тренировка С
    ХОТЯ БЫ ОДНИМ ИЗ ТЕХ ЖЕ УПРАЖНЕНИЙ.

    Главная защита от повторной загрузки того же файла: раньше её не было
    вообще, и второй присланный файл молча удваивал историю (20 тренировок и
    400 подходов превращались в 40 и 800), а пересчёт ачивок закреплял это по
    удвоенному тоннажу. Разгребать приходилось руками, по одной тренировке.

    Сравнение раньше шло по одной дате целиком: любая существующая тренировка
    в этот день (даже ручная запись веса или совсем другое упражнение)
    заставляла молча пропустить весь день из файла. resolved (name → exercise
    id) сужает совпадение до реального пересечения упражнений — без него
    (например, до разрешения незнакомых названий) откатываемся к старому
    поведению по дате целиком, потому что сравнивать пока не с чем.
    """
    if resolved is None:
        have_dates = set(await db.list_finished_workout_dates(user_id))
        return {w["date"] for w in workouts if w["date"] in have_dates}
    have = await db.list_finished_workout_exercise_ids_by_date(user_id)
    dup = set()
    for w in workouts:
        existing = have.get(w["date"])
        if not existing:
            continue
        if any(resolved.get(entry["name"]) in existing for entry in w["entries"]):
            dup.add(w["date"])
    return dup


IMPORT_PAGE_SIZE = 8


async def _render_confirmation_page(event, state: FSMContext, page: int) -> None:
    """Несколько тренировок на странице, как в 📚 Истории: дата и до трёх
    упражнений короткими буллитами вместо подробного разбора каждого
    подхода — это превью будущей истории, а не отдельный, более тяжёлый
    экран. Решение «загрузить» общее для всех страниц и не листается вместе
    с ними; дубли не пропускаются молча — отмечены прямо у своей даты."""
    data = await state.get_data()
    workouts = data["imp_workouts"]
    dup = set(data.get("imp_dup") or [])
    total_pages = max(1, -(-len(workouts) // IMPORT_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * IMPORT_PAGE_SIZE
    page_workouts = workouts[start:start + IMPORT_PAGE_SIZE]

    entries = [
        (dt.date.fromisoformat(w["date"]), [e["name"] for e in w["entries"]])
        for w in page_workouts
    ]
    word = formatting.plural_ru(len(workouts), ("тренировка", "тренировки", "тренировок"))
    header = f"📥 <b>Импорт: {len(workouts)} {word}</b>"
    if total_pages > 1:
        header += f" · стр. {page + 1}/{total_pages}"
    text = formatting.build_import_confirmation_list(entries, dup, header)

    new_count = len(workouts) - len(dup)
    kb = keyboards.csv_import_page_keyboard(page, total_pages, new_count, len(dup))
    await state.update_data(imp_confirm_page=page)
    if isinstance(event, CallbackQuery):
        await ui.safe_edit(event, text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


async def show_confirmation(event, state: FSMContext) -> None:
    data = await state.get_data()
    workouts = data["imp_workouts"]
    dup = await _duplicate_dates(event.from_user.id, workouts, data.get("imp_resolved"))
    await state.update_data(imp_dup=sorted(dup))
    await state.set_state(ImportFlow.confirming)
    await _render_confirmation_page(event, state, 0)


@router.callback_query(StateFilter(ImportFlow.confirming), F.data.startswith("imp:page:"))
async def import_confirm_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[2])
    await _render_confirmation_page(callback, state, page)
    await callback.answer()


@router.callback_query(StateFilter(ImportFlow.confirming), F.data.in_({"imp:save", "imp:saveall"}))
async def import_save(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    workouts = data["imp_workouts"]
    resolved = data["imp_resolved"]
    user_id = callback.from_user.id
    # imp:saveall — человек посмотрел на список дублей и всё равно хочет их
    # (бывает: две тренировки в один день). imp:save грузит только новые даты.
    force = callback.data == "imp:saveall"

    # Считаем дубли здесь, а не только на экране подтверждения: между показом и
    # нажатием могла появиться тренировка, да и «Загрузить» легко нажать дважды.
    skip = set() if force else await _duplicate_dates(user_id, workouts, resolved)
    to_import = [w for w in workouts if w["date"] not in skip]

    if not to_import:
        # Импорт закончился ничем — но переписка с AI-тренером и черновик его
        # программы к нему отношения не имеют, сохраняем их.
        await clear_state_keep_ai(state)
        from handlers.settings import show_settings
        await show_settings(
            callback, state, alert="Эти тренировки уже есть в истории — ничего не добавил"
        )
        return

    # Запись подходов по одному плюс пересчёт ачивок (ниже) — для файла на
    # десятки тренировок заметно не мгновенно, а кнопка до этого места ничем
    # не показывала, что вообще что-то происходит.
    await ui.safe_edit(callback, "⏳ Загружаю тренировки, момент...", reply_markup=None)

    # Дата тренировки в файле — календарная, местная для пользователя, а
    # started_at хранится в UTC и местный день восстанавливается прибавлением
    # tz_offset (db._local_day). «Безопасный полдень» без поправки на офсет
    # ловит верхнюю границу пикера часовых поясов (UTC+12,
    # keyboards.py:1183): 12:00 + 12 часов перекатывается на полночь
    # следующих суток. Сдвигаем полдень назад на величину офсета — тогда
    # 12:00 + tz_offset - tz_offset снова даёт исходную дату при любом
    # значении из диапазона пикера (-1…+12).
    tz_offset = await db.user_tz_offset(user_id)
    for w in to_import:
        local_noon = dt.datetime.fromisoformat(f"{w['date']}T12:00:00")
        started_at = (local_noon - dt.timedelta(hours=tz_offset)).isoformat()
        workout_id = await db.create_finished_workout(user_id, started_at, started_at, source="import")
        for entry in w["entries"]:
            ex_id = resolved[entry["name"]]
            block_id = await db.create_block(workout_id, "single")
            await db.add_block_exercise(block_id, ex_id, 0)
            await db.touch_exercise_last_used(ex_id)
            for idx, (weight, reps, rpe) in enumerate(entry["sets"], start=1):
                await db.add_set(block_id, ex_id, idx, 0, weight, reps, rpe)

    # A year of imported history can complete streaks, weight clubs and tonnage
    # badges all at once. Without this the grid stays empty until the next live
    # workout happens to trigger an evaluation — the same resync the history and
    # edit screens already run after changing the past.
    await achievement_sync.resync(user_id)

    # Импорт завершён — а переписка с AI-тренером и черновик его программы
    # переживают такие потоки, их не трогаем.
    await clear_state_keep_ai(state)
    # show_settings redraws this very message, so a "✅ Импортировано N" written
    # here would live for milliseconds — it goes in the alert instead.
    n = len(to_import)
    word = formatting.plural_ru(n, ("тренировка", "тренировки", "тренировок"))
    alert = f"✅ Импортировано {n} {word}"
    if skip:
        # Пропуск озвучиваем в том же алерте: иначе «импортировано 5» вместо
        # ожидаемых 25 выглядит как потеря данных.
        alert += f", пропущено {len(skip)} (уже были в истории)"
    from handlers.settings import show_settings
    await show_settings(callback, state, alert=alert)


@router.callback_query(F.data == "imp:cancel")
async def import_cancel(callback: CallbackQuery, state: FSMContext):
    # Отмена импорта не отменяет переписку с AI-тренером и черновик его
    # программы — сохраняем их.
    await clear_state_keep_ai(state)
    from handlers.settings import show_settings
    await show_settings(callback, state)
    await callback.answer("Отменено")
