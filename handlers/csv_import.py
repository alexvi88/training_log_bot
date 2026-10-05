"""§A3 — CSV import (round-trip with the §9 export): дата, упражнение, вес, повторы[, подход]."""

import asyncio
import collections
import copy
import csv
import datetime as dt
import functools
import io
import logging
import math
import re
from contextlib import suppress
from html import escape
from typing import Iterable, Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import achievement_sync
import achievements
import ai_limits
import ai_trainer
import config
import db
import formatting
import i18n
import keyboards
import search_terms
import seed_data
import text_import
import timeutil
import ui
from fsm import ImportFlow
from parser import MAX_REPS, MAX_WEIGHT, ParseError, parse_ru_date
from state_scaffold import clear_state_keep_workout

router = Router(name="csv_import")

logger = logging.getLogger(__name__)

# Тот же приём, что у handlers.workout._background_tasks: голая ссылка цикла
# на create_task — слабая, и без своей задача может собраться сборщиком мусора
# на середине ожидания ответа модели.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _attach_import_overview(bot, chat_id: int, user_id: int) -> None:
    """«Вижу два года жима, присед бросил в марте» — одно сообщение фоном,
    следом за экраном «Импортировано N», а не вместо него: сама загрузка уже
    закончилась и экран ушёл в главное меню, ждать модель на этом месте было
    бы чистой задержкой без выгоды (тот же приём, что у
    workout._attach_ai_comment). Ниже порога тренировок в истории эта же
    функция вернёт детерминированную реплику без обращения к модели —
    см. ai_trainer.import_history_overview.
    """
    try:
        overview = await ai_trainer.import_history_overview(user_id)
    except Exception:
        logger.exception("AI import overview failed for user %s", user_id)
        return
    if not overview:
        return
    with suppress(TelegramBadRequest):
        # Кнопка под разбором — иначе это монолог тренера, а не начало
        # разговора: человек только что перенёс историю, и логичный
        # следующий шаг — спросить про неё же, а не печатать вопрос заново
        # (см. handlers.ai_trainer.ai_import_cta и keyboards.
        # import_overview_cta_keyboard).
        await bot.send_message(
            chat_id, formatting.ai_markdown_to_html(overview), parse_mode="HTML",
            reply_markup=keyboards.import_overview_cta_keyboard(),
        )

# Кто прямо сейчас пишет импортированные тренировки в базу — двойной тап по
# «Загрузить» (или медленный ответ Telegram на первый тап) иначе запускает
# import_save дважды почти одновременно: оба видят одно и то же ImportFlow.
# confirming, оба независимо проверяют дубли до того, как первый успел
# что-то закоммитить, и оба записывают файл целиком. Тот же приём, что
# ai_trainer._try_claim_busy — atomically check-and-reserve без await между
# проверкой и добавлением, чтобы asyncio не успел переключиться в зазоре.
_saving: set[int] = set()


def _try_claim_saving(user_id: int) -> bool:
    if user_id in _saving:
        return False
    _saving.add(user_id)
    return True

REQUIRED_FIELDS = ["date", "exercise", "weight", "reps"]
# Ключи каталога, не готовые строки: словарь модульного уровня со значением
# i18n.t() застыл бы на языке процесса на момент импорта модуля (обычно ru,
# задолго до первого апдейта) — вызывать i18n.t() нужно на месте, в момент
# показа/ошибки, см. _field_label ниже.
_FIELD_LABEL_KEYS = {
    "date": "import.field_date",
    "exercise": "import.field_exercise",
    "weight": "import.field_weight",
    "reps": "import.field_reps",
    "round": "import.field_round",
}


def _field_label(field: str) -> str:
    return i18n.t(_FIELD_LABEL_KEYS[field])


# Кандидаты в разделители: свой экспорт пишет запятую, но «Сохранить как CSV»
# в русском Excel даёт «;» (и запятую внутри дробей), а Google Sheets — табы.
DELIMITERS = ",;\t|"
# start_time/exercise_title/weight_kg/set_index/set_type — колонки родного
# экспорта Hevy: самого частого источника миграции. Без них файл оттуда не
# автоопределялся ни по одному из четырёх обязательных полей и упирался в
# ручной маппинг с нуля.
# exercise name/set order — второй такой источник, Strong: у него дата и вес
# называются как у нас ("Date", "Weight"), а упражнение и номер подхода — нет,
# и человек всё равно шёл в ручной маппинг ради двух полей из четырёх.
SYNONYMS = {
    "date": {"дата", "date", "started_at", "start_time"},
    "exercise": {"упражнение", "exercise", "exercise_title", "exercise name"},
    # weight_lbs/weight_lb — тот же Hevy у атлета с фунтами в настройках: без
    # синонима файл упирался в ручной маппинг веса, а единицу колонки после
    # выбора всё равно определяет _weight_factor по заголовку.
    "weight": {"вес", "weight", "weight_kg", "weight_lbs", "weight_lb"},
    "reps": {"повторы", "reps"},
    "round": {"подход", "раунд", "round", "set", "round_index", "set_index", "set order"},
    "rpe": {"rpe", "рпе"},
    # Тип подхода у Hevy (normal/warmup/failure/dropset): разминку пропускаем,
    # см. _is_warmup. Не обязательная колонка — в ручной маппинг не попадает.
    "set_type": {"set_type", "set type"},
    # Дальше — необязательные колонки Hevy/Strong, тоже без ручного маппинга.
    # Название тренировки: по нему (вместе со временем начала) строки
    # делятся на сессии — две тренировки за день остаются двумя.
    "title": {"title", "workout name", "workout_name"},
    "end": {"end_time"},
    # Длительность сессии у Strong («1h 5m»): конец тренировки = начало + она.
    "workout_duration": {"duration"},
    # Кардио и упражнения на время: строки с дистанцией или секундами без
    # веса и повторов пропускаем, но называем в отчёте (а не теряем молча).
    "distance": {"distance_km", "distance_miles", "distance_m", "distance"},
    "seconds": {"duration_seconds", "seconds"},
    # Заметки: к упражнению (Hevy exercise_notes, Strong Notes) и ко всей
    # тренировке (Hevy description, Strong Workout Notes).
    "exercise_note": {"exercise_notes", "notes"},
    "workout_note": {"description", "workout notes"},
    "superset": {"superset_id"},
}
# Единицы в скобках у заголовка — «Weight (kg)», «Weight (lbs)», «Distance (m)»:
# Strong подписывает ими колонки по настройкам аккаунта, и без этой срезки один
# и тот же экспорт автоопределялся у одного человека и не автоопределялся у
# другого — только потому, что тот считает вес в фунтах.
_HEADER_UNITS_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _normalize_header(text: str) -> str:
    return _HEADER_UNITS_RE.sub("", text.strip().lower()).strip()


@router.callback_query(F.data.in_({"settings:import", "settings:import:menu"}))
async def import_start(callback: CallbackQuery, state: FSMContext):
    # Запомнить, откуда зашли (пустое главное меню или ⚙️ Настройки) — «Отмена»
    # в конце флоу (import_cancel) возвращает туда же, а не всегда в настройки.
    origin = "menu" if callback.data.endswith(":menu") else "settings"
    await state.set_state(ImportFlow.awaiting_file)
    await state.update_data(import_origin=origin)
    # Прошлые импорты за 30 дней можно отменить прямо отсюда — тот же экран,
    # куда человек придёт, если файл лёг не так.
    has_undo = any(b["can_undo"] for b in await undoable_batches(callback.from_user.id))
    await ui.safe_edit(
        callback,
        i18n.t("import.intro"),
        reply_markup=keyboards.import_intro_keyboard(has_undo),
    )
    await callback.answer()


def _auto_detect(headers: list[str]) -> dict[str, int]:
    lowered = [_normalize_header(h) for h in headers]
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
    Проверки на будущее и на слишком старую дату здесь нет нарочно: строка с
    такой датой — всё равно данные, и ошибку про неё скажет разбор ниже, со
    своим номером строки, а не «не понял, где заголовки».
    """
    for cell in row:
        for month_first in (False, True):
            try:
                _parse_row_date_raw(cell, month_first=month_first)
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
    return [i18n.t("import.column_n", n=i + 1) for i in range(width)], rows, False


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
    text = i18n.t(
        "import.mapping_step", step=step, total=total, label=_field_label(field),
        headers=", ".join(headers),
    )
    # Без строки заголовков «Колонка 2» ни о чём не говорит — показываем первую
    # строку данных, по ней видно, где что.
    if not data.get("imp_has_header", True):
        sample = data.get("imp_sample_row") or []
        text = (
            i18n.t("import.no_header_notice")
            + text
            + (i18n.t("import.first_row_sample", sample=" | ".join(sample)) if sample else "")
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
        await message.reply(i18n.t("import.wrong_extension"))
        return
    buf = await message.bot.download(document)
    raw = buf.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")

    headers, data_rows, has_header = _read_table(text)
    if not headers:
        await message.reply(i18n.t("import.file_empty"))
        return
    if not data_rows:
        await message.reply(i18n.t("import.no_data_rows"))
        return
    if len(headers) < len(REQUIRED_FIELDS):
        # Обычно это не «файл из одной колонки», а неугаданный разделитель —
        # лучше сказать это сразу, чем после четырёх шагов маппинга.
        await message.reply(i18n.t("import.too_few_columns", n=len(headers)))
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


@router.message(StateFilter(ImportFlow.awaiting_file), ~F.text)
async def import_file_missing(message: Message, state: FSMContext):
    await message.reply(i18n.t("import.file_missing"))


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
        await callback.answer(i18n.t("import.first_question_no_back"))
        return
    field = answered.pop()
    mapping = {k: v for k, v in dict(data["imp_mapping"]).items() if k != field}
    pending = [field] + list(data.get("imp_pending_fields") or [])
    await state.update_data(
        imp_mapping=mapping, imp_pending_fields=pending, imp_answered_fields=answered
    )
    await _ask_next_mapping(callback, state)
    await callback.answer()


# Hevy и Strong пишут дату словом на языке телефона: «7 Aug 2026, 08:27»,
# «4 мая 2026, 19:00», «8 сент. 2026», «2 июн. 2026», «Sep 8, 2026». Своего
# формата в parse_ru_date/ISO для этого нет. Список руками, а не через
# locale/strptime («%b»): locale контейнера решает, на каком языке страптайм
# ждёт месяц, и «Aug» на сервере с русской локалью незаметно перестал бы
# разбираться. Месяц узнаём по основе слова (_month_number), поэтому любое
# сокращение и любой падеж с точкой и без — «сен», «сент.», «сентября»,
# «мая», «май», «февр.», «нояб.» — читаются одинаково.
_MONTH_STEMS_EN = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_MONTH_STEMS_RU = (
    "январ", "феврал", "март", "апрел", "ма", "июн",
    "июл", "август", "сентябр", "октябр", "ноябр", "декабр",
)


def _month_number(word: str) -> Optional[int]:
    """Номер месяца по слову из даты или None. Два способа совпасть: слово —
    начало полного названия («sept» → september, «нояб» → ноябрь), или
    основа — начало слова с окончанием падежа («сентября», «мая», «июня»).
    Короче трёх букв — не месяц."""
    word = word.strip().lower().rstrip(".")
    if len(word) < 3:
        return None
    for stems in (_MONTH_STEMS_EN, _MONTH_STEMS_RU):
        starts = [i for i, stem in enumerate(stems) if len(stem) >= 3 and stem.startswith(word)]
        if len(starts) == 1:
            return starts[0] + 1
    best: Optional[int] = None
    best_len = 0
    for stems in (_MONTH_STEMS_EN, _MONTH_STEMS_RU):
        for i, stem in enumerate(stems):
            if word.startswith(stem) and len(word) - len(stem) <= 3 and len(stem) > best_len:
                best, best_len = i + 1, len(stem)
    return best


# Время после даты: «18:30», «18:30:00» и 12-часовое «6:30 PM» (американский
# экспорт пишет «3/15/2024 6:30 PM»). Тренировка ложится на свой календарный
# день, а время из файла становится её настоящим началом (см. _row_time).
_TIME_PART = r"\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp]\.?\s?[Mm]\.?)?"
# [^\W\d_] — любая буква (латиница и кириллица), без самой кириллицы в
# литерале; месяц всё равно сверяется с _month_number. После года бывает «г.».
_WORD_DATE_RE = re.compile(
    r"^(?P<d>\d{1,2})\s+(?P<mon>[^\W\d_]{3,})\.?,?\s+(?P<y>\d{4})(?:\s*[^\W\d_]{1,2}\.?)?"
    r"(?:,?\s*" + _TIME_PART + r")?$"
)
_WORD_DATE_MONTH_FIRST_RE = re.compile(
    r"^(?P<mon>[^\W\d_]{3,})\.?\s+(?P<d>\d{1,2}),?\s+(?P<y>\d{4})(?:,?\s*" + _TIME_PART + r")?$"
)
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*([AaPp])\.?\s?[Mm]\.?)?")
_TIME_SUFFIX_RE = re.compile(r"[,\s]+" + _TIME_PART + r"$")
# Числовая дата «день-разделитель-месяц-разделитель-год» — та же форма, что
# у parser.parse_ru_date, только отдельно ради косой черты: её одну читаем
# как месяц/день, если колонка это доказывает (см. _slash_date_order).
_NUMERIC_DATE_RE = re.compile(r"^\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}$")
_SLASH_DATE_RE = re.compile(r"^(?P<a>\d{1,2})/(?P<b>\d{1,2})/(?P<y>\d{2,4})$")
# Нижняя граница даты из файла. Дневник заведён сильно позже, и дата до 2000
# года — почти всегда пустая ячейка, выгруженная как эпоха Unix
# («1970-01-01»), или перепутанный год, а не настоящая тренировка: записанная
# молча, она навсегда растягивает историю и график прогресса на десятилетия.
_MIN_IMPORT_DATE = dt.date(2000, 1, 1)


def _strip_time_suffix(text: str) -> str:
    """«05.03.2024 18:30» → «05.03.2024», «3/15/2024 6:30 PM» → «3/15/2024».
    Срезаем только когда перед временем стоит именно числовая дата — ISO и
    Hevy со своим временем разбираются своими ветками ниже."""
    match = _TIME_SUFFIX_RE.search(text)
    if match and _NUMERIC_DATE_RE.match(text[:match.start()]):
        return text[:match.start()]
    return text


def _slash_date_order(values: list[str]) -> tuple[Optional[str], bool]:
    """(как читать «a/b/гггг»: "mdy", "dmy" или None — в колонке нет дат через
    косую черту; неоднозначна ли колонка).

    Решаем по всей колонке, а не по ячейке: «03/04/2024» в одиночку — это и
    3 апреля, и 4 марта, и только соседние строки говорят, какой формат у
    файла. Вторая часть больше 12 хоть где-то — файл американский (м/д);
    первая больше 12 — европейский (д/м), как раньше. Если все значения
    неоднозначны — остаёмся на д/м (прежнее поведение, формат бота), а
    превью честно показывает получившийся диапазон дат и флаг
    неоднозначности. Точки и дефисы не трогаем: у них формат всегда д.м.
    """
    day_first_seen = month_first_seen = has_slash = False
    for value in values:
        match = _SLASH_DATE_RE.match(_strip_time_suffix(value.strip()))
        if not match:
            continue
        has_slash = True
        if int(match["a"]) > 12:
            day_first_seen = True
        if int(match["b"]) > 12:
            month_first_seen = True
    if not has_slash:
        return None, False
    order = "mdy" if month_first_seen and not day_first_seen else "dmy"
    return order, not month_first_seen and not day_first_seen


def _parse_row_date_raw(text: str, month_first: bool = False) -> dt.date:
    """Календарная дата из ячейки без проверки границ (будущее, до 2000) —
    их ставит _parse_row_date. Отдельно ради _looks_like_data: строке с
    датой в будущем всё равно положено остаться строкой данных."""
    text = _strip_time_suffix(text.strip())
    slash = _SLASH_DATE_RE.match(text)
    if month_first and slash:
        year = int(slash["y"])
        if year < 100:
            year += 2000
        try:
            return dt.date(year, int(slash["a"]), int(slash["b"]))
        except ValueError:
            raise ParseError(i18n.t("import.err_date", text=text)) from None
    try:
        # dt.date.max вместо «сегодня»: будущее проверяет _parse_row_date,
        # одинаково для всех форматов, а не только для дд.мм.
        return parse_ru_date(text, dt.date.max)
    except ParseError:
        pass
    for regex in (_WORD_DATE_RE, _WORD_DATE_MONTH_FIRST_RE):
        match = regex.match(text)
        month = _month_number(match["mon"]) if match else None
        if month is not None:
            try:
                return dt.date(int(match["y"]), month, int(match["d"]))
            except ValueError:
                raise ParseError(i18n.t("import.err_date", text=text)) from None
    try:
        if "t" in text.lower():
            return dt.datetime.fromisoformat(text).date()
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        raise ParseError(i18n.t("import.err_date", text=text)) from None


def _parse_row_date(
    text: str, today: Optional[dt.date] = None, month_first: bool = False
) -> dt.date:
    """Дата строки импорта в любом из форматов — и с одними и теми же
    границами для всех. Раньше «ещё в будущем» ловил только дд.мм
    (parser.parse_ru_date), а ISO и Hevy пропускали и 2099-01-01, и
    1970-01-01: такая тренировка становилась вечной «последней» или
    растягивала историю на полвека. `today` — местный день пользователя
    (timeutil.user_today), одинаково в боте и в REST."""
    date = _parse_row_date_raw(text, month_first=month_first)
    if date > (today or dt.date.today()):
        raise ParseError(i18n.t("input.date_in_future"))
    if date < _MIN_IMPORT_DATE:
        raise ParseError(i18n.t("import.err_date_too_old", text=text.strip()))
    return date


def _unescape_formula_cell(text: str) -> str:
    """Обратное к csv_export.escape_formula_cell: «'=cmd» → «=cmd». Апостроф
    снимается только перед знаком формулы — «'Тяга» остаётся как есть."""
    if len(text) > 1 and text[0] == "'" and text[1] in "=+-@":
        return text[1:]
    return text


# Запятая-разделитель тысяч: ровно три цифры в каждой группе после первой
# ("1,200", "1,234,000.5") — так группирует английский/американский экспорт.
# Десятичная запятая ("100,5") этому не соответствует почти никогда: дробная
# часть веса/повторов редко бывает ровно трёхзначной. Без этого различия
# "1,200" (=1200) молча превращался в 1.2 — тихая порча веса без единой ошибки.
_THOUSANDS_COMMA_RE = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")


def _parse_number(text: str) -> float:
    """Дробное из ячейки: «100.5», «100,5» и «1 000» — одно и то же число.

    Запятая как десятичный разделитель — норма для русской локали Excel, а
    пробел там же приезжает разрядным разделителем. «1,200» из
    английского/американского экспорта — другой случай: там запятая между
    разрядами тысяч, а не дробная часть (см. _THOUSANDS_COMMA_RE).
    """
    text = text.strip()
    text = text.replace(",", "") if _THOUSANDS_COMMA_RE.match(text) else text.replace(",", ".")
    value = float(text.replace(" ", "").replace("\xa0", ""))
    # «nan» и «inf» float() принимает как числа: дальше они проходят любые
    # сравнения и ломают int()/превью — для всех ячеек это просто «не число».
    if not math.isfinite(value):
        raise ValueError(f"not a finite number: {text!r}")
    return value


# Фунты в весе — не косметика заголовка, а другая величина: без пересчёта
# «225 lbs» легли бы в историю как 225 кг, стали бы вечным рекордом упражнения
# и разом открыли бы весовые клубы, которые уже не отбираются. Строка «Weight
# (lbs)» бывает только у Strong: он подписывает колонку единицей аккаунта.
_LBS_IN_KG = 0.45359237
# «Weight (lbs)» у Strong, «weight_lbs» у Hevy, «Weight lb» руками: единица —
# отдельное слово, в скобках или через подчёркивание. Буквы вокруг не
# допускаются, чтобы «bulbs» или «climbs» не превратились в фунты.
_LBS_HEADER_RE = re.compile(r"(?:^|[^a-z])(?:lbs?|pounds?)(?:[^a-z]|$)", re.IGNORECASE)
# Явные килограммы: «weight_kg» у Hevy, «Weight (kg)» у Strong, «Вес (кг)» из
# русского Excel. Отличать их от просто «Weight» нужно атлету в фунтах: у него
# безымянная колонка — это его же единица (так пишет наш экспорт), а явные
# килограммы надо перевести в фунты. Границы — любой не-буквенно-цифровой знак
# или «_», чтобы «kgs»/«кг» внутри слова не считались единицей.
_KG_UNIT_RU = "кг"
_KG_HEADER_RE = re.compile(
    r"(?:^|[\W_])(?:kgs?|kilos?|kilograms?|" + _KG_UNIT_RU + r")(?:[\W_]|$)", re.IGNORECASE,
)


def _file_weight_unit(headers: list[str], mapping: dict[str, int]) -> Optional[str]:
    """Единица колонки веса по её заголовку: "lb", "kg" или None — не указана."""
    idx = mapping.get("weight")
    if idx is None or idx >= len(headers):
        return None
    header = headers[idx].strip()
    if _LBS_HEADER_RE.search(header):
        return "lb"
    if _KG_HEADER_RE.search(header):
        return "kg"
    return None


def _file_source(headers: list[str], has_header: bool) -> str:
    """Откуда файл: "strong", "hevy" или "other" — по фирменным заголовкам."""
    if not has_header:
        return "other"
    names = {_normalize_header(h) for h in headers}
    if "exercise name" in names and "set order" in names:
        return "strong"
    if "exercise_title" in names or ("start_time" in names and "set_type" in names):
        return "hevy"
    return "other"


def _weight_factor(
    headers: list[str], mapping: dict[str, int], account_unit: str,
    file_unit: Optional[str] = None,
) -> float:
    """Множитель «единица файла → единица аккаунта».

    Вес подхода хранится в единице аккаунта (см. db.scale_user_set_weights,
    которая пересчитывает историю при смене кг↔lb), поэтому целевая единица —
    `users.unit`, а не всегда килограммы: раньше атлет в фунтах получал из
    «weight_lbs = 225» подход 102.1 (показ — «102 lb»), а явные килограммы
    ложились у него как фунты без пересчёта. Колонка без единицы считается
    записанной в единице аккаунта — так пишет наш собственный экспорт.
    """
    # `file_unit` — явный выбор человека («кг | lb» в предпросмотре приложения):
    # он сильнее заголовка, потому что автоопределение по заголовку может
    # ошибиться (у Strong единица — настройка аккаунта, а не файла).
    if file_unit is None:
        file_unit = _file_weight_unit(headers, mapping)
    if file_unit is None or file_unit == account_unit:
        return 1.0
    if file_unit == "lb":
        return _LBS_IN_KG
    return config.LB_PER_KG


def _parse_count(text: str, label: str) -> int:
    """Целое из ячейки, терпимое к «8.0».

    Таблицы хранят числа float'ами и охотно пишут «8.0» в повторах — на этом
    раньше падал весь импорт целиком («не разобрал вес/повторы»), хотя восемь
    повторов тут читаются однозначно. А вот «8.5» — уже настоящая ошибка.
    """
    try:
        value = _parse_number(text)
    except ValueError:
        raise ParseError(i18n.t("import.err_count", label=label, text=text)) from None
    if value != int(value):
        raise ParseError(i18n.t("import.err_not_integer", label=label, text=text))
    return int(value)


def _number_or_zero(text: str) -> float:
    try:
        return _parse_number(text) if text.strip() else 0.0
    except ValueError:
        return 0.0


def _row_skip_reason(row: list[str], mapping: dict[str, int]) -> Optional[str]:
    """Почему строку не превращать в подход — или None, если она подход.

    Strong и Hevy держат в одной таблице силовые, кардио и упражнения на
    время: у пробежки вес и повторы пусты или нули, а работа лежит в
    дистанции/секундах. Раньше такая строка валила весь файл, потом молча
    пропадала; теперь она пропускается с причиной, которую видно в отчёте
    (SKIP_REASONS): разминка, кардио (есть дистанция), упражнение на время
    (есть только секунды), пустая строка без нагрузки («unparsed»).
    Нечисловая ячейка веса или повторов — не наше дело: об этом скажет
    разбор ниже, своей строкой с номером (bad_line)."""
    if _is_warmup(row, mapping):
        return "warmup"
    weight_text = _cell(row, mapping, "weight")
    reps_text = _cell(row, mapping, "reps")
    for text in (weight_text, reps_text):
        if text:
            try:
                _parse_number(text)
            except ValueError:
                return None
    weight = _number_or_zero(weight_text)
    reps = _number_or_zero(reps_text)
    if reps > 0:
        return None
    if _number_or_zero(_cell(row, mapping, "distance")) > 0:
        return "cardio"
    if _number_or_zero(_cell(row, mapping, "seconds")) > 0:
        return "duration_only"
    if weight == 0:
        return "unparsed"
    return None


# Номер подхода у Strong бывает буквой: W — разминка, D — дроп-сет, F — отказ.
# Раньше любая буква валила весь файл на «не понял номер подхода».
_WARMUP_SET_MARKS = {"W"}
_UNNUMBERED_SET_MARKS = {"D", "F"}
# set_type у Hevy.
_WARMUP_SET_TYPES = {"warmup", "warm-up", "warm up"}

# Причины пропуска строк — одни и те же коды в отчёте бота и в поле
# `skipped` у REST (см. api_v1_import). bad_line — строка, которую не
# разобрать (номер строки и что не так); no_weight — подход без веса у
# упражнения не с собственным весом (решается после сопоставления имён,
# см. drop_unweighted_sets); unparsed у заметок — то, что модель не
# поняла.
SKIP_REASONS = ("warmup", "cardio", "duration_only", "no_weight", "bad_line", "unparsed")
_MAX_SKIP_EXAMPLES = 3


def add_skip(stats: dict, reason: str, example: Optional[str] = None, count: int = 1) -> None:
    """Учесть пропуск в stats["skipped"]: reason → {"count", "examples"}.
    Примеров — не больше трёх и без повторов."""
    bucket = stats.setdefault("skipped", {}).setdefault(reason, {"count": 0, "examples": []})
    bucket["count"] += count
    if example and example not in bucket["examples"] and len(bucket["examples"]) < _MAX_SKIP_EXAMPLES:
        bucket["examples"].append(example)


def skipped_report(stats: dict) -> list[dict]:
    """Пропуски в порядке SKIP_REASONS: [{"reason", "count", "label",
    "examples"}] — label на языке текущего запроса (i18n.t)."""
    out = []
    for reason in SKIP_REASONS:
        bucket = (stats.get("skipped") or {}).get(reason)
        if not bucket or not bucket["count"]:
            continue
        out.append({
            "reason": reason,
            "count": bucket["count"],
            "label": i18n.t(f"import.skipped.{reason}", n=bucket["count"]),
            "examples": list(bucket["examples"]),
        })
    return out


def _cell(row: list[str], mapping: dict[str, int], field: str) -> str:
    idx = mapping.get(field)
    if idx is None or idx >= len(row):
        return ""
    return row[idx].strip()


def _is_warmup(row: list[str], mapping: dict[str, int]) -> bool:
    """Разминочный подход (Strong «W» в Set Order, Hevy set_type=warmup).

    Пропускаем, а не пишем: разминка с пустым грифом в истории портила бы
    средний вес, тоннаж и «последний раз» упражнения, а у нас самих разминка
    в дневник не записывается. И это не ошибка файла — пропуск считается и
    называется в отчёте (SKIP_REASONS), а не валит импорт.
    """
    if _cell(row, mapping, "round").upper() in _WARMUP_SET_MARKS:
        return True
    return _cell(row, mapping, "set_type").lower() in _WARMUP_SET_TYPES


def _date_column_values(rows: list[list[str]], mapping: dict[str, int]) -> list[str]:
    idx = mapping.get("date")
    if idx is None:
        return []
    return [r[idx] for r in rows if idx < len(r)]


def _row_time(text: str) -> Optional[dt.time]:
    """Время начала из ячейки даты, если оно там есть («7 Aug 2026, 08:27»,
    «2024-01-15 18:30:00», «3/15/2024 6:30 PM»), иначе None — у своего
    экспорта и заметок времени нет, такая тренировка встаёт задним числом
    (timeutil.backdated_moment), как и раньше."""
    match = _TIME_RE.search(text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    second = int(match.group(3) or 0)
    ampm = (match.group(4) or "").lower()
    if ampm == "p" and hour < 12:
        hour += 12
    elif ampm == "a" and hour == 12:
        hour = 0
    try:
        return dt.time(hour, minute, second)
    except ValueError:
        return None


_DURATION_PART_RE = re.compile(r"(\d+)\s*([hms])", re.IGNORECASE)


def _parse_duration(text: str) -> Optional[dt.timedelta]:
    """Длительность сессии Strong: «1h 5m», «45m», «3600» (секунды)."""
    text = text.strip().lower()
    if not text:
        return None
    if text.isdigit():
        return dt.timedelta(seconds=int(text))
    total = 0
    for value, unit in _DURATION_PART_RE.findall(text):
        total += int(value) * {"h": 3600, "m": 60, "s": 1}[unit.lower()]
    return dt.timedelta(seconds=total) if total else None


def _session_bounds(
    row: list[str], mapping: dict[str, int], date_val: dt.date, month_first: bool
) -> tuple[Optional[dt.datetime], Optional[dt.datetime]]:
    """(начало, конец) сессии по местным часам атлета — из времени в ячейке
    даты, колонки end_time (Hevy) или длительности (Strong). Нет времени —
    (None, None)."""
    start_time = _row_time(_cell(row, mapping, "date"))
    if start_time is None:
        return None, None
    started = dt.datetime.combine(date_val, start_time)
    finished = None
    end_text = _cell(row, mapping, "end")
    if end_text:
        end_time = _row_time(end_text)
        try:
            end_date = _parse_row_date_raw(end_text, month_first=month_first)
        except ParseError:
            end_date = None
        if end_time is not None and end_date is not None:
            finished = dt.datetime.combine(end_date, end_time)
    if finished is None:
        duration = _parse_duration(_cell(row, mapping, "workout_duration"))
        if duration is not None:
            finished = started + duration
    if finished is None or finished < started:
        finished = started
    return started, finished


def _skip_example(row: list[str], mapping: dict[str, int]) -> str:
    name = _cell(row, mapping, "exercise")
    weight, reps = _cell(row, mapping, "weight"), _cell(row, mapping, "reps")
    if weight and reps:
        return f"{name} {weight}×{reps}"
    return name


def _build_workout_groups(
    rows: list[list[str]], mapping: dict[str, int], first_line: int = 2,
    today: Optional[dt.date] = None, weight_factor: float = 1.0,
    stats: Optional[dict] = None, account_unit: str = "kg",
) -> list[dict]:
    """Строки файла → тренировки-сессии.

    Сессия — это не календарный день, а одна тренировка в чужом приложении:
    строки с тем же началом (дата и время в ячейке даты) и тем же названием
    (колонка title/Workout Name). Раньше всё за день склеивалось в одну
    тренировку: четыре сессии Hevy 7 августа становились одной на 13:30, а
    название и время терялись. У своего экспорта и заметок времени нет —
    там сессия по-прежнему равна дню.

    Тренировка: {"date", "title", "started_at"/"finished_at" (местные часы
    атлета, ISO, или None), "note", "entries": [{"name", "sets": [[вес|None,
    повторы, rpe]], "superset", "note"}]}. Вес None — ячейка веса пуста:
    у упражнения с собственным весом это подход с весом тела, у остальных —
    пропуск no_weight (решается после сопоставления, drop_unweighted_sets).

    Вес на выходе — в единице аккаунта (`weight_factor` из _weight_factor),
    а потолок MAX_WEIGHT сверяется в килограммовом эквиваленте: он задан в
    кг, и у атлета в фунтах честные 1600 lb (≈726 кг) иначе валили бы файл.

    Одна битая строка больше не валит весь файл: она пропускается с номером
    и причиной (bad_line в stats["skipped"]). Файл отклоняется, только если
    из него не разобралось ни одного подхода — тогда летит первая ошибка.

    `stats`, если передан, дополняется тем, что разбор решил сам и о чём
    стоит сказать человеку до записи: пропуски по причинам (stats["skipped"],
    см. skipped_report) и прежние счётчики rows_skipped_warmup/
    rows_skipped_no_load/rows_skipped, как прочитан формат «a/b/гггг»
    (date_order: "dmy"/"mdy"/None, date_order_ambiguous).
    """
    stats = stats if stats is not None else {}
    sessions: dict[tuple, dict] = {}
    session_order: list[tuple] = []
    # Одно упражнение, записанное в файле по-разному («Жим лёжа», «жим лежа»,
    # «Жим лёжа »), — одно упражнение, а не три: тем же сравнением, что у
    # db.find_exercise_by_name (регистр, ё=е, пробелы по краям). Показываем
    # то написание, что встретилось первым.
    name_by_fold: dict[str, str] = {}
    warmups = 0
    no_load = 0
    first_error: Optional[ParseError] = None
    slash_order, ambiguous = _slash_date_order(_date_column_values(rows, mapping))
    month_first = slash_order == "mdy"

    for line_no, row in enumerate(rows, start=first_line):
        if not row or all(not c.strip() for c in row):
            continue
        reason = _row_skip_reason(row, mapping)
        if reason is not None:
            if reason == "warmup":
                warmups += 1
            else:
                no_load += 1
            add_skip(stats, reason, _skip_example(row, mapping))
            continue
        try:
            date_val, name, weight, reps, round_val, rpe_val = _parse_set_row(
                row, mapping, line_no, today, month_first, weight_factor, account_unit,
            )
        except ParseError as e:
            first_error = first_error or e
            add_skip(stats, "bad_line", e.message)
            continue
        name = name_by_fold.setdefault(db._fold_exercise_name(name), name)

        title = _cell(row, mapping, "title") or None
        key = (date_val.isoformat(), _cell(row, mapping, "date"), title)
        session = sessions.get(key)
        if session is None:
            started, finished = _session_bounds(row, mapping, date_val, month_first)
            session = {
                "date": date_val.isoformat(),
                "title": title,
                "started_at": started.isoformat() if started else None,
                "finished_at": finished.isoformat() if finished else None,
                "note": _cell(row, mapping, "workout_note") or None,
                "_entries": {},
                "_order": [],
            }
            sessions[key] = session
            session_order.append(key)
        superset = _cell(row, mapping, "superset") or None
        entry = session["_entries"].get(name)
        if entry is None:
            entry = {"name": name, "rows": [], "superset": superset, "note": None}
            session["_entries"][name] = entry
            session["_order"].append(name)
        entry["note"] = entry["note"] or (_cell(row, mapping, "exercise_note") or None)
        entry["rows"].append((round_val, weight, reps, rpe_val))

    workouts = []
    for key in session_order:
        session = sessions.pop(key)
        entries = []
        for name in session.pop("_order"):
            entry = session["_entries"][name]
            rows_for_ex = entry.pop("rows")
            if all(r[0] is not None for r in rows_for_ex):
                rows_for_ex = sorted(rows_for_ex, key=lambda r: r[0])
            out = {"name": name, "sets": [[w, r, rpe] for _, w, r, rpe in rows_for_ex]}
            if entry["superset"] is not None:
                out["superset"] = entry["superset"]
            if entry["note"]:
                out["note"] = entry["note"]
            entries.append(out)
        session.pop("_entries")
        session["entries"] = entries
        workouts.append(session)
    if not workouts and first_error is not None:
        raise first_error
    stats["rows_skipped_warmup"] = warmups
    stats["rows_skipped_no_load"] = no_load
    stats["rows_skipped"] = warmups + no_load
    stats["date_order"] = slash_order
    stats["date_order_ambiguous"] = ambiguous
    if ambiguous:
        stats["date_order_example"] = _ambiguous_example(rows, mapping)
    return workouts


def _ambiguous_example(rows: list[list[str]], mapping: dict[str, int]) -> Optional[dict]:
    """Первая дата вида «a/b/гггг», где и a, и b не больше 12, — и как её
    прочитали (день/месяц, см. _slash_date_order): для предупреждения."""
    for value in _date_column_values(rows, mapping):
        text = _strip_time_suffix(value.strip())
        match = _SLASH_DATE_RE.match(text)
        if match and match["a"] != match["b"]:
            try:
                return {"text": text, "date": _parse_row_date_raw(text).isoformat()}
            except ParseError:
                continue
    return None


def _parse_set_row(
    row: list[str], mapping: dict[str, int], line_no: int, today: Optional[dt.date],
    month_first: bool, weight_factor: float, account_unit: str,
):
    """Одна строка-подход: (дата, имя, вес|None, повторы, номер, rpe).
    ParseError — уже с номером строки («Строка N: …»)."""
    try:
        date_val = _parse_row_date(row[mapping["date"]], today, month_first=month_first)
        name = formatting.strip_control_chars(
            _unescape_formula_cell(row[mapping["exercise"]].strip())
        ).strip()
        weight_text = row[mapping["weight"]].strip()
        try:
            weight = _parse_number(weight_text) * weight_factor if weight_text else None
            if weight is not None and weight_factor != 1.0:
                weight = round(weight, 1)
        except ValueError:
            raise ParseError(i18n.t("import.err_weight", text=weight_text)) from None
        reps = _parse_count(row[mapping["reps"]].strip(), _field_label("reps"))
        round_val = None
        if "round" in mapping and mapping["round"] < len(row):
            round_text = row[mapping["round"]].strip()
            if round_text and round_text.upper() not in _UNNUMBERED_SET_MARKS:
                round_val = _parse_count(round_text, _field_label("round"))
        rpe_val = None
        if "rpe" in mapping and mapping["rpe"] < len(row):
            rpe_text = row[mapping["rpe"]].strip()
            if rpe_text:
                try:
                    rpe_val = _parse_number(rpe_text)
                except ValueError:
                    raise ParseError(i18n.t("import.err_rpe", text=rpe_text)) from None
                if not (0 < rpe_val <= 10):
                    raise ParseError(i18n.t("import.err_rpe_range", text=rpe_text))
    except ParseError as e:
        raise ParseError(i18n.t("import.err_line", n=line_no, message=e.message)) from None
    except IndexError:
        # Обрезанная строка: раньше это тоже было «не разобрал вес/повторы»,
        # хотя искать нужно не число, а недостающую колонку.
        raise ParseError(
            i18n.t("import.err_line", n=line_no, message=i18n.t("import.err_too_few_cells", len=len(row)))
        ) from None
    except ValueError:
        raise ParseError(
            i18n.t("import.err_line", n=line_no, message=i18n.t("import.err_generic_parse"))
        ) from None

    def fail(message: str) -> ParseError:
        return ParseError(i18n.t("import.err_line", n=line_no, message=message))

    if not name:
        raise fail(i18n.t("import.err_empty_name"))
    # Тот же порог, что у имени упражнения в /v1 (api_v1._exercise_name):
    # импорт заводит недостающие упражнения под именем из файла, и без
    # проверки съехавшая колонка (заметки вместо названия) давала
    # упражнение в 5000 символов, разваливающее каждую карточку и список.
    if len(name) > config.MAX_EXERCISE_NAME_LENGTH:
        raise fail(i18n.t("import.err_name_too_long", max=config.MAX_EXERCISE_NAME_LENGTH))
    if reps <= 0:
        raise fail(i18n.t("import.err_reps_nonpositive"))
    # Same ceilings the typed-set parser enforces, and for the same reason:
    # an impossible set imported here is silently permanent — it becomes the
    # exercise's all-time record, joins lifetime tonnage, and unlocks weight
    # clubs that are never revoked.
    if reps > MAX_REPS:
        raise fail(i18n.t("import.err_too_many_reps", reps=reps))
    if weight is not None and weight < 0:
        raise fail(i18n.t("import.err_negative_weight", weight=weight_text))
    if weight is not None and formatting.to_kg(weight, account_unit) > MAX_WEIGHT:
        raise fail(i18n.t("import.err_too_heavy", weight=weight_text))
    return date_val, name, weight, reps, round_val, rpe_val


# ---------------------------------------------------------------------------
# Сопоставление имён из файла со своими упражнениями — ОДИН резолвер на бота,
# /v1/import/csv и заметки (они приходят в /v1 тем же CSV). Без записи в базу:
# он только решает, куда ляжет имя; заводит упражнения materialize_decisions,
# и только после «Загрузить» (до подтверждения в базе не появляется ничего).


async def resolve_exercise_names_exact(
    user_id: int, names: list[str]
) -> tuple[dict[str, int], list[str]]:
    """Точное совпадение имени упражнения с каталогом пользователя (id →
    существующее упражнение), без сети и без записи в базу."""
    resolved: dict[str, int] = {}
    unresolved: list[str] = []
    for name in dict.fromkeys(names):
        ex = await db.find_exercise_by_name(user_id, name)
        if ex:
            resolved[name] = ex["id"]
        else:
            unresolved.append(name)
    return resolved, unresolved


class NameMatch:
    """Куда ляжет одно имя из файла по решению match_exercise_names — без
    модели и без записи. Ровно одно из трёх:

      * exercise_id — своё упражнение атлета (точно, по закреплённому имени,
        по той же идентичности каталога или уверенно похожее);
      * template_name — своего нет, но имя однозначно указывает на шаблон
        каталога (русская идентичность), которого у атлета ещё нет: заведётся
        новое упражнение под именем из файла, привязанное к шаблону;
      * ни того, ни другого — новое упражнение как есть.

    needs_choice — уверенности нет, а похожие свои есть: решает человек
    (бот — кнопками, приложение — по `needs_choice` в предпросмотре), и
    «завести новое» молча не подставляется. candidates — свои упражнения,
    похожие на имя (лучшие первыми). exact — имя совпало точно или закреплено
    за упражнением (exercise_aliases): такое не переспрашивают вовсе.

    Обычный класс, а не dataclass со строковыми полями по умолчанию, — чтобы
    ничего не вычислялось при импорте (см. tests/test_no_frozen_language.py)."""

    __slots__ = ("exercise_id", "template_name", "candidates", "exact", "needs_choice", "via", "group_name")

    def __init__(
        self, exercise_id=None, template_name=None, candidates=None, exact=False,
        needs_choice=False, via=None, group_name=None,
    ):
        self.exercise_id: Optional[int] = exercise_id
        self.template_name: Optional[str] = template_name
        self.candidates: list[int] = list(candidates or [])
        self.exact: bool = exact
        self.needs_choice: bool = needs_choice
        self.via: Optional[str] = via
        # Только у нового как есть: группа мышц (русская идентичность
        # встроенной группы), которую подсказала модель, когда упражнение
        # каталога она точно назвать не смогла (guess_groups_with_model).
        # Без неё новое ложится в «Другое».
        self.group_name: Optional[str] = group_name


# Сколько похожих своих упражнений показывать на выбор при неоднозначности.
MAX_MATCH_CANDIDATES = 5


def _name_texts(row) -> list[str]:
    """Все написания, под которыми атлет может узнать своё упражнение:
    показанное имя, голое имя, идентичность каталога (original_name — русская
    всегда, на любом языке) и её перевод на другой язык. Так «Жим лёжа»
    находит англоязычный форк «Barbell Bench Press», а «Bench Press» —
    русский «Жим штанги лёжа»."""
    texts = [row["display_name"], row["name"]]
    identities = [row["original_name"], row["display_name"], row["name"]]
    for value in list(identities):
        if value:
            canonical = seed_data.canonical_exercise_name(value)
            if canonical:
                identities.append(canonical)
    for value in identities:
        if not value:
            continue
        texts.append(value)
        for lang in i18n.SUPPORTED:
            texts.append(seed_data.localized_exercise_name(value, lang))
    return list(dict.fromkeys(t for t in texts if t))


def _template_texts(template) -> list[str]:
    return [
        template["name"],
        *(seed_data.localized_exercise_name(template["name"], lang) for lang in i18n.SUPPORTED),
    ]


def _covers(text: str, groups: list[tuple[str, ...]]) -> bool:
    """Все слова запроса (группы вариантов search_terms.query_groups) есть в
    text — та же проверка, что у поиска упражнений (db._stem_filter)."""
    folded = search_terms.fold(text)
    return bool(groups) and all(any(v in folded for v in variants) for variants in groups)


@functools.lru_cache(maxsize=4096)
def _profile(text: str) -> tuple[frozenset, tuple]:
    """Основы слов и группы вариантов поиска для одного названия — чистая
    функция текста (языка в ней нет), кэш только экономит повторы."""
    return frozenset(search_terms.query_stems(text)), tuple(search_terms.query_groups(text))


def _same_words(a: str, b: str) -> bool:
    """Те же слова с точностью до порядка, регистра, ё/е, пунктуации и
    словоформы: «лёжа жим» = «Жим лёжа», «Приседания» = «Присед»."""
    stems_a = _profile(a)[0]
    return bool(stems_a) and stems_a == _profile(b)[0]


def _pick(name: str, groups, rows, texts_of) -> tuple[Optional[object], list]:
    """(уверенный выбор или None, похожие — лучшие первыми) среди rows.

    Уверенно — только когда ровно один кандидат совпал словами целиком
    («лёжа жим» = «Жим лёжа»). Похожее «вперёд» (в названии есть все слова
    имени из файла: «Жим лёжа» → «Жим штанги лёжа») и «назад» (все слова
    названия есть в имени из файла) — только кандидаты: пропущенное или
    лишнее слово-уточнение часто и есть другое движение (штанга против
    гантелей, наклонная скамья, Смит), и угадывать за человека здесь нельзя."""
    same, forward, backward = [], [], []
    for row in rows:
        texts = texts_of(row)
        if any(_same_words(name, t) for t in texts):
            same.append(row)
        elif any(_covers(t, groups) for t in texts):
            forward.append(row)
        elif any(_covers(name, _profile(t)[1]) for t in texts):
            backward.append(row)
    ordered = same + forward + backward
    if len(same) == 1:
        return same[0], ordered
    return None, ordered


def _pick_template(name: str, groups, rows, texts_of) -> Optional[object]:
    """Шаблон каталога для нового упражнения: совпал словами целиком ровно
    один, или (никто целиком) «вперёд» подошёл ровно один, а в имени хотя бы
    два слова. Здесь угадывать можно: решается не куда лечь чужой истории,
    а только какая картинка и группа будут у нового упражнения."""
    chosen, similar = _pick(name, groups, rows, texts_of)
    if chosen is not None:
        return chosen
    forward = [r for r in similar if any(_covers(t, groups) for t in texts_of(r))]
    if len(forward) == 1 and len(groups) >= 2:
        return forward[0]
    return None


async def match_exercise_names(
    user_id: int, names: list[str], *, use_model: bool = False,
) -> dict[str, NameMatch]:
    """Единственный резолв имён импорта — бот, /v1/import/csv (предпросмотр и
    коммит) и заметки идут через него, поэтому показанное и записанное
    совпадают. Без записи в базу.

    Порядок: точное имя своего упражнения → имя, закреплённое за упражнением
    (exercise_aliases: прошлый выбор человека или объединение) → шаблон
    каталога: если у атлета уже есть упражнение с той же идентичностью
    (original_name), имя ложится в него, новое НЕ заводится (штанга, гантели,
    наклонная, Смит — разные идентичности каталога и остаются разными) →
    похожее своё (порядок слов, словоформы, синонимы поиска): одно совпавшее
    словами целиком — уверенно, иначе needs_choice → шаблон, которого у
    атлета нет → новое как есть.

    use_model — для имён, которые иначе стали бы «новым как есть», спросить
    модель, какое это движение каталога (refine_with_model): с той же
    проверкой идентичности. Платный шаг: зовёт его только вызывающий, у
    которого на это есть согласие человека.
    """
    unique = list(dict.fromkeys(n for n in names if n))
    exact, rest = await resolve_exercise_names_exact(user_id, unique)
    out = {name: NameMatch(exercise_id=ex_id, exact=True, via="exact") for name, ex_id in exact.items()}
    remaining = []
    for name in rest:
        aliased = await db.find_exercise_by_alias(user_id, name)
        if aliased is not None:
            out[name] = NameMatch(exercise_id=aliased["id"], exact=True, via="alias")
        else:
            remaining.append(name)
    if not remaining:
        return {n: out[n] for n in unique}

    own = await db.list_user_exercises(user_id)
    own_texts = {r["id"]: _name_texts(r) for r in own}
    identity_owner = await _identity_owners(user_id)
    exact_templates = await db.find_global_templates_by_names(remaining)
    # Стандартные названия Hevy, которые каталог называет иначе («Lat
    # Pulldown (Cable)» → «Тяга верхнего блока»), — словарём, раньше модели:
    # бесплатно и одинаково при каждом импорте (seed_data.HEVY_CATALOG_ALIASES).
    alias_targets = {
        n: t for n in remaining
        if n not in exact_templates and (t := seed_data.hevy_catalog_identity(n))
    }
    if alias_targets:
        alias_rows = await db.find_global_templates_by_names(list(set(alias_targets.values())))
        for n, t in alias_targets.items():
            if t in alias_rows:
                exact_templates[n] = alias_rows[t]
    catalog = None
    plain: list[str] = []

    for name in remaining:
        groups = search_terms.query_groups(name)
        template = exact_templates.get(name)
        if template is not None:
            owner = identity_owner.get(search_terms.fold(template["name"]))
            if owner is not None:
                out[name] = NameMatch(exercise_id=owner, candidates=[owner], via="identity")
                continue
            _, similar = _pick(name, groups, own, lambda r: own_texts[r["id"]]) if groups else (None, [])
            if similar:
                out[name] = NameMatch(
                    template_name=template["name"], needs_choice=True, via="similar",
                    candidates=[r["id"] for r in similar[:MAX_MATCH_CANDIDATES]],
                )
            else:
                out[name] = NameMatch(template_name=template["name"], via="template")
            continue

        if not groups:
            out[name] = NameMatch(via="new")
            plain.append(name)
            continue
        chosen, similar = _pick(name, groups, own, lambda r: own_texts[r["id"]])
        candidates = [r["id"] for r in similar[:MAX_MATCH_CANDIDATES]]
        if catalog is None:
            catalog = [
                t for t in await db.list_all_exercise_templates()
                if t["user_id"] is None and search_terms.fold(t["name"]) not in identity_owner
            ]
        chosen_t = _pick_template(name, groups, catalog, _template_texts)
        template_name = chosen_t["name"] if chosen_t is not None else None
        if chosen is not None:
            out[name] = NameMatch(exercise_id=chosen["id"], candidates=candidates, via="similar")
        elif similar:
            out[name] = NameMatch(
                template_name=template_name, candidates=candidates, needs_choice=True, via="similar",
            )
        elif template_name is not None:
            out[name] = NameMatch(template_name=template_name, via="template")
        else:
            out[name] = NameMatch(via="new")
            plain.append(name)

    if use_model and plain:
        await refine_with_model(user_id, out, plain, identity_owner)
    return {n: out[n] for n in unique}


async def _identity_owners(user_id: int) -> dict[str, int]:
    """Свёрнутая идентичность каталога → своё живое упражнение с ней. Все
    живые, включая скрытые остатки программ (их нет в list_user_exercises):
    второй форк того же шаблона — это дубль, раскалывающий историю."""
    owners: dict[str, int] = {}
    for r in await db.list_active_exercises_with_identity(user_id):
        owners.setdefault(search_terms.fold(r["original_name"]), r["id"])
    return owners


# Ответы модели «имя из файла → шаблон каталога» на время жизни процесса:
# предпросмотр в приложении зовут несколько раз подряд (переключили единицы),
# и платить за то же сопоставление каждый раз незачем. Коммит берёт ровно то,
# что показал предпросмотр.
_MODEL_MATCH_CACHE: "collections.OrderedDict[tuple[int, str], Optional[str]]" = collections.OrderedDict()
_MODEL_MATCH_CACHE_SIZE = 4096


async def refine_with_model(
    user_id: int, matches: dict[str, NameMatch], names: list[str],
    identity_owner: Optional[dict[str, int]] = None,
) -> None:
    """Имена, которые иначе стали бы «новым как есть», — модели: какое это
    движение каталога. Совпало с идентичностью, которая у атлета уже есть, —
    имя ложится в его упражнение; нет — новое заводится, привязанным к
    шаблону (группа, фото, техника). Платный вызов — через
    ai_trainer.match_exercise_names_to_catalog (paid_call и HARD-стоп по
    деньгам внутри); модель недоступна — имена остаются новыми."""
    ask = [n for n in names if (user_id, n) not in _MODEL_MATCH_CACHE]
    found = {n: _MODEL_MATCH_CACHE[(user_id, n)] for n in names if (user_id, n) in _MODEL_MATCH_CACHE}
    if ask:
        # Модель не настроена — match_exercise_names_to_catalog ответит пустым,
        # и такой пустой ответ не запоминаем.
        answers = await ai_trainer.match_exercise_names_to_catalog(user_id, ask)
        for name in ask:
            found[name] = answers.get(name)
            if ai_trainer.is_configured():
                _MODEL_MATCH_CACHE[(user_id, name)] = answers.get(name)
        while len(_MODEL_MATCH_CACHE) > _MODEL_MATCH_CACHE_SIZE:
            _MODEL_MATCH_CACHE.popitem(last=False)
    found = {n: c for n, c in found.items() if c}
    templates = await db.find_global_templates_by_names(list(set(found.values()))) if found else {}
    if identity_owner is None:
        identity_owner = await _identity_owners(user_id)
    for name, catalog_name in found.items():
        template = templates.get(catalog_name)
        if template is None:
            continue
        owner = identity_owner.get(search_terms.fold(template["name"]))
        if owner is not None:
            matches[name] = NameMatch(exercise_id=owner, candidates=[owner], via="model")
        else:
            matches[name] = NameMatch(template_name=template["name"], via="model")
    # Что модель упражнению каталога не привязала (не уверена: «жим гантелей»
    # без «сидя»/«стоя»), остаётся новым как есть — но хотя бы в своей группе
    # мышц, а не в «Другом». Чужую историю это не смешивает: упражнение
    # отдельное, у него нет фото и техники каталога, только группа.
    still_new = [n for n in names if matches.get(n) is not None and matches[n].via == "new"]
    if still_new:
        for name, group in (await guess_groups_with_model(user_id, still_new)).items():
            matches[name] = NameMatch(via="new", group_name=group)


# Ответы модели «имя → группа мышц» — тот же смысл, что у _MODEL_MATCH_CACHE.
_MODEL_GROUP_CACHE: "collections.OrderedDict[tuple[int, str], Optional[str]]" = collections.OrderedDict()


async def guess_groups_with_model(user_id: int, names: list[str]) -> dict[str, str]:
    """Имя нового упражнения → встроенная группа мышц (русская идентичность),
    если модель в ней уверена. Модель недоступна — пусто, упражнения лягут в
    «Другое», как раньше."""
    ask = [n for n in names if (user_id, n) not in _MODEL_GROUP_CACHE]
    found = {n: _MODEL_GROUP_CACHE[(user_id, n)] for n in names if (user_id, n) in _MODEL_GROUP_CACHE}
    if ask:
        answers = await ai_trainer.guess_exercise_groups(user_id, ask)
        for name in ask:
            found[name] = answers.get(name)
            if ai_trainer.is_configured():
                _MODEL_GROUP_CACHE[(user_id, name)] = answers.get(name)
        while len(_MODEL_GROUP_CACHE) > _MODEL_MATCH_CACHE_SIZE:
            _MODEL_GROUP_CACHE.popitem(last=False)
    return {n: g for n, g in found.items() if g}


def default_decisions(matches: dict[str, NameMatch]) -> dict[str, dict]:
    """Решение по каждому уверенному имени — то, что будет записано без
    вопросов: {"kind": "existing", "id"} | {"kind": "template", "template"} |
    {"kind": "new"}. Имена с needs_choice сюда не попадают: их решает человек."""
    out: dict[str, dict] = {}
    for name, m in matches.items():
        if m.needs_choice:
            continue
        if m.exercise_id is not None:
            out[name] = {"kind": "existing", "id": m.exercise_id}
        elif m.template_name:
            out[name] = {"kind": "template", "template": m.template_name}
        elif m.group_name:
            out[name] = {"kind": "new", "group": m.group_name}
        else:
            out[name] = {"kind": "new"}
    return out


def resolved_ids(decisions: dict[str, dict]) -> dict[str, int]:
    """Имя → id для уже существующих упражнений (остальные заведёт сохранение)."""
    return {n: d["id"] for n, d in decisions.items() if d.get("kind") == "existing"}


async def _allows_empty_weight(user_id: int, decision: Optional[dict], all_unweighted: bool) -> bool:
    """Можно ли подходу этого упражнения быть без веса (подход с весом тела).

    Упражнение с собственным весом (подтягивания, брусья — bodyweight_load у
    своего упражнения или у шаблона) — да. Упражнение с отягощением — нет:
    пустая ячейка веса у жима не «0 кг», а дырка в файле, и такой подход
    пропускается (no_weight), а не становится рекордом «0×10». Своё или новое
    без признака: да, если у него нет ни одного подхода с весом ни в истории,
    ни в этом файле (упражнение на повторы, которое атлет так и ведёт)."""
    if decision is None or decision.get("kind") == "new":
        return all_unweighted
    if decision["kind"] == "template":
        template = await db._find_global_template_by_name(decision["template"])
        return template is not None and template["bodyweight_load"] != "none"
    exercise = await db.get_exercise(decision["id"])
    if exercise is None:
        return all_unweighted
    if exercise["bodyweight_load"] != "none":
        return True
    cur = await db.conn().execute(
        "SELECT 1 FROM sets WHERE exercise_id = ? AND weight > 0 LIMIT 1", (exercise["id"],)
    )
    if await cur.fetchone() is not None:
        return False
    return all_unweighted


async def drop_unweighted_sets(
    user_id: int, workouts: list[dict], decisions: dict[str, dict], stats: Optional[dict] = None,
) -> list[dict]:
    """Подходы без веса (ячейка пуста или ноль) — с весом тела у упражнений,
    которым так можно (_allows_empty_weight), остальные пропускаются с
    причиной no_weight. Упражнение, у которого не осталось подходов,
    выпадает из тренировки, тренировка без упражнений — из импорта.
    Возвращает новый список; вес None в нём уже 0.0."""
    unweighted_everywhere: dict[str, bool] = {}
    for w in workouts:
        for entry in w["entries"]:
            has_weight = any(s[0] for s in entry["sets"])
            unweighted_everywhere[entry["name"]] = (
                unweighted_everywhere.get(entry["name"], True) and not has_weight
            )
    allowed: dict[str, bool] = {}
    out = []
    for w in workouts:
        entries = []
        for entry in w["entries"]:
            name = entry["name"]
            if any(not s[0] for s in entry["sets"]) and name not in allowed:
                allowed[name] = await _allows_empty_weight(
                    user_id, decisions.get(name), unweighted_everywhere[name]
                )
            kept = []
            for weight, reps, rpe in entry["sets"]:
                if not weight:
                    if not allowed.get(name):
                        if stats is not None:
                            add_skip(stats, "no_weight", f"{name} ×{reps}")
                        continue
                    weight = 0.0
                kept.append([weight, reps, rpe])
            if kept:
                entries.append({**entry, "sets": kept})
        if entries:
            out.append({**w, "entries": entries})
    return out


def _utc_iso(local_iso: str, tz_offset: int) -> str:
    """Местное время атлета из файла → наивный UTC, как всё в базе."""
    moment = dt.datetime.fromisoformat(local_iso) - dt.timedelta(hours=int(tz_offset))
    return moment.isoformat(timespec="seconds")


def _session_times(w: dict, tz_offset: int) -> tuple[str, str]:
    """(started_at, finished_at) тренировки для базы: настоящее время из
    файла, а без него — задним числом за её день (timeutil.backdated_moment)."""
    if w.get("started_at"):
        started = _utc_iso(w["started_at"], tz_offset)
        finished = _utc_iso(w.get("finished_at") or w["started_at"], tz_offset)
        return started, max(started, finished)
    moment = timeutil.backdated_moment(dt.date.fromisoformat(w["date"]), tz_offset)
    return moment, moment


def _signature(w: dict, resolved: dict[str, int]) -> Optional[tuple]:
    """Состав тренировки для сверки с историей: отсортированные (упражнение,
    вес, повторы) — или None, если хоть одно имя ещё не сопоставлено (новое
    упражнение по составу совпасть с историей не может)."""
    items = []
    for entry in w["entries"]:
        ex_id = resolved.get(entry["name"])
        if ex_id is None:
            return None
        for weight, reps, _rpe in entry["sets"]:
            items.append((ex_id, round(float(weight or 0), 1), int(reps)))
    return tuple(sorted(items))


async def _duplicate_sessions(
    user_id: int, workouts: list[dict], resolved: Optional[dict[str, int]] = None
) -> set[int]:
    """Номера тренировок файла, которые уже есть в истории.

    Дубль — это та же СЕССИЯ, а не тот же день: тренировка с тем же началом
    с точностью до минуты (у Hevy/Strong время есть в файле) или с тем же
    составом — упражнения, веса и повторы — в тот же день. Раньше дублем
    считалась любая дата, где уже было хоть одно из упражнений файла: вторая
    тренировка дня из того же файла пропадала целиком, а ручная тренировка
    того же дня «съедала» импорт. Повторная заливка того же файла
    по-прежнему не удваивает историю: совпадут и время, и состав."""
    resolved = resolved or {}
    tz_offset = await db.user_tz_offset(user_id)
    existing = await db.list_session_fingerprints(user_id, tz_offset=tz_offset)
    by_minute = {e["started_at"][:16] for e in existing}
    by_day: dict[str, set[tuple]] = {}
    for e in existing:
        by_day.setdefault(e["date"], set()).add(tuple(sorted(e["sets"])))
    dup = set()
    for i, w in enumerate(workouts):
        if w.get("started_at"):
            started, _ = _session_times(w, tz_offset)
            if started[:16] in by_minute:
                dup.add(i)
                continue
        signature = _signature(w, resolved)
        if signature and signature in by_day.get(w["date"], ()):
            dup.add(i)
    return dup


async def _duplicate_dates(
    user_id: int, workouts: list[dict], resolved: Optional[dict[str, int]] = None
) -> set[str]:
    """Даты тренировок-дублей (см. _duplicate_sessions) — для поля
    `duplicate_dates` у REST и старых вызывающих."""
    return {workouts[i]["date"] for i in await _duplicate_sessions(user_id, workouts, resolved)}


async def materialize_decisions(
    user_id: int, decisions: dict[str, dict], names: Iterable[str], batch_id: Optional[str] = None,
) -> dict[str, int]:
    """Завести то, что решено завести, — уже после «Загрузить». Имя → id.

    template: если у атлета уже есть упражнение с этой идентичностью — в
    него; иначе новое под ИМЕНЕМ ИЗ ФАЙЛА, привязанное к шаблону (группа,
    фото, техника) — тот же порядок имён и у ручного выбора шаблона, и у
    модели: «Bench Press (Barbell)» не переименовывается в «Жим штанги
    лёжа». Несколько имён файла на один шаблон — одно упражнение.
    new: новое как есть, в выбранной группе или в «Другое».
    Выбор человека (decision["chosen"]) закрепляется за упражнением
    (exercise_aliases): следующий импорт того же имени не переспрашивает.
    Заведённое этим импортом помечается пачкой — отмена импорта его снесёт."""
    resolved: dict[str, int] = {}
    owners = await _identity_owners(user_id)
    by_template: dict[str, int] = {}

    async def created(name: str, make) -> Optional[int]:
        # make — ещё не запущенная корутина создания: сначала смотрим, было ли
        # такое упражнение до импорта (тогда оно не «заведено импортом»).
        before = await db.find_exercise_by_display_name(user_id, name)
        ex_id = await make
        if ex_id is not None and before is None and batch_id:
            await db.tag_exercise_import_batch(ex_id, batch_id)
        return ex_id

    for name in dict.fromkeys(names):
        decision = decisions.get(name) or {"kind": "new"}
        kind = decision.get("kind")
        ex_id: Optional[int] = None
        if kind == "existing":
            ex_id = decision["id"]
        elif kind == "template":
            key = search_terms.fold(decision["template"])
            ex_id = owners.get(key) or by_template.get(key)
            if ex_id is None:
                ex_id = await created(
                    name, db.create_exercise_matching_catalog_name(user_id, name, decision["template"]),
                )
                if ex_id is not None:
                    by_template[key] = ex_id
        if ex_id is None:
            group_id = decision.get("group_id")
            if group_id is None and decision.get("group"):
                group_id = await db.builtin_muscle_group_id(decision["group"])
            ex_id = await created(name, db.create_exercise(user_id, name, group_id))
        resolved[name] = ex_id
        if decision.get("chosen"):
            await db.set_exercise_alias(user_id, name, ex_id, "import")
    return resolved


async def resolve_exercise_names_via_ai(
    user_id: int, unresolved: list[str], use_model: bool = True
) -> dict[str, int]:
    """Старый вход «сопоставь и заведи сразу» — оставлен для совместимости:
    теперь это тот же резолвер (match_exercise_names) и та же запись
    (materialize_decisions), что у импорта; имена, по которым нужен выбор
    человека, сюда не попадают."""
    matches = await match_exercise_names(user_id, unresolved, use_model=use_model)
    decisions = {
        n: d for n, d in default_decisions(matches).items()
        if d["kind"] != "new"
    }
    return await materialize_decisions(user_id, decisions, decisions.keys())


async def apply_import(
    user_id: int, workouts: list[dict], resolved: dict[str, int], *,
    batch_id: Optional[str] = None, report: Optional[dict] = None,
) -> tuple[int, int]:
    """Записать разобранные тренировки в базу — общий код бота и REST.

    Каждая тренировка — своя попытка: исключение посреди одной не портит уже
    записанные соседние и не оставляет эту наполовину записанной. Тренировка
    встаёт на своё настоящее время из файла (или задним числом за свой день),
    с названием и заметкой; подходы — в порядке файла и со временем внутри
    сессии, суперсет Hevy (superset_id) — одним блоком-суперсетом, как при
    ручной записи. achievement_sync запускается тем же поводом, что и у
    правки прошлого; открытые импортом значки получают дату тренировки,
    которая их дала, и метку пачки (их снимет отмена импорта).

    `report`, если передан, получает workout_ids, sets и новые значки
    (achievements). Возвращает (сколько тренировок записано, сколько сорвалось).
    """
    tz_offset = await db.user_tz_offset(user_id)
    imported = 0
    failed = 0
    sets_written = 0
    workout_ids: list[int] = []
    for w in workouts:
        started_at, finished_at = _session_times(w, tz_offset)
        workout_id = await db.create_finished_workout(
            user_id, started_at, finished_at, source="import", note=w.get("note"),
            title=w.get("title"), import_batch_id=batch_id,
        )
        try:
            written = await _write_session(workout_id, w, resolved, started_at, finished_at)
        except Exception:
            logger.exception("import: workout on %s failed for user %s", w.get("date"), user_id)
            await db.discard_workout(workout_id)
            failed += 1
            continue
        imported += 1
        sets_written += written
        workout_ids.append(workout_id)

    new_codes: list[str] = []
    if imported:
        new_codes, _ = await achievement_sync.resync(user_id)
        if new_codes and batch_id:
            earned_on = await achievement_sync.earned_dates(user_id, new_codes)
            await db.tag_achievements_import_batch(user_id, earned_on, batch_id)
    if report is not None:
        report["workout_ids"] = workout_ids
        report["sets"] = sets_written
        report["achievements"] = list(new_codes)
    return imported, failed


async def _write_session(
    workout_id: int, w: dict, resolved: dict[str, int], started_at: str, finished_at: str,
) -> int:
    """Блоки и подходы одной тренировки. Время подходов раскладывается
    равномерно между началом и концом сессии: длительность перенесённой
    тренировки — настоящая, а не «секунда в момент загрузки»."""
    groups: list[list[dict]] = []
    by_superset: dict[str, list[dict]] = {}
    for entry in w["entries"]:
        key = entry.get("superset")
        if key is not None and key in by_superset:
            by_superset[key].append(entry)
            continue
        group = [entry]
        groups.append(group)
        if key is not None:
            by_superset[key] = group
    total = sum(len(e["sets"]) for e in w["entries"])
    start = dt.datetime.fromisoformat(started_at)
    span = (dt.datetime.fromisoformat(finished_at) - start).total_seconds()
    position = 0
    for group in groups:
        block_id = await db.create_block(workout_id, "superset" if len(group) > 1 else "single")
        for order, entry in enumerate(group):
            ex_id = resolved[entry["name"]]
            await db.add_block_exercise(block_id, ex_id, order)
            await db.touch_exercise_last_used(ex_id)
            if entry.get("note"):
                await db.set_workout_exercise_note(workout_id, ex_id, entry["note"])
        rounds = max(len(e["sets"]) for e in group)
        for idx in range(rounds):
            for order, entry in enumerate(group):
                if idx >= len(entry["sets"]):
                    continue
                weight, reps, rpe = entry["sets"][idx]
                offset = span * position / (total - 1) if total > 1 else 0
                created = (start + dt.timedelta(seconds=offset)).isoformat(timespec="seconds")
                await db.add_set(
                    block_id, resolved[entry["name"]], idx + 1, order, weight or 0.0, reps, rpe,
                    created_at=created,
                )
                position += 1
    return total


def detect_source(stats: dict) -> str:
    """Источник пачки импорта для списка «что загружал»: hevy/strong/csv/notes."""
    source = stats.get("source") or "other"
    if stats.get("from_notes"):
        return "notes"
    return source if source in ("hevy", "strong") else "csv"


async def run_import(
    user_id: int, workouts: list[dict], decisions: dict[str, dict], source: str,
) -> dict:
    """Сохранение импорта целиком — один путь у бота и REST: завести решённые
    упражнения, открыть пачку, записать тренировки, пометить значки.
    Возвращает {"batch_id" (None, если ничего не записалось), "imported", "failed", "sets", "achievements",
    "resolved"}."""
    names = [e["name"] for w in workouts for e in w["entries"]]
    batch_id = await db.create_import_batch(user_id, source)
    resolved = await materialize_decisions(user_id, decisions, names, batch_id=batch_id)
    report: dict = {}
    imported, failed = await apply_import(
        user_id, workouts, resolved, batch_id=batch_id, report=report,
    )
    if imported:
        await db.finish_import_batch(batch_id, imported, report.get("sets", 0))
    else:
        # Полный провал: пустая пачка отменять нечего, а клиенту её id был бы
        # ссылкой в никуда.
        await db.discard_import_batch(user_id, batch_id)
        batch_id = None
    return {
        "batch_id": batch_id,
        "imported": imported,
        "failed": failed,
        "sets": report.get("sets", 0) if imported else 0,
        "achievements": report.get("achievements", []),
        "resolved": resolved,
    }


async def undo_import(user_id: int, batch_id: str) -> Optional[dict]:
    """Отменить импорт целиком (db.undo_import_batch) и пересчитать значки по
    оставшейся истории. None — пачки нет, она чужая, уже отменена или старше
    config.IMPORT_UNDO_DAYS."""
    batch = await db.get_import_batch(batch_id)
    if batch is None or batch["user_id"] != user_id or batch["undone_at"] is not None:
        return None
    if not _batch_in_window(batch):
        return None
    result = await db.undo_import_batch(user_id, batch_id)
    if result is not None:
        await achievement_sync.resync(user_id)
    return result


def _batch_in_window(batch) -> bool:
    created = dt.datetime.fromisoformat(batch["created_at"])
    return timeutil.utc_now() - created <= dt.timedelta(days=config.IMPORT_UNDO_DAYS)


async def undoable_batches(user_id: int) -> list[dict]:
    """Импорты за config.IMPORT_UNDO_DAYS — для экрана импорта и
    GET /v1/import/batches."""
    out = []
    for row in await db.list_import_batches(user_id, config.IMPORT_UNDO_DAYS):
        out.append({
            "batch_id": row["id"],
            "created_at": row["created_at"],
            "source": row["source"],
            "workouts": row["workouts"],
            "sets": row["sets"],
            "can_undo": row["undone_at"] is None and row["live_workouts"] > 0 and _batch_in_window(row),
        })
    return out


def warnings_for(stats: dict) -> list[dict]:
    """Предупреждения разбора на языке человека: [{"code", "message"}].
    ambiguous_date — даты вида «03/09/2026», которые читаются двумя
    способами, а файл не доказал ни один: называем, как прочитали."""
    out = []
    if stats.get("date_order_ambiguous") and stats.get("date_order_example"):
        example = stats["date_order_example"]
        out.append({
            "code": "ambiguous_date",
            "message": i18n.t(
                "import.warning.ambiguous_date",
                example=example["text"],
                date=formatting.format_day_month_ru(dt.date.fromisoformat(example["date"]), dt.date.min),
            ),
        })
    for item in stats.get("warnings_extra") or []:
        out.append(dict(item))
    return out


def achievements_summary(codes: list[str]) -> list[dict]:
    """[{"code", "title", "emoji"}] открытых значков — в порядке каталога."""
    order = {a.code: i for i, a in enumerate(achievements.CATALOG)}
    out = []
    for code in sorted(codes, key=lambda c: order.get(c, len(order))):
        badge = achievements.BY_CODE.get(code)
        if badge is not None:
            out.append({"code": code, "title": badge.title, "emoji": badge.emoji})
    return out


# ---------------------------------------------------------------------------
# Бот: от разобранного файла до подтверждения и записи.


async def _finish_mapping(event, state: FSMContext) -> None:
    data = await state.get_data()

    async def _back_to_file(text: str) -> None:
        await state.set_state(ImportFlow.awaiting_file)
        kb = keyboards.cancel_keyboard("imp:cancel")
        if isinstance(event, CallbackQuery):
            await ui.safe_edit(event, text, reply_markup=kb)
        else:
            await event.answer(text, reply_markup=kb)

    user = await db.get_user(event.from_user.id)
    # Заметки приезжают сюда уже CSV (import_notes_go) — со своими пропусками
    # и предупреждениями от разбора текста, их и продолжаем.
    stats: dict = copy.deepcopy(data.get("imp_stats_seed") or {})
    headers = data.get("imp_headers") or []
    try:
        workouts = _build_workout_groups(
            data["imp_rows"], data["imp_mapping"],
            first_line=2 if data.get("imp_has_header", True) else 1,
            today=timeutil.user_today(user),
            # Считаем здесь, а не при приёме файла: колонку веса могли выбрать
            # руками, и единица берётся из заголовка той колонки, что выбрана в
            # итоге, — какой бы дорогой она сюда ни пришла.
            weight_factor=_weight_factor(headers, data["imp_mapping"], user["unit"]),
            stats=stats,
            account_unit=user["unit"],
        )
    except ParseError as e:
        await _back_to_file(i18n.t("import.file_error", message=e.message))
        return
    if not workouts:
        # Раньше пустой результат доезжал до подтверждения «0 тренировки» с
        # кнопкой «✅ Загрузить», которая рапортовала «Импортировано 0 тренировок».
        await _back_to_file(i18n.t("import.no_sets_found"))
        return
    stats.setdefault("source", _file_source(headers, data.get("imp_has_header", True)))

    user_id = event.from_user.id
    all_names = [entry["name"] for w in workouts for entry in w["entries"]]
    matches = await match_exercise_names(user_id, all_names)
    plain = [n for n, m in matches.items() if m.via == "new"]
    if plain and ai_trainer.is_configured():
        # Совпадение через модель — сетевой вызов, секунды: без знака, что
        # файл вообще читается, бот выглядит зависшим.
        progress_text = i18n.t("import.matching_progress")
        if isinstance(event, CallbackQuery):
            await event.message.answer(progress_text)
        else:
            await event.answer(progress_text)
        await refine_with_model(user_id, matches, plain)
    decisions = default_decisions(matches)
    choices = {
        name: {"candidates": m.candidates, "template": m.template_name}
        for name, m in matches.items() if m.needs_choice
    }
    await state.update_data(
        imp_workouts=workouts, imp_stats=stats, imp_decisions=decisions,
        imp_resolved=resolved_ids(decisions), imp_choices=choices,
        # Сырые строки файла дальше не нужны — состояние диалога лежит в
        # файле FSM, и тысячи строк большого экспорта держать там незачем.
        imp_rows=None,
    )
    if choices:
        from handlers.exercise_resolve import start as start_resolve
        await start_resolve(event, state, list(choices))
    else:
        await show_confirmation(event, state)


async def on_exercises_resolved(event, state: FSMContext) -> None:
    """Человек ответил на все вопросы «куда это имя» — решения (ничего ещё
    не заведено) добавляются к уверенным, дальше — подтверждение."""
    data = await state.get_data()
    decisions = dict(data.get("imp_decisions") or {})
    decisions.update(data.get("resolve_decisions") or {})
    # Старые состояния (до решений) знали только имя → id.
    for name, ex_id in (data.get("resolve_resolved") or {}).items():
        decisions.setdefault(name, {"kind": "existing", "id": ex_id})
    await state.update_data(imp_decisions=decisions, imp_resolved=resolved_ids(decisions))
    await show_confirmation(event, state)


def _decisions_from_state(data: dict) -> dict[str, dict]:
    decisions = dict(data.get("imp_decisions") or {})
    for name, ex_id in (data.get("imp_resolved") or {}).items():
        decisions.setdefault(name, {"kind": "existing", "id": ex_id})
    return decisions


IMPORT_PAGE_SIZE = 8


def _session_label(w: dict) -> str:
    """«08:27 · Push» — время и название сессии рядом с датой."""
    parts = []
    if w.get("started_at"):
        parts.append(w["started_at"][11:16])
    if w.get("title"):
        parts.append(w["title"])
    return " · ".join(parts)


def _ordered_indices(workouts: list[dict]) -> list[int]:
    """Порядок показа: новые первыми, как в 📚 Истории, — по дате и времени,
    а не в порядке строк файла."""
    return sorted(
        range(len(workouts)),
        key=lambda i: (workouts[i]["date"], workouts[i].get("started_at") or ""),
        reverse=True,
    )


def _report_lines(stats: dict) -> list[str]:
    """Что пропущено и о чём предупредить — строками для экрана бота."""
    lines = []
    for item in skipped_report(stats):
        examples = ", ".join(item["examples"])
        lines.append(
            i18n.t("import.skipped_line", label=escape(item["label"], quote=False), examples=escape(examples, quote=False))
            if examples else i18n.t("import.skipped_line_bare", label=escape(item["label"], quote=False))
        )
    for warning in warnings_for(stats):
        lines.append(f"⚠️ {escape(warning['message'], quote=False)}")
    return lines


async def _render_confirmation_page(event, state: FSMContext, page: int) -> None:
    """Несколько тренировок на странице, как в 📚 Истории: дата, время и
    название сессии и до трёх упражнений короткими буллитами. Решение
    «загрузить» общее для всех страниц; дубли отмечены у своей тренировки.
    Если загружать нечего (всё уже есть) — это говорится прямо, а «загрузить
    с дублями» уходит во второстепенное меню."""
    data = await state.get_data()
    workouts = data.get("imp_ready") or data["imp_workouts"]
    dup = set(data.get("imp_dup_idx") or [])
    if "imp_dup_idx" not in data and data.get("imp_dup"):
        dup = {i for i, w in enumerate(workouts) if w["date"] in set(data["imp_dup"])}
    order = _ordered_indices(workouts)
    total_pages = max(1, -(-len(order) // IMPORT_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * IMPORT_PAGE_SIZE
    entries = [
        (
            dt.date.fromisoformat(workouts[i]["date"]),
            [e["name"] for e in workouts[i]["entries"]],
            i in dup,
            _session_label(workouts[i]),
        )
        for i in order[start:start + IMPORT_PAGE_SIZE]
    ]
    new_count = len(workouts) - len(dup)
    header = i18n.t("import.confirm_header", n=len(workouts))
    if total_pages > 1:
        header += i18n.t("import.page_suffix", page=page + 1, total=total_pages)
    text = formatting.build_import_confirmation_list(entries, set(), header)
    if page == 0:
        extra = _report_lines(data.get("imp_view_stats") or {})
        if new_count == 0:
            extra.insert(0, i18n.t("import.all_already_there"))
        if extra:
            text += "\n\n" + "\n".join(extra)
    if new_count == 0:
        kb = keyboards.csv_import_all_duplicates_keyboard(page, total_pages)
    else:
        kb = keyboards.csv_import_page_keyboard(page, total_pages, new_count, len(dup))
    await state.update_data(imp_confirm_page=page)
    if isinstance(event, CallbackQuery):
        await ui.safe_edit(event, text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


async def _prepare(user_id: int, data: dict) -> tuple[list[dict], set[int], dict]:
    """(что загрузится, номера дублей, отчёт) — по текущим решениям, без записи."""
    view_stats = copy.deepcopy(data.get("imp_stats") or {})
    decisions = _decisions_from_state(data)
    ready = await drop_unweighted_sets(user_id, data["imp_workouts"], decisions, view_stats)
    dup = await _duplicate_sessions(user_id, ready, resolved_ids(decisions))
    return ready, dup, view_stats


async def show_confirmation(event, state: FSMContext) -> None:
    data = await state.get_data()
    ready, dup, view_stats = await _prepare(event.from_user.id, data)
    if not ready:
        await state.set_state(ImportFlow.awaiting_file)
        text = i18n.t("import.no_sets_found")
        extra = _report_lines(view_stats)
        if extra:
            text += "\n\n" + "\n".join(extra)
        kb = keyboards.cancel_keyboard("imp:cancel")
        if isinstance(event, CallbackQuery):
            await ui.safe_edit(event, text, reply_markup=kb, parse_mode="HTML")
        else:
            await event.answer(text, reply_markup=kb, parse_mode="HTML")
        return
    await state.update_data(imp_ready=ready, imp_dup_idx=sorted(dup), imp_view_stats=view_stats)
    await state.set_state(ImportFlow.confirming)
    await _render_confirmation_page(event, state, 0)


@router.callback_query(StateFilter(ImportFlow.confirming), F.data.startswith("imp:page:"))
async def import_confirm_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[2])
    await _render_confirmation_page(callback, state, page)
    await callback.answer()


@router.callback_query(StateFilter(ImportFlow.confirming), F.data == "imp:more")
async def import_more(callback: CallbackQuery, state: FSMContext):
    """Второстепенное меню при «всё уже есть»: загрузить с дублями — только
    отсюда, а не единственной кнопкой на главном экране."""
    data = await state.get_data()
    total = len(data.get("imp_ready") or data.get("imp_workouts") or [])
    await ui.safe_edit(
        callback, i18n.t("import.dupes_submenu", n=total),
        reply_markup=keyboards.csv_import_dupes_submenu_keyboard(total),
    )
    await callback.answer()


@router.callback_query(StateFilter(ImportFlow.confirming), F.data == "imp:moreback")
async def import_more_back(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await _render_confirmation_page(callback, state, data.get("imp_confirm_page") or 0)
    await callback.answer()


@router.callback_query(StateFilter(ImportFlow.confirming), F.data.in_({"imp:save", "imp:saveall"}))
async def import_save(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not _try_claim_saving(user_id):
        # Второй тап застаёт первый посреди записи — без этой брони оба видят
        # одно и то же ImportFlow.confirming и файл записывается дважды.
        await callback.answer(i18n.t("import.already_uploading"))
        return
    try:
        await _do_import_save(callback, state)
    finally:
        _saving.discard(user_id)


async def _do_import_save(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    user_id = callback.from_user.id
    # imp:saveall — человек посмотрел на список дублей и всё равно хочет их.
    # imp:save грузит только то, чего в истории ещё нет.
    force = callback.data == "imp:saveall"

    # Пересчитываем здесь, а не только на экране подтверждения: между показом
    # и нажатием могла появиться тренировка.
    ready, dup, view_stats = await _prepare(user_id, data)
    skip = set() if force else dup
    to_import = [w for i, w in enumerate(ready) if i not in skip]

    if not to_import:
        # Импорт достижим и посреди незакрытой тренировки (через ⚙️ Настройки) —
        # каркас открытых упражнений не стираем.
        await clear_state_keep_workout(state)
        from handlers.workout import _show_main_menu
        await _show_main_menu(callback, state)
        await callback.answer(i18n.t("import.all_duplicate_alert"), show_alert=True)
        return

    await ui.safe_edit(callback, i18n.t("import.uploading"), reply_markup=None)
    result = await run_import(
        user_id, to_import, _decisions_from_state(data), detect_source(view_stats),
    )
    imported = result["imported"]
    if imported:
        # Разбор тренера — фоном, следом за итогом: экран не ждёт модель.
        _spawn(_attach_import_overview(
            callback.bot, callback.message.chat.id, user_id, result["batch_id"],
        ))

    await clear_state_keep_workout(state)
    alert = i18n.t("import.uploaded_alert", n=imported)
    if skip:
        # Пропуск озвучиваем в том же алерте: иначе «загрузил 5» вместо
        # ожидаемых 25 выглядит как потеря данных.
        alert += i18n.t("import.skipped_suffix", n=len(skip))
    if result["failed"]:
        alert += i18n.t("import.failed_suffix", n=result["failed"])
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer(alert, show_alert=True)
    if imported:
        # Итог отдельным сообщением под меню: что загрузил, что пропустил и
        # почему, какие значки открылись (одним списком, а не россыпью) — и
        # кнопка отменить весь этот импорт разом.
        text = _result_text(result, len(skip), view_stats)
        with suppress(TelegramBadRequest):
            await callback.message.answer(
                text, parse_mode="HTML",
                reply_markup=keyboards.import_result_keyboard(result["batch_id"]),
            )


def _result_text(result: dict, dup_count: int, stats: dict) -> str:
    lines = [i18n.t("import.result_header", n=result["imported"], sets=result["sets"])]
    if dup_count:
        lines.append(i18n.t("import.result_dupes", n=dup_count))
    lines.extend(_report_lines(stats))
    badges = achievements_summary(result["achievements"])
    if badges:
        shown = ", ".join(f"{b['emoji']} {escape(b['title'], quote=False)}" for b in badges[:_MAX_BADGES_SHOWN])
        if len(badges) > _MAX_BADGES_SHOWN:
            shown += i18n.t("import.result_badges_more", n=len(badges) - _MAX_BADGES_SHOWN)
        lines.append(i18n.t("import.result_badges", n=len(badges), list=shown))
    lines.append(i18n.t("import.result_undo_hint"))
    return "\n".join(lines)


_MAX_BADGES_SHOWN = 8


async def _attach_import_overview(bot, chat_id: int, user_id: int, batch_id: Optional[str] = None) -> None:
    """«Вижу два года жима, присед бросил в марте» — одно сообщение фоном,
    следом за итогом импорта. Ниже порога тренировок в ЭТОМ импорте —
    детерминированная реплика без обращения к модели, см.
    ai_trainer.import_history_overview."""
    try:
        overview = await ai_trainer.import_history_overview(user_id, batch_id=batch_id)
    except Exception:
        logger.exception("AI import overview failed for user %s", user_id)
        return
    if not overview:
        return
    with suppress(TelegramBadRequest):
        await bot.send_message(
            chat_id, formatting.ai_markdown_to_html(overview), parse_mode="HTML",
            reply_markup=keyboards.import_overview_cta_keyboard(),
        )


async def _return_to_origin(callback: CallbackQuery, state: FSMContext, origin: str) -> None:
    """Туда, откуда зашли в импорт: главное меню или ⚙️ Настройки."""
    if origin == "menu":
        from handlers.workout import _show_main_menu
        await _show_main_menu(callback, state)
        return
    from handlers.settings import show_settings
    await show_settings(callback, state)


@router.callback_query(F.data == "imp:cancel")
async def import_cancel(callback: CallbackQuery, state: FSMContext):
    # Откуда зашли — до сброса состояния, иначе clear_state_keep_workout
    # унесёт import_origin вместе со всем остальным.
    data = await state.get_data()
    origin = data.get("import_origin", "settings")
    # Отмена импорта не отменяет незакрытую тренировку, из которой сюда
    # зашли. И ничего не оставляет в базе: до «Загрузить» импорт ничего не
    # заводит (ни упражнений, ни групп).
    await clear_state_keep_workout(state)
    await callback.answer(i18n.t("import.cancelled_toast"))
    await _return_to_origin(callback, state, origin)


@router.callback_query(F.data == "imp:ok")
async def import_ok(callback: CallbackQuery, state: FSMContext):
    """«Понятно» на экране «всё уже есть» — выход туда, откуда пришли."""
    data = await state.get_data()
    origin = data.get("import_origin", "settings")
    await clear_state_keep_workout(state)
    await callback.answer()
    await _return_to_origin(callback, state, origin)


# ---------------------------------------------------------------------------
# Отмена импорта целиком.


@router.callback_query(F.data == "imp:batches")
async def import_batches(callback: CallbackQuery, state: FSMContext):
    batches = [b for b in await undoable_batches(callback.from_user.id) if b["can_undo"]]
    if not batches:
        await callback.answer(i18n.t("import.undo_nothing"), show_alert=True)
        return
    items = []
    for b in batches:
        when = formatting.format_date_ru(dt.datetime.fromisoformat(b["created_at"]))
        items.append((b["batch_id"], i18n.t(
            "import.batch_label", date=when, source=i18n.t(f"import.source.{b['source']}"), n=b["workouts"],
        )))
    await ui.safe_edit(
        callback, i18n.t("import.batches_header"),
        reply_markup=keyboards.import_batches_keyboard(items),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("imp:undo:"))
async def import_undo_ask(callback: CallbackQuery, state: FSMContext):
    batch_id = callback.data.split(":", 2)[2]
    batch = await db.get_import_batch(batch_id)
    if batch is None or batch["user_id"] != callback.from_user.id or batch["undone_at"] is not None:
        await callback.answer(i18n.t("import.undo_gone"), show_alert=True)
        return
    when = formatting.format_date_ru(dt.datetime.fromisoformat(batch["created_at"]))
    await callback.message.answer(
        i18n.t("import.undo_confirm", n=batch["workouts"], sets=batch["sets"], date=when),
        reply_markup=keyboards.import_undo_confirm_keyboard(batch_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("imp:undoyes:"))
async def import_undo_go(callback: CallbackQuery, state: FSMContext):
    batch_id = callback.data.split(":", 2)[2]
    result = await undo_import(callback.from_user.id, batch_id)
    if result is None:
        await callback.answer(i18n.t("import.undo_gone"), show_alert=True)
        return
    await ui.safe_edit(callback, undo_message(result), reply_markup=None)
    await callback.answer()


@router.callback_query(F.data == "imp:undono")
async def import_undo_no(callback: CallbackQuery, state: FSMContext):
    await ui.safe_edit(callback, i18n.t("import.undo_kept"), reply_markup=None)
    await callback.answer()


def undo_message(result: dict) -> str:
    """Что сняла отмена импорта — голосом тренера, на языке человека."""
    text = i18n.t(
        "import.undo_done", n=result["removed_workouts"], sets=result["removed_sets"],
    )
    if result["removed_exercises"]:
        text += " " + i18n.t("import.undo_done_exercises", n=result["removed_exercises"])
    return text


# ---------------------------------------------------------------------------
# Заметки: текст вместо файла на экране «Перенести историю».

# Текст короче этого и без единой цифры — не заметки с подходами, а реплика
# («привет», «а где взять файл?»): на неё — прежняя подсказка прислать файл.
_MIN_NOTES_CHARS = 8


def _looks_like_notes(text: str) -> bool:
    return len(text.strip()) >= _MIN_NOTES_CHARS and any(ch.isdigit() for ch in text)


@router.message(StateFilter(ImportFlow.awaiting_file), F.text)
async def import_notes_received(message: Message, state: FSMContext):
    """Заметки вставлены текстом — тот же разбор, что у приложения
    (/v1/import/text/convert): модель собирает строки «дата, упражнение,
    вес, повторы», дальше это обычный импорт CSV — предпросмотр, вопросы,
    дубли. Модель стоит денег и получает текст человека, поэтому сначала
    спрашиваем — как согласие в приложении."""
    text = message.text.strip()
    if not _looks_like_notes(text):
        await message.reply(i18n.t("import.file_missing"))
        return
    if len(text) > text_import.MAX_TEXT_CHARS:
        await message.reply(i18n.t("import.text_too_large"))
        return
    if not ai_trainer.is_configured():
        await message.reply(i18n.t("import.notes_unavailable"))
        return
    await state.update_data(imp_notes_text=text)
    await message.answer(
        i18n.t("import.notes_consent"), reply_markup=keyboards.import_notes_consent_keyboard(),
    )


@router.callback_query(StateFilter(ImportFlow.awaiting_file), F.data == "imp:notes:go")
async def import_notes_go(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = data.get("imp_notes_text")
    user_id = callback.from_user.id
    if not text:
        await callback.answer(i18n.t("import.notes_gone"), show_alert=True)
        return
    block = await ai_limits.hard_stop_block()
    if block is not None:
        await callback.answer()
        await ai_limits.reply(callback.message, block)
        return
    if user_id in _notes_converting:
        await callback.answer(i18n.t("import.already_uploading"))
        return
    _notes_converting.add(user_id)
    await callback.answer()
    await ui.safe_edit(callback, i18n.t("import.notes_progress"), reply_markup=None)
    user = await db.get_user(user_id)
    try:
        result = await text_import.extract_sets(
            user_id, text, timeutil.user_today(user), lang=user["lang"]
        )
    except Exception:
        logger.exception("notes import failed for user %s", user_id)
        await callback.message.answer(
            i18n.t("import.notes_failed"), reply_markup=keyboards.cancel_keyboard("imp:cancel"),
        )
        return
    finally:
        _notes_converting.discard(user_id)
    if not result.rows:
        key = "import.text_no_dates" if result.undated else "import.text_no_sets"
        await callback.message.answer(i18n.t(key), reply_markup=keyboards.cancel_keyboard("imp:cancel"))
        return
    csv_text = text_import.rows_to_csv(result.rows, user["unit"])
    headers, rows, has_header = _read_table(csv_text)
    seed = {"from_notes": True, "source": "notes"}
    for item in result.skipped:
        add_skip(seed, item["reason"], item.get("text"))
    if result.undated:
        add_skip(seed, "unparsed", None, count=result.undated)
    seed["warnings_extra"] = text_import.date_warnings(text, user["lang"])
    await state.update_data(
        imp_headers=headers, imp_rows=rows, imp_mapping=_auto_detect(headers),
        imp_has_header=has_header, imp_stats_seed=seed, imp_notes_text=None,
    )
    await _finish_mapping(_NotesEvent(callback), state)


_notes_converting: set[int] = set()


class _NotesEvent:
    """Колбэк кнопки «Разобрать» как событие для _finish_mapping: ответы
    уходят новым сообщением в чат, а не правкой экрана с «⏳ Разбираю»."""

    def __init__(self, callback: CallbackQuery):
        self.from_user = callback.from_user
        self._message = callback.message

    async def answer(self, text: str, **kwargs):
        return await self._message.answer(text, **kwargs)
