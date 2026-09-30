"""Импорт истории из произвольного текста: заметки в телефоне, выгрузка чата,
переписанная тетрадь — во что угодно, где человек годами писал «жим 100 8».

Главный конкурент дневника — не другое приложение, а заметки: туда уходят
те, кто бросил трекер, и оттуда не забрать историю ни одним импортом (у Hevy,
например, импорт понимает только файл Strong). Формата у заметок нет, поэтому
разбирает их модель — но только до уровня строк «дата, упражнение, вес,
повторы». Дальше текст превращается в обычный CSV (`rows_to_csv`) и идёт тем
же путём, что файл Hevy/Strong: предпросмотр, дубли, сопоставление названий с
каталогом, потолки веса и повторов — всё из handlers/csv_import.py, второй
реализации импорта нет. Модель ничего не пишет в базу: её ответ — только
черновик CSV, который человек сначала видит в предпросмотре.

Текст режется на куски по строкам (`_chunks`): длинную историю одним вызовом
не разобрать — ответ упирается в max_tokens и обрезается посреди JSON. Куски
идут модели параллельно (`_PARALLEL`), иначе год заметок разбирался бы
минутами и упирался в таймаут запроса. Заголовок дня при этом мог остаться в
прошлом куске, а подходы под ним — уехать в следующий: модель отдаёт такие
подходы без даты, и дату им проставляет `_stitch_dates` уже после ответа — по
последнему датированному подходу выше по тексту.

Промпт — по-английски и без кириллицы намеренно: модуль в
i18n_coverage.LOCALIZED, а заметки на любом языке модель читает и так.

Порядок в числовой дате (`numeric_date_order`) зависит от языка атлета:
«03/12» у американца — 12 марта, у нас — 3 декабря, и сама одна такая дата
ничего не доказывает. Подсказка едет полем в сообщении пользователя, а не в
системном промпте: промпт — общий для всех кэшируемый префикс, и две его
версии по языку поделили бы кэш пополам.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import ai_trainer
import config
import i18n

logger = logging.getLogger(__name__)

# Потолок входа. Год заметок «жим 100 8» по три тренировки в неделю — порядка
# 20–30 тысяч символов; больше за раз не разбираем, чтобы один запрос не стоил
# как неделя вопросов тренеру. Кто пишет подробнее — пришлёт частями.
MAX_TEXT_CHARS = 30_000
# Кусок на один вызов модели. Ответ — примерно строка JSON на подход, и
# кусок такого размера с запасом укладывается в _MAX_OUTPUT_TOKENS.
CHUNK_CHARS = 5_000
_MAX_OUTPUT_TOKENS = 12_000
# Сколько кусков разбирается одновременно: весь потолок текста — два захода.
_PARALLEL = 3

_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "workout_sets_from_notes",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "sets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": ["string", "null"]},
                            "exercise": {"type": "string"},
                            "weight": {"type": "number"},
                            "unit": {"type": "string", "enum": ["kg", "lb", "unknown"]},
                            "reps": {"type": "integer"},
                        },
                        "required": ["date", "exercise", "weight", "unit", "reps"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["sets"],
            "additionalProperties": False,
        },
    },
}

_SYSTEM_PROMPT = """You convert a lifter's free-form training notes into a flat list of sets.

The notes can be in any language (very often Russian), copied from a phone notes app, a chat, a spreadsheet or a paper notebook. There is no fixed format. Typical shapes:
- a date line followed by exercises: "12.03" / "March 12" / "Mon 12/03/24", or a month name in the notes' language;
- "bench 100 8" or "bench 100x8" = one set of 8 reps with 100;
- "squat 100x5x3" or "squat 3x5 100" = three sets of 5 reps with 100;
- "100 8, 105 6, 110 4" or "100/8 105/6" = several sets of the same exercise;
- "pull-ups 10 10 8" with no weight = bodyweight sets, weight 0;
- "+20 x 8" on a bodyweight exercise = 20 extra weight;
- a set line without an exercise name continues the previous exercise.

Rules:
- Output one item per set, in the order the sets appear. Expand "NxM" set counts into N separate items.
- "date": ISO YYYY-MM-DD of the workout the set belongs to. A set inherits the most recent date above it. Sets that come before the first date in this piece get null: the piece may be cut out of longer notes, and their date is further up. Never invent a date that the notes do not imply.
- Dates without a year: choose the year so that the date is not after "today" and the notes stay in chronological order (notes usually go oldest to newest or newest to oldest — keep whichever the text shows). Numeric dates without a month name (03.04, 03/04, 03-04) follow "numeric_date_order" from the input: "day.month" or "month/day". Read them the other way only when the notes prove it, e.g. a number above 12 in the position of the month. A month written as a word ("Mar 12", "12 March") is never ambiguous.
- "exercise": the exercise name exactly as the lifter wrote it (fix only obvious typos and letter case), in the lifter's language. Do not translate, do not rename to a canonical name.
- "weight": the load as a number. "unit": "kg" or "lb" if the notes say so (kg, lb, lbs or the same words in the notes' language), otherwise "unknown".
- "reps": a positive integer.
- Skip anything that is not a set with reps: cardio, distances, times, planks in seconds, body measurements, food, comments, plans for the future, warm-up sets marked as warm-up.
- If the piece contains no sets at all, return {"sets": []}.
"""


@dataclass
class ExtractResult:
    rows: list[dict] = field(default_factory=list)
    # Подходы, для которых в тексте не нашлось даты: в CSV их не положить,
    # но человеку стоит сказать, что они были, а не терять их молча.
    undated: int = 0


def numeric_date_order(lang: Optional[str]) -> str:
    """Как по умолчанию читать «03/12» без названия месяца: по-английски —
    месяц/день (американский порядок), по-русски — день.месяц."""
    return "month/day" if i18n.normalize(lang) == "en" else "day.month"


def _chunks(text: str, limit: Optional[int] = None) -> list[str]:
    """Куски не длиннее limit (по умолчанию CHUNK_CHARS), разрезанные только
    по границам строк; строка длиннее limit сама по себе — отдельный кусок
    (резать посреди строки значит разорвать подход на вес и повторы)."""
    limit = limit or CHUNK_CHARS
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        if current and size + len(line) + 1 > limit:
            pieces.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current and any(s.strip() for s in current):
        pieces.append("\n".join(current))
    return [p for p in pieces if p.strip()]


def _valid_date(raw: Any, today: dt.date) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    try:
        day = dt.date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None
    if day > today:
        return None
    return day.isoformat()


def _clean_rows(raw_sets: Any, today: dt.date) -> tuple[list[dict], int]:
    """Ответ модели → строки с проверенными полями. Модель отвечает по схеме,
    но схема — не гарантия смысла: дата в будущем, отрицательный вес или
    пустое имя отбрасываются здесь, а вес/повторы за пределами потолков
    разбор CSV отклонит сам с номером строки. `date: None` остаётся — это
    подход до первой даты куска, его дату проставит `_stitch_dates`."""
    rows: list[dict] = []
    undated = 0
    if not isinstance(raw_sets, list):
        return rows, undated
    for item in raw_sets:
        if not isinstance(item, dict):
            continue
        name = item.get("exercise")
        weight = item.get("weight")
        reps = item.get("reps")
        if not isinstance(name, str) or not name.strip():
            continue
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0:
            continue
        if isinstance(reps, bool) or not isinstance(reps, int) or reps <= 0:
            continue
        raw_date = item.get("date")
        date = _valid_date(raw_date, today)
        if date is None and raw_date is not None:
            # Дата была, но битая или в будущем — не «унаследовать выше»,
            # а честно неизвестна.
            undated += 1
            continue
        unit = item.get("unit")
        rows.append({
            "date": date,
            "exercise": " ".join(name.split()),
            "weight": float(weight),
            "unit": unit if unit in ("kg", "lb") else None,
            "reps": reps,
        })
    return rows, undated


async def _extract_chunk(
    user_id: int, piece: str, today: dt.date, date_order: str
) -> tuple[list[dict], int]:
    client = ai_trainer._get_client()
    payload = {"today": today.isoformat(), "numeric_date_order": date_order, "notes": piece}
    response = await ai_trainer.paid_call(
        user_id,
        None,
        lambda: client.chat.completions.create(
            model=config.GROK_MODEL,
            max_tokens=_MAX_OUTPUT_TOKENS,
            extra_body={"reasoning_effort": config.GROK_QUICK_REASONING_EFFORT},
            response_format=_SCHEMA,
            messages=[
                # Промпт первым и неизменным: он одинаковый у всех кусков и
                # всех людей, и такой префикс попадает в кэш провайдера.
                # Всё, что зависит от атлета (сегодня, порядок дат), — только
                # в сообщении пользователя ниже.
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        ),
        source="ios",
    )
    try:
        data = ai_trainer._extract_json_object(response.choices[0].message.content or "")
    except (ValueError, IndexError, AttributeError):
        logger.warning("text import: unparsable model response for user %s", user_id)
        return [], 0
    return _clean_rows(data.get("sets") if isinstance(data, dict) else None, today)


def _stitch_dates(rows: list[dict]) -> tuple[list[dict], int]:
    """Подходы без даты (они шли в куске до первого заголовка дня) получают
    дату последнего датированного подхода выше по тексту. Выше ничего нет —
    это и есть подходы без даты, их только считаем."""
    stitched: list[dict] = []
    undated = 0
    carry: Optional[str] = None
    for row in rows:
        if row["date"] is None:
            if carry is None:
                undated += 1
                continue
            row = {**row, "date": carry}
        carry = row["date"]
        stitched.append(row)
    return stitched, undated


async def extract_sets(
    user_id: int, text: str, today: dt.date, lang: Optional[str] = None
) -> ExtractResult:
    """Текст заметок → подходы с датами. Кусок, на котором модель ответила
    мусором, просто ничего не добавляет; исключение сети/провайдера летит
    наверх — ручка отвечает на него 502, а не наполовину пустым импортом.
    `lang` — язык атлета (users.lang), от него порядок числовых дат; None —
    язык текущего запроса (i18n.get_lang() в момент вызова)."""
    gate = asyncio.Semaphore(_PARALLEL)
    date_order = numeric_date_order(lang if lang is not None else i18n.get_lang())

    async def one(piece: str) -> tuple[list[dict], int]:
        async with gate:
            return await _extract_chunk(user_id, piece, today, date_order)

    answers = await asyncio.gather(*(one(piece) for piece in _chunks(text)))
    rows = [row for chunk_rows, _ in answers for row in chunk_rows]
    stitched, undated = _stitch_dates(rows)
    return ExtractResult(rows=stitched, undated=undated + sum(bad for _, bad in answers))


def _format_weight(value: float) -> str:
    rounded = round(value, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def rows_to_csv(rows: list[dict], account_unit: str) -> str:
    """Подходы → CSV в формате нашего же экспорта: колонка веса без единицы
    читается разбором в единице аккаунта (handlers.csv_import._weight_factor),
    поэтому явно помеченные в заметках другие единицы пересчитываются здесь.
    Порядок строк — порядок дат, внутри дня — порядок записи: разбор CSV
    группирует строки по дате и сохраняет порядок упражнений как в файле."""
    ordered = sorted(enumerate(rows), key=lambda pair: (pair[1]["date"], pair[0]))
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["date", "exercise", "weight", "reps"])
    for _, row in ordered:
        weight = row["weight"]
        unit = row.get("unit")
        if unit == "lb" and account_unit == "kg":
            weight = weight / config.LB_PER_KG
        elif unit == "kg" and account_unit == "lb":
            weight = weight * config.LB_PER_KG
        writer.writerow([row["date"], row["exercise"], _format_weight(weight), row["reps"]])
    return buf.getvalue()
