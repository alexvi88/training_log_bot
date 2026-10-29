"""Визитка программы картинкой — то, что бот шлёт фото на «Поделиться».

Та же картинка, что у приложения (`training_log_bot_ios`,
`Features/Export/ProgramShareCard.swift`, формат `.card`): угольный фон, слева
сверху чип лампой «Программа», справа — сколько дней, название крупно, по дню —
имя дня лампой капсом и упражнения рядами со схемой подходов справа, и подпись
приложения внизу (`FinishShareFrame(height: nil)` — высота по содержимому, а
не пустой уголь до 640). Ширина 360 точек, картинка ×3 — 1080 пикселей.

Картинку пересылают в мессенджер, и простыня никому не нужна: дней — не больше
`MAX_DAYS`, упражнений в дне — не больше `MAX_EXERCISES_PER_DAY`, остаток —
строкой «ещё N». Так высота ограничена сама собой (`MAX_HEIGHT_PT`).

Данные — только из снапшота визитки (`handlers.sharing.build_program_payload`):
на картинке ровно то, что получит человек по ссылке. Имена в снапшоте уже на
языке владельца; упражнения из каталога и схемы проходят через те же
`shown_exercise_name`/`shown_target`, что и текст визитки.

Слоя два, как у `finish_slides`: `assemble` (под языком владельца) собирает
все строки, `render` рисует только то, что ему дали, — поэтому всё, что
уезжает в PNG, видно тесту строками (`card_texts`). Рамка, подпись, чипы и
шрифты — общие с `finish_slides`.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Optional

import finish_slides as fs
import i18n

MAX_DAYS = 4
MAX_EXERCISES_PER_DAY = 5

# Отступы и кегли — из `ProgramShareCard.content`/`dayBlock`/`exerciseRow`.
_PAD_X = 16.0
_CONTENT_TOP = 50.0
_CONTENT_BOTTOM = 14.0
_TITLE_SIZE = 28.0
_TITLE_MIN_SCALE = 0.7
_TITLE_GAP = 10.0
_DAY_GAP = 12.0
_DAY_NAME_SIZE = 11.0
_ROW_NAME_SIZE = 13.0
_ROW_TARGET_SIZE = 12.0
_ROW_PAD = 6.0
_MORE_EX_SIZE = 11.0
_MORE_DAYS_SIZE = 12.0


@dataclasses.dataclass(frozen=True)
class CardRow:
    name: str
    target: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class CardDay:
    name: str
    rows: tuple[CardRow, ...]
    more: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class ProgramCard:
    chip: str
    size: Optional[str]
    title: str
    days: tuple[CardDay, ...]
    more_days: Optional[str]
    signature: fs.Signature
    lang: str = "ru"


# --- сборка (чистая, на текущем языке) --------------------------------------

_LEADING_NON_WORD = re.compile(r"^[^\w(]+")


def _drawable(text: str) -> str:
    """Без букв, которых нет в шрифте (эмодзи в имени): Pillow нарисовал бы
    пустые клетки. Лишние пробелы после выброшенного — схлопнуть."""
    missing = fs.missing_glyphs(text, bold=True) | fs.missing_glyphs(text, bold=False)
    if missing:
        text = "".join(ch for ch in text if ch not in missing)
    return " ".join(text.split())


def shown_title(name: str) -> str:
    """Название без эмодзи впереди — как `ProgramName.shown` у приложения
    («💪 Сплит на 3 дня» → «Сплит на 3 дня»)."""
    stripped = _drawable(_LEADING_NON_WORD.sub("", name).strip())
    return stripped or _drawable(name)


def assemble(payload: dict[str, Any]) -> ProgramCard:
    """Визитка из снапшота программы на текущем языке (вызывающий — в языке
    владельца: обработчик бота или `i18n.use_lang`). Ни базы, ни файлов."""
    # Импорт здесь: handlers.sharing тянет aiogram и роутер, а картинке нужны
    # только две функции показа — те же, что у текста визитки и у /v1.
    from handlers.sharing import program_days_totals, shown_exercise_name, shown_target

    in_card, total = program_days_totals(payload)
    days: list[CardDay] = []
    for day in payload["days"][:MAX_DAYS]:
        exercises = day["exercises"]
        rows = tuple(
            CardRow(
                name=_drawable(shown_exercise_name(ex["name"])),
                target=_drawable(shown_target(ex.get("target")) or "") or None,
            )
            for ex in exercises[:MAX_EXERCISES_PER_DAY]
        )
        hidden = len(exercises) - len(rows)
        days.append(CardDay(
            name=_drawable(day["name"]), rows=rows,
            more=i18n.t("program_card.more_exercises", n=hidden) if hidden > 0 else None,
        ))
    hidden_days = max(total, in_card) - len(days)
    return ProgramCard(
        chip=i18n.t("program_card.chip"),
        size=i18n.t("btn.program_days", n=max(total, in_card)),
        title=shown_title(payload["name"]),
        days=tuple(days),
        more_days=i18n.t("program_card.more_days", n=hidden_days) if hidden_days > 0 else None,
        signature=fs.current_signature(),
        lang=i18n.get_lang(),
    )


def card_texts(card: ProgramCard) -> list[str]:
    """Все строки, которые визитка кладёт в PNG, — для проверок языка и глифов."""
    sig = card.signature
    texts = [sig.brand, sig.tagline, sig.store_small, sig.store_big, card.chip, card.size or "",
             card.title, card.more_days or ""]
    for day in card.days:
        texts += [day.name, day.more or ""]
        for row in day.rows:
            texts += [row.name, row.target or ""]
    return [text for text in texts if text]


# --- раскладка и рендер -----------------------------------------------------


def _wrap_all(canvas, text: str, size: float, width: float) -> list[str]:
    """Перенос по словам без обрезки — чтобы понять, влезает ли кегль."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if not current or canvas.text_width(candidate, size) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _title_lines(canvas, title: str, width: float) -> tuple[float, list[str]]:
    """Две строки, не влезает — мельче до 0.7 (`lineLimit(2)` +
    `minimumScaleFactor(0.7)`), и только потом многоточие."""
    size = _TITLE_SIZE
    minimum = _TITLE_SIZE * _TITLE_MIN_SCALE
    while True:
        lines = _wrap_all(canvas, title, size, width)
        fits = len(lines) <= 2 and all(canvas.text_width(line, size) <= width for line in lines)
        if fits or size <= minimum:
            break
        size = max(minimum, size - 0.5)
    if not fits:
        lines = fs._wrap(canvas, title, size, width, 2)
    return size, lines


def _layout(card: ProgramCard, canvas, draw: bool) -> float:
    """Пройти визитку сверху вниз; `draw=False` — только померить. Вернёт низ
    содержимого (без нижнего поля и подписи)."""
    width = fs.CARD_W - 2 * _PAD_X
    x = _PAD_X
    if draw:
        fs._chips(canvas, card.chip, card.size)
    y = _CONTENT_TOP

    size, lines = _title_lines(canvas, card.title, width)
    for line in lines:
        if draw:
            canvas.text(x, y + canvas.metrics(size)[0], line, size, fs.WHITE)
        y += canvas.line_height(size)
    y += _TITLE_GAP

    row_h = _ROW_PAD + canvas.line_height(_ROW_NAME_SIZE) + _ROW_PAD
    for index, day in enumerate(card.days):
        if index:
            y += _DAY_GAP
        if day.name:
            label = fs._upper(day.name, card.lang)
            label = _ellipsize_kerned(canvas, label, _DAY_NAME_SIZE, width, kerning=1)
            if draw:
                canvas.text(x, y + canvas.metrics(_DAY_NAME_SIZE)[0], label, _DAY_NAME_SIZE, fs.LAMP,
                            kerning=1)
            y += canvas.line_height(_DAY_NAME_SIZE) + 2
        for r, row in enumerate(day.rows):
            if draw:
                _row(canvas, row, x, y, width, row_h)
                if r < len(day.rows) - 1 or day.more:
                    canvas.rect(x, y + row_h - 1, x + width, y + row_h, fs._rgba(fs.WHITE, 0.1))
            y += row_h
        if day.more:
            y += 4
            if draw:
                canvas.text(x, y + canvas.metrics(_MORE_EX_SIZE, False)[0], day.more, _MORE_EX_SIZE,
                            fs._rgba(fs.CHALK, 0.6), bold=False)
            y += canvas.line_height(_MORE_EX_SIZE, bold=False)
    if card.more_days:
        y += _DAY_GAP
        if draw:
            canvas.text(x, y + canvas.metrics(_MORE_DAYS_SIZE)[0], card.more_days, _MORE_DAYS_SIZE, fs.LAMP)
        y += canvas.line_height(_MORE_DAYS_SIZE)
    return y


def _ellipsize_kerned(canvas, text: str, size: float, width: float, kerning: float) -> str:
    if canvas.text_width(text, size, kerning=kerning) <= width:
        return text
    while text and canvas.text_width(text + "…", size, kerning=kerning) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _row(canvas, row: CardRow, x: float, top: float, width: float, row_h: float) -> None:
    """Ряд как в «Итоге»: название мелом слева, схема приглушённо справа по
    одной базовой линии (`firstTextBaseline`)."""
    baseline = top + _ROW_PAD + canvas.metrics(_ROW_NAME_SIZE)[0]
    right = x + width
    room = width
    if row.target:
        # Схема не сжимается (`fixedSize`), но и не съедает всю строку.
        target = fs._ellipsize(canvas, row.target, _ROW_TARGET_SIZE, width * 0.45, bold=False)
        tw = canvas.text_width(target, _ROW_TARGET_SIZE, bold=False)
        canvas.text(right - tw, baseline, target, _ROW_TARGET_SIZE, fs._rgba(fs.CHALK, 0.6), bold=False)
        room = width - tw - 10
    # Шрифт шире системного: сперва чуть мельче, потом многоточие.
    size = fs._fit_size(canvas, row.name, _ROW_NAME_SIZE, room, minimum=_ROW_NAME_SIZE * 0.85)
    canvas.text(x, baseline, fs._ellipsize(canvas, row.name, size, room), size, fs.CHALK)


def height_pt(card: ProgramCard) -> float:
    """Высота визитки в точках: содержимое, нижнее поле и подпись."""
    measure = fs._Canvas(card.lang, height=1)
    return round(_layout(card, measure, draw=False) + _CONTENT_BOTTOM + fs.SIGNATURE_H)


def render(card: ProgramCard, icon: Optional[str] = None) -> bytes:
    """PNG визитки: ширина 1080, высота по содержимому."""
    canvas = fs._Canvas(card.lang, height=height_pt(card))
    _layout(card, canvas, draw=True)
    fs._signature(canvas, card.signature, icon if icon is not None else fs.app_icon())
    return canvas.finish()
