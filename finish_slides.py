"""Слайды итогов тренировки — альбом сторис 9:16 после «Завершить» в боте.

Та же карусель, что у приложения на листе итогов
(`training_log_bot_ios`, `FinishShare/FinishShareCards.swift`,
`FinishBarbellSlide.swift`, `FinishShareCarousel.swift`): угольный фон, слева
сверху чип «что за слайд» лампой, справа дата, одна крупная цифра, под ней
строка фактов и подпись приложения внизу. Карточка 360×640 точек, картинка
×3 — 1080×1920.

Колода бота — первые карточки приложения, у которых есть данные:

1. Штанга с тоннажем — только если тоннаж больше нуля (одни подходы своим
   весом без веса тела — штанги нет). Шутки «это как N слонов» на слайде нет
   сознательно, как и в приложении: рядом с блинами она лишняя, а в тексте
   карточки над альбомом она уже есть.
2. Рекорды — по карточке на упражнение, не больше `MAX_RECORD_CARDS`, самый
   заметный рывок (прибавка относительно нового значения) первым. С весом —
   по e1RM, своим весом — по повторам: та же отметка 🔥, что в тексте
   карточки (`view_builder.build_block_views(mark_records=True)`), и так же
   молчит e1RM при выключенных доп. цифрах.
3. «Тренировка» — всегда, когда есть хоть один подход.
4. «Сегодня работали» — схема тела спереди и сзади (`body_map.py`, та же
   фигура, что у приложения): основные мышцы лампой, помогающие светлым
   кирпичом. Только когда в тренировке есть группа с зоной на схеме
   (`FinishShareDeck.groupNames`): заголовок называет только закрашенное.

Трофеев, «ТЫ» и тренера здесь нет: комментарий тренера бот и так
присылает текстом.

Без кадра упражнения — схема, а не буква, как у приложения: постер рекорда
берёт фигуру этого упражнения (`exerciseFigures`), ряд «Тренировки» — мини-
плитку с мышцей его группы (`GroupTilePlaceholder`). Буква — когда о мышцах
упражнения не известно ничего.

Разделение на два слоя — ради проверок. `assemble` (чистая, под
`i18n.use_lang`) собирает все строки и цифры колоды; `render_*` рисуют только
то, что им дали, и сами текстов не придумывают. Поэтому всё, что уезжает в
PNG, видно тесту строками (`slide_texts`) — кириллица у англоязычного, буквы
без глифа в шрифте, HTML-сущности. Рендер синхронный (Pillow) и уходит в
поток (`asyncio.to_thread`), как у `workout_card.py`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import functools
import math
import os
import random
import re
from io import BytesIO
from typing import Any, Optional, Sequence

import analytics
import body_map
import config
import db
import exercise_media
import exercise_muscles
import exercise_photos
import formatting
import i18n
import timeutil
import view_builder

# --- размеры и палитра (FinishShareLayout, FinishSharePalette, Theme) -------

CARD_W, CARD_H = 360, 640
SCALE = 3
WIDTH, HEIGHT = CARD_W * SCALE, CARD_H * SCALE
SIGNATURE_H = 54
CONTENT_H = CARD_H - SIGNATURE_H
# Рисуем вдвое крупнее и ужимаем: Pillow не сглаживает фигуры, а фаски
# чипов, блины и края кадра на 1080 иначе шли бы лесенкой.
_SS = 2
_K = SCALE * _SS

MAX_RECORD_CARDS = 3
SUMMARY_MAX_EXERCISES = 10

COAL = (27, 21, 18)
CHALK = (244, 236, 226)
LAMP = (233, 168, 61)
BRICK = (180, 71, 47)
BRICK_DEEP = (125, 46, 30)
WHITE = (255, 255, 255)
CONCRETE = (51, 48, 46)
# Схема тела на угле (`FinishSharePalette.body*`): тело тёмным бежем,
# основные мышцы лампой, помогающие — кирпичом светлее обычного.
BODY_BASE = (90, 78, 70)
BODY_PRIMARY = LAMP
BODY_SECONDARY = (194, 90, 60)
# Мини-плитка группы без кадра (`Theme.thumbFace/thumbFigure/thumbMuscle`):
# песок, фигура тоном темнее, мышцы группы кирпичом.
THUMB_FACE = (239, 225, 198)
THUMB_FIGURE = (216, 198, 166)
THUMB_MUSCLE = BRICK


def _rgba(rgb: tuple[int, int, int], alpha: float) -> tuple[int, int, int, int]:
    return rgb[0], rgb[1], rgb[2], round(255 * alpha)


# --- данные колоды ----------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Fact:
    """Факт под цифрой: число и подпись капсом под ним (прибавка — лампой)."""

    value: str
    label: str
    gain: bool = False


@dataclasses.dataclass(frozen=True)
class Signature:
    brand: str
    tagline: str
    store_small: str
    store_big: str = "App Store"


@dataclasses.dataclass(frozen=True)
class BarbellSlide:
    day: str
    title: str
    value: str
    unit: str
    lifted: str
    facts: tuple[Fact, ...]
    kilograms: float
    # Блин на грифе — в единице аккаунта: 50 KG или 100 LB.
    plate_weight: int
    plate_mark: str
    units_per_pair: float
    units_per_kg: float


@dataclasses.dataclass(frozen=True)
class RecordSlide:
    exercise_id: int
    chip: str
    day: str
    exercise_name: str
    value: str
    unit: str
    prefix: Optional[str]
    facts: tuple[Fact, ...]
    # Прибавка относительно нового значения — по ней рекорды встают в очередь.
    relative_gain: float
    poster: Optional[str] = None
    # Схема тела этого упражнения — постер без кадра; нет и её — буква.
    figure: Optional[body_map.Figure] = None


@dataclasses.dataclass(frozen=True)
class SummaryRow:
    name: str
    sets: str
    badge: Optional[str] = None
    thumb: Optional[str] = None
    # Зона группы (`body_map.REGIONS`) — мини-плитка с мышцей, когда кадра нет.
    region: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class SummarySlide:
    chip: str
    day: str
    value: str
    unit: str
    meta: str
    rows: tuple[SummaryRow, ...]
    more: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class BodySlide:
    chip: str
    day: str
    # Группы по порядку упражнений, без повторов, — только с зоной на схеме.
    groups: tuple[str, ...]
    figure: body_map.Figure
    facts: tuple[Fact, ...]

    @property
    def title(self) -> str:
        return " · ".join(self.groups)


Slide = BarbellSlide | RecordSlide | SummarySlide | BodySlide


@dataclasses.dataclass(frozen=True)
class Deck:
    barbell: Optional[BarbellSlide]
    records: tuple[RecordSlide, ...]
    summary: Optional[SummarySlide]
    signature: Signature
    lang: str = "ru"
    body: Optional[BodySlide] = None

    @property
    def record_cards(self) -> list[RecordSlide]:
        return ranked_records(self.records)[:MAX_RECORD_CARDS]

    @property
    def slides(self) -> list[Slide]:
        """Порядок колоды: штанга → рекорды → «Тренировка» → «Сегодня
        работали»; без данных — нет карточки (`FinishShareDeck.cards`
        приложения: трофеи, «ТЫ» и тренер бота не касаются)."""
        result: list[Slide] = []
        if self.barbell is not None:
            result.append(self.barbell)
        result.extend(self.record_cards)
        if self.summary is not None:
            result.append(self.summary)
        if self.body is not None and self.body.groups:
            result.append(self.body)
        return result


def current_signature() -> Signature:
    """Подпись приложения на текущем языке — общая у слайдов итогов и визитки
    программы (`program_card.py`)."""
    return Signature(
        brand=i18n.t("finish_slides.brand"),
        tagline=i18n.t("finish_slides.tagline"),
        store_small=i18n.t("finish_slides.store"),
    )


def ranked_records(records: Sequence[RecordSlide]) -> list[RecordSlide]:
    """По убыванию относительной прибавки; при равной — по порядку
    упражнений (`FinishShareDeck.rankedRecords`)."""
    indexed = list(enumerate(records))
    indexed.sort(key=lambda item: (-item[1].relative_gain, item[0]))
    return [record for _index, record in indexed]


# --- сборка колоды (чистая, под i18n.use_lang) ------------------------------

_NUMBER_RE = re.compile(r"^[0-9]+(?:[.,   ][0-9]+)*")


def split_number(text: str) -> tuple[str, str]:
    """«2.9 тонны» → («2.9», «тонны»), «576кг» → («576», «кг»): число крупно,
    единица мельче (`FinishShareBigNumber.split`)."""
    match = _NUMBER_RE.match(text)
    if match is None:
        return text, ""
    return match.group(0), text[match.end():].strip()


def _field(row: Any, key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value


def _collapsed_sets(block: formatting.ExerciseBlockView) -> str:
    """Подряд идущие одинаковые подходы — «100×8 ×3», как у приложения
    (`WorkoutShareCard.collapsedSets`) и `formatting.format_set` бота."""
    parts: list[str] = []
    current, run = "", 0
    for index, (weight, reps) in enumerate(block.sets):
        text = formatting.format_set(weight, reps, block.rpe_for(index))
        if text == current:
            run += 1
            continue
        if current:
            parts.append(f"{current} ×{run}" if run > 1 else current)
        current, run = text, 1
    if current:
        parts.append(f"{current} ×{run}" if run > 1 else current)
    return ", ".join(parts) or "—"


def _top_set(block: formatting.ExerciseBlockView) -> Optional[str]:
    """Подход, которым поставлен рекорд e1RM: тот, что дал лучший e1RM, — как
    его записал человек («70×5»)."""
    best_index, best = None, 0.0
    for index, (_weight, reps) in enumerate(block.sets):
        value = analytics.e1rm(block.load_for(index), reps, block.formula, block.rpe_for(index))
        if value > best:
            best_index, best = index, value
    if best_index is None:
        return None
    weight, reps = block.sets[best_index]
    return formatting.format_set(weight, reps)


def _record(
    block: formatting.ExerciseBlockView, unit: str, show_extra: bool, chip: str, day: str,
    poster: Optional[str],
) -> Optional[RecordSlide]:
    """Карточка рекорда блока, или None. Своим весом — повторы и «+N» к
    прошлому лучшему; с весом — e1RM, «+Xкг» и подход. e1RM при выключенных
    доп. цифрах молчит, как и строка 🔥 в тексте (`format_block_record`)."""
    if block.record_reps is not None:
        value = block.record_reps
        delta = value - (block.record_reps_prev or 0)
        if delta <= 0:
            return None
        return RecordSlide(
            exercise_id=block.exercise_id or 0, chip=chip, day=day,
            exercise_name=block.exercise_name, value=str(value),
            unit=i18n.t("finish_slides.reps", n=value), prefix=None,
            facts=(Fact(f"+{delta}", i18n.t("finish_slides.fact.gain"), gain=True),),
            relative_gain=delta / value, poster=poster,
        )
    delta = block.record_e1rm_delta
    if delta is None or not show_extra:
        return None
    value = block.top_e1rm
    delta = round(delta, 1)
    if value <= 0 or delta <= 0:
        return None
    u = formatting.unit_label(unit)
    facts = [Fact(f"+{formatting.format_weight(delta)}{u}", i18n.t("finish_slides.fact.gain"), gain=True)]
    top = _top_set(block)
    if top:
        facts.append(Fact(top, i18n.t("finish_slides.fact.set")))
    return RecordSlide(
        exercise_id=block.exercise_id or 0, chip=chip, day=day,
        exercise_name=block.exercise_name, value=str(round(value)), unit=u,
        prefix="e1RM",  # l10n: термин, одинаковый на всех языках
        facts=tuple(facts), relative_gain=delta / value, poster=poster,
    )


def _record_badge(block: formatting.ExerciseBlockView, unit: str, show_extra: bool) -> Optional[str]:
    """Плашка рекорда в ряду «Тренировки»: прибавка e1RM числом, рекорд
    повторов — звёздочкой (`WorkoutShareCard.recordBadge`)."""
    if block.record_reps is not None:
        return "★"  # l10n: значок, не текст
    if block.record_e1rm_delta is not None and show_extra and block.record_e1rm_delta > 0:
        return f"+{formatting.format_weight(round(block.record_e1rm_delta, 1))}{formatting.unit_label(unit)}"
    return None


def assemble(
    blocks: Sequence[formatting.BlockView],
    user: Any,
    *,
    started_at: dt.datetime,
    duration_seconds: Optional[float],
    posters: Optional[dict[int, str]] = None,
    thumbs: Optional[dict[int, str]] = None,
    muscles: Optional[dict[int, body_map.Muscles]] = None,
) -> Deck:
    """Колода слайдов из блоков тренировки — на текущем языке (вызывающий
    оборачивает в `i18n.use_lang`). Ни базы, ни файлов: только строки.
    `muscles` — разметка мышц каталога по id упражнения
    (`exercise_muscles.for_exercise`); нет записи — красит зона группы."""
    posters = posters or {}
    thumbs = thumbs or {}
    muscles = muscles or {}
    unit = _field(user, "unit", "kg")
    show_extra = bool(_field(user, "show_extra_stats", 1))
    exercises = [b for b in blocks if isinstance(b, formatting.ExerciseBlockView) and b.sets]
    signature = current_signature()
    lang = i18n.get_lang()
    if not exercises:
        return Deck(None, (), None, signature, lang)

    day = formatting.format_date_short(timeutil.to_user_local(started_at, user))
    sets_count = sum(len(b.sets) for b in exercises)
    exercise_count = len({b.exercise_id for b in exercises})
    tonnage = sum(b.load_tonnage for b in exercises)
    duration = formatting.format_duration(duration_seconds) if duration_seconds else None

    count_facts: list[Fact] = [
        Fact(str(exercise_count), i18n.t("finish_slides.fact.exercises", n=exercise_count)),
        Fact(str(sets_count), i18n.t("finish_slides.fact.sets", n=sets_count)),
    ]
    if duration:
        count_facts.append(Fact(duration, i18n.t("finish_slides.fact.time")))

    tonnage_text = formatting.format_tonnage(tonnage, unit) if tonnage > 0 else ""
    barbell = None
    if tonnage > 0:
        value, unit_word = split_number(tonnage_text)
        lifted_key = (
            "finish_slides.lifted_female" if _field(user, "gender") == "female" else "finish_slides.lifted"
        )
        is_lb = unit == "lb"
        barbell = BarbellSlide(
            day=day, title=i18n.t("finish_slides.title"), value=value, unit=unit_word,
            lifted=i18n.t(lifted_key), facts=tuple(count_facts),
            kilograms=formatting.to_kg(tonnage, unit),
            plate_weight=100 if is_lb else 50,
            plate_mark="LB" if is_lb else "KG",  # l10n: маркировка на железном блине
            units_per_pair=200.0 if is_lb else 100.0,
            units_per_kg=config.LB_PER_KG if is_lb else 1.0,
        )

    chip = i18n.t("finish_slides.chip.record")
    records = tuple(
        dataclasses.replace(record, figure=None if record.poster else body_map.figure(
            [(muscles.get(block.exercise_id or 0), body_map.group_key(block.group_name))]
        ))
        for block in exercises
        if (record := _record(block, unit, show_extra, chip, day, posters.get(block.exercise_id or 0)))
    )

    # Цифра «Тренировки»: тоннаж, а без него — подходы (`FinishSummaryCard.headline`).
    meta = [f"{exercise_count} {i18n.t('finish_slides.fact.exercises', n=exercise_count)}"]
    if tonnage > 0:
        big_value, big_unit = split_number(tonnage_text)
        meta.append(f"{sets_count} {i18n.t('finish_slides.fact.sets', n=sets_count)}")
    else:
        big_value, big_unit = str(sets_count), i18n.t("finish_slides.fact.sets", n=sets_count)
    if duration:
        meta.append(duration)
    rows = tuple(
        SummaryRow(
            name=block.exercise_name,
            sets=_collapsed_sets(block),
            badge=_record_badge(block, unit, show_extra),
            thumb=thumbs.get(block.exercise_id or 0),
            region=body_map.group_key(block.group_name),
        )
        for block in exercises[:SUMMARY_MAX_EXERCISES]
    )
    hidden = len(exercises) - len(rows)
    summary = SummarySlide(
        chip=i18n.t("finish_slides.chip.workout"), day=day, value=big_value, unit=big_unit,
        meta=" · ".join(meta), rows=rows,
        more=i18n.t("finish_slides.more", n=hidden) if hidden > 0 else None,
    )
    body = _body_slide(exercises, muscles, day, sets_count, tonnage_text)
    return Deck(barbell, records, summary, signature, lang, body)


def _group_title(group_name: str) -> str:
    """Название встроенной группы на текущем языке — «Грудь» у русского,
    «Chest» у англоязычного, как бы группа ни звалась в базе."""
    import seed_data

    canonical = seed_data.canonical_muscle_group_name(group_name) or group_name
    return seed_data.localized_muscle_group_name(canonical, i18n.get_lang())


def _body_slide(
    exercises: Sequence[formatting.ExerciseBlockView], muscles: dict[int, body_map.Muscles],
    day: str, sets_count: int, tonnage_text: str,
) -> Optional[BodySlide]:
    """«Сегодня работали» — или None. Заголовок называет только группы с зоной
    на схеме: упражнение такой группы красит фигуру всегда (своей разметкой
    или зоной группы), так что названо только закрашенное. Упражнение с
    разметкой, но в своей группе, красит, а в заголовок не попадает
    (`FinishShareDeck.groupNames`)."""
    figure = body_map.figure(
        (muscles.get(block.exercise_id or 0), body_map.group_key(block.group_name)) for block in exercises
    )
    seen: set[str] = set()
    groups: list[str] = []
    for block in exercises:
        key = body_map.group_key(block.group_name)
        if key is None or key in seen:
            continue
        seen.add(key)
        groups.append(_group_title(block.group_name))
    if figure is None or not groups:
        return None
    facts = [
        Fact(str(len(groups)), i18n.t("finish_slides.fact.groups", n=len(groups))),
        Fact(str(sets_count), i18n.t("finish_slides.fact.sets", n=sets_count)),
    ]
    if tonnage_text:
        facts.append(Fact(tonnage_text, i18n.t("finish_slides.fact.tonnage")))
    return BodySlide(
        chip=i18n.t("finish_slides.chip.body"), day=day, groups=tuple(groups), figure=figure,
        facts=tuple(facts),
    )


def slide_texts(slide: Slide, signature: Signature) -> list[str]:
    """Все строки, которые слайд кладёт в PNG, — для проверок языка и глифов."""
    texts = [signature.brand, signature.tagline, signature.store_small, signature.store_big]
    if isinstance(slide, BarbellSlide):
        texts += [slide.day, slide.title, slide.value, slide.unit, slide.lifted, slide.plate_mark,
                  str(slide.plate_weight)]
    elif isinstance(slide, RecordSlide):
        texts += [slide.chip, slide.day, slide.exercise_name, slide.value, slide.unit, slide.prefix or ""]
    elif isinstance(slide, BodySlide):
        texts += [slide.chip, slide.day, slide.title]
    else:
        texts += [slide.chip, slide.day, slide.value, slide.unit, slide.meta, slide.more or ""]
        for row in slide.rows:
            texts += [row.name, row.sets, row.badge or ""]
    for fact in getattr(slide, "facts", ()):
        texts += [fact.value, fact.label]
    return [text for text in texts if text]


# --- шрифты -----------------------------------------------------------------


def font_path(bold: bool = True) -> str:
    """DejaVu едет вместе с matplotlib — тот же шрифт, что у графиков и
    картинок сайта (`seo_static._font`): кириллица, латиница с диакритикой и
    турецкий на месте, на сервере ничего ставить не нужно."""
    import matplotlib

    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return os.path.join(matplotlib.get_data_path(), "fonts", "ttf", name)


@functools.lru_cache(maxsize=2)
def _codepoints(bold: bool) -> frozenset[int]:
    from fontTools.ttLib import TTFont

    return frozenset(TTFont(font_path(bold)).getBestCmap())


def missing_glyphs(text: str, bold: bool = True) -> set[str]:
    """Буквы, которых нет в шрифте, — Pillow нарисовал бы их пустыми клетками."""
    cmap = _codepoints(bold)
    return {ch for ch in text if not ch.isspace() and ord(ch) not in cmap}


@functools.lru_cache(maxsize=128)
def _font(size_px: int, bold: bool):
    from PIL import ImageFont

    return ImageFont.truetype(font_path(bold), max(size_px, 1))


def _upper(text: str, lang: str) -> str:
    """Капс по языку: турецкая i — «İ», а не «I»
    (`uppercased(with: AppLanguage.locale)` у приложения)."""
    if lang == "tr":
        text = text.replace("i", "İ")
    return text.upper()


# --- холст ------------------------------------------------------------------


class _Canvas:
    """Холст в точках карточки: все координаты — pt, как в SwiftUI-макете."""

    def __init__(self, lang: str, height: float = CARD_H):
        from PIL import Image, ImageDraw

        self.lang = lang
        # Высота в точках: у слайдов — кадр 640, у визитки программы
        # (`program_card.py`) — по содержимому (`FinishShareFrame(height: nil)`).
        self.height = height
        self.image = Image.new("RGB", (CARD_W * _K, self.px(height)), COAL)
        self.draw = ImageDraw.Draw(self.image, "RGBA")

    @staticmethod
    def px(value: float) -> int:
        return round(value * _K)

    def font(self, size: float, bold: bool = True):
        return _font(round(size * _K), bold)

    def metrics(self, size: float, bold: bool = True) -> tuple[float, float]:
        ascent, descent = self.font(size, bold).getmetrics()
        return ascent / _K, descent / _K

    def line_height(self, size: float, bold: bool = True) -> float:
        ascent, descent = self.metrics(size, bold)
        return ascent + descent

    def text_width(self, text: str, size: float, bold: bool = True, kerning: float = 0) -> float:
        font = self.font(size, bold)
        if not kerning:
            return font.getlength(text) / _K
        return sum(font.getlength(ch) / _K + kerning for ch in text) - (kerning if text else 0)

    def text(
        self, x: float, baseline: float, text: str, size: float, fill, bold: bool = True,
        kerning: float = 0, draw=None,
    ) -> float:
        """Строка от левого края по базовой линии; вернёт ширину в pt."""
        draw = draw or self.draw
        font = self.font(size, bold)
        if not kerning:
            draw.text((self.px(x), self.px(baseline)), text, font=font, fill=fill, anchor="ls")
            return font.getlength(text) / _K
        cursor = x
        for ch in text:
            draw.text((self.px(cursor), self.px(baseline)), ch, font=font, fill=fill, anchor="ls")
            cursor += font.getlength(ch) / _K + kerning
        return cursor - x - (kerning if text else 0)

    def shadowed_text(
        self, x: float, baseline: float, text: str, size: float, fill, *, kerning: float = 0,
        radius: float = 6, dy: float = 2, alpha: float = 0.55,
    ) -> float:
        """Белая цифра на кирпиче с мягкой тёмной тенью (`.shadow` приложения)."""
        from PIL import Image, ImageDraw, ImageFilter

        # Тень рисуется в своём окне вокруг строки, а не на весь холст:
        # размытие 2160×3840 на каждую строку стоило секунды на слайд.
        ascent, descent = self.metrics(size)
        pad = radius * 3
        x0, y0 = x - pad, baseline - ascent + dy - pad
        width = self.text_width(text, size, kerning=kerning) + 2 * pad
        height = ascent + descent + 2 * pad
        origin = (self.px(x0), self.px(y0))
        mask = Image.new("L", (self.px(width), self.px(height)), 0)
        self.text(pad, pad + ascent, text, size, 255, kerning=kerning, draw=ImageDraw.Draw(mask))
        mask = mask.filter(ImageFilter.GaussianBlur(radius * _K / 2))
        shadow = Image.new("RGB", mask.size, (0, 0, 0))
        self.image.paste(shadow, origin, mask.point(lambda v: round(v * alpha)))
        return self.text(x, baseline, text, size, fill, kerning=kerning)

    def rect(self, x0: float, y0: float, x1: float, y1: float, fill) -> None:
        self.draw.rectangle((self.px(x0), self.px(y0), self.px(x1) - 1, self.px(y1) - 1), fill=fill)

    def rounded(self, x0, y0, x1, y1, radius, fill=None, outline=None, width: float = 0) -> None:
        self.draw.rounded_rectangle(
            (self.px(x0), self.px(y0), self.px(x1), self.px(y1)), radius=self.px(radius),
            fill=fill, outline=outline, width=self.px(width) if width else 0,
        )

    def vgradient(self, x0, y0, x1, y1, stops: Sequence[tuple[float, tuple[int, int, int, int]]]) -> None:
        """Вертикальный градиент по остановкам (доля высоты, RGBA) поверх холста."""
        from PIL import Image

        h = max(self.px(y1) - self.px(y0), 1)
        w = max(self.px(x1) - self.px(x0), 1)
        column = Image.new("RGBA", (1, h))
        pixels = column.load()
        for row in range(h):
            t = row / max(h - 1, 1)
            pixels[0, row] = _interpolate(stops, t)
        layer = column.resize((w, h))
        self.image.paste(layer, (self.px(x0), self.px(y0)), layer)

    def paste_cover(self, path: str, x0: float, y0: float, x1: float, y1: float) -> bool:
        """Кадр «по заполнению»: растянут до краёв, лишнее обрезано по центру."""
        from PIL import Image, ImageOps

        try:
            with Image.open(path) as source:
                source = ImageOps.exif_transpose(source).convert("RGB")
                w, h = self.px(x1) - self.px(x0), self.px(y1) - self.px(y0)
                fitted = ImageOps.fit(source, (w, h), Image.LANCZOS)
        except Exception:
            return False
        self.image.paste(fitted, (self.px(x0), self.px(y0)))
        return True

    def finish(self) -> bytes:
        from PIL import Image

        out = self.image.resize((WIDTH, round(self.height * SCALE)), Image.LANCZOS)
        buffer = BytesIO()
        out.save(buffer, format="PNG")
        return buffer.getvalue()


def _interpolate(stops, t: float) -> tuple[int, int, int, int]:
    if t <= stops[0][0]:
        return stops[0][1]
    for (t0, c0), (t1, c1) in zip(stops, stops[1:], strict=False):
        if t <= t1:
            f = 0 if t1 == t0 else (t - t0) / (t1 - t0)
            return tuple(round(a + (b - a) * f) for a, b in zip(c0, c1, strict=True))  # type: ignore[return-value]
    return stops[-1][1]


# --- общие детали (FinishShareSignature, Chips, BigNumber, FactsRow) --------


def _brick(canvas: _Canvas, x0: float, y0: float, x1: float, y1: float, seed: str) -> None:
    """Кирпич с зерном (`BrickFace(grain: true)`): глина сверху вниз, обжиг,
    песчаные крапины, свет сверху и тень снизу."""
    canvas.vgradient(x0, y0, x1, y1, [(0, BRICK + (255,)), (1, BRICK_DEEP + (255,))])
    rng = random.Random(seed)
    shade = rng.uniform(-0.06, 0.06)
    warmth = rng.uniform(0, 0.07)
    canvas.rect(x0, y0, x1, y1, _rgba((0, 0, 0) if shade < 0 else WHITE, abs(shade)))
    canvas.rect(x0, y0, x1, y1, _rgba(LAMP, warmth))
    specks = int(150 * (x1 - x0) * (y1 - y0) / (CARD_W * 200)) + 150
    for _ in range(specks):
        x = rng.uniform(x0, x1)
        y = rng.uniform(y0, y1)
        side = rng.uniform(0.7, 1.8)
        light = rng.random() > 0.35
        alpha = rng.uniform(0.04, 0.12)
        canvas.rect(x, y, x + side, y + side, _rgba(WHITE if light else (0, 0, 0), alpha))
    h = y1 - y0
    canvas.vgradient(x0, y0, x1, y0 + h * 0.4, [(0, _rgba(WHITE, 0.17)), (1, _rgba(WHITE, 0))])
    canvas.vgradient(x0, y0 + h * 0.55, x1, y1, [(0, (0, 0, 0, 0)), (1, (0, 0, 0, 51))])


def _fade(canvas: _Canvas, x0: float, y0: float, x1: float, y1: float) -> None:
    """Уход в уголь снизу — у кадра и буквы одинаковый (`FinishShareFade`)."""
    canvas.vgradient(x0, y0, x1, y1, [
        (0.45, _rgba(COAL, 0)), (0.85, _rgba(COAL, 0.85)), (1, _rgba(COAL, 1)),
    ])


def _apple_logo(canvas: _Canvas, cx: float, cy: float, size: float) -> None:
    """Яблоко плашки App Store — фигурой: в DejaVu глифа нет."""
    r = size * 0.27
    d = canvas.draw
    p = canvas.px
    body_top = cy - size * 0.22
    d.ellipse((p(cx - r * 1.75), p(body_top), p(cx + r * 0.2), p(body_top + r * 2.6)), fill=WHITE)
    d.ellipse((p(cx - r * 0.2), p(body_top), p(cx + r * 1.75), p(body_top + r * 2.6)), fill=WHITE)
    d.ellipse((p(cx - r * 1.2), p(body_top + r * 1.2), p(cx + r * 1.2), p(body_top + r * 3.0)), fill=WHITE)
    # Укус справа и ямка сверху — цветом плашки.
    d.ellipse((p(cx + r * 1.15), p(body_top + r * 0.35), p(cx + r * 2.35), p(body_top + r * 1.55)), fill=(0, 0, 0))
    d.ellipse((p(cx - r * 0.28), p(body_top - r * 0.35), p(cx + r * 0.28), p(body_top + r * 0.2)), fill=(0, 0, 0))
    # Листик.
    leaf = [(cx + r * 0.05, body_top - r * 0.15), (cx + r * 0.9, body_top - r * 1.15),
            (cx + r * 0.55, body_top - r * 0.2)]
    d.polygon([(p(x), p(y)) for x, y in leaf], fill=WHITE)


def _signature(canvas: _Canvas, signature: Signature, icon: Optional[str]) -> None:
    """Подпись приложения по низу холста — у кадра 640 и у визитки любой высоты."""
    top = canvas.height - SIGNATURE_H
    canvas.vgradient(0, top, CARD_W, canvas.height, [(0, (59, 56, 54, 255)), (1, (41, 38, 38, 255))])
    canvas.rect(0, top, CARD_W, top + 2, _rgba(WHITE, 0.1))
    mid = top + SIGNATURE_H / 2
    x = 14.0
    if icon:
        from PIL import Image

        try:
            with Image.open(icon) as source:
                size = canvas.px(34)
                tile = source.convert("RGBA").resize((size, size), Image.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            from PIL import ImageDraw

            ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=canvas.px(8), fill=255)
            alpha = Image.composite(tile.getchannel("A"), Image.new("L", (size, size), 0), mask)
            canvas.image.paste(tile.convert("RGB"), (canvas.px(x), canvas.px(mid - 17)), alpha)
            x += 34 + 10
        except Exception:
            pass
    # Плашка App Store справа.
    small_h = canvas.line_height(7.5, bold=False)
    big_h = canvas.line_height(12)
    badge_inner_w = 15 + 5 + max(
        canvas.text_width(signature.store_small, 7.5, bold=False), canvas.text_width(signature.store_big, 12)
    )
    badge_w = 7 + badge_inner_w + 9
    badge_h = 4 + small_h + big_h + 4
    bx1 = CARD_W - 14
    bx0 = bx1 - badge_w
    by0 = mid - badge_h / 2
    canvas.rounded(bx0, by0, bx1, by0 + badge_h, 7, fill=(0, 0, 0), outline=_rgba(WHITE, 0.35), width=1)
    _apple_logo(canvas, bx0 + 7 + 7.5, mid, 15)
    tx = bx0 + 7 + 15 + 5
    small_asc, _ = canvas.metrics(7.5, bold=False)
    big_asc, _ = canvas.metrics(12)
    canvas.text(tx, by0 + 4 + small_asc, signature.store_small, 7.5, WHITE, bold=False)
    canvas.text(tx, by0 + 4 + small_h + big_asc, signature.store_big, 12, WHITE)
    # Имя и чем полезно — в оставшееся место.
    room = bx0 - 6 - x
    brand_size = _fit_size(canvas, signature.brand, 13, room, minimum=9)
    tag_size = _fit_size(canvas, signature.tagline, 10.5, room, minimum=7.5, bold=False)
    h1 = canvas.line_height(brand_size)
    h2 = canvas.line_height(tag_size, bold=False)
    y = mid - (h1 + 1 + h2) / 2
    canvas.text(x, y + canvas.metrics(brand_size)[0], signature.brand, brand_size, WHITE)
    canvas.text(x, y + h1 + 1 + canvas.metrics(tag_size, False)[0], signature.tagline, tag_size,
                _rgba(WHITE, 0.65), bold=False)


def _fit_size(canvas: _Canvas, text: str, size: float, width: float, *, minimum: float,
              bold: bool = True, kerning: float = 0) -> float:
    """Ужать шрифт, пока строка не влезет (`minimumScaleFactor`)."""
    while size > minimum and canvas.text_width(text, size, bold, kerning) > width:
        size = max(minimum, size - 0.25)
    return size


def _ellipsize(canvas: _Canvas, text: str, size: float, width: float, bold: bool = True) -> str:
    if canvas.text_width(text, size, bold) <= width:
        return text
    while text and canvas.text_width(text + "…", size, bold) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _star(canvas: _Canvas, cx: float, cy: float, r: float, fill) -> None:
    points = []
    for i in range(10):
        radius = r if i % 2 == 0 else r * 0.45
        angle = -math.pi / 2 + i * math.pi / 5
        points.append((canvas.px(cx + radius * math.cos(angle)), canvas.px(cy + radius * math.sin(angle))))
    canvas.draw.polygon(points, fill=fill)


def _chip(canvas: _Canvas, x: float, y: float, text: str, *, fill, ink, star: bool = False,
          align_right: bool = False, backing: bool = False) -> float:
    """Чип: капс 10.5 heavy с разрядкой 1.4, поля 9×5, скругление 4.
    Вернёт низ чипа."""
    label = _upper(text, canvas.lang)
    size = 10.5
    width = canvas.text_width(label, size, kerning=1.4) + (9 + 5 if star else 0)
    height = 5 + canvas.line_height(size) + 5
    x0 = x - (width + 18) if align_right else x
    if backing:
        # Бледный чип даты на светлом кадре упражнения не читался: под ним
        # полупрозрачный уголь (на угольном фоне его не видно вовсе).
        canvas.rounded(x0, y, x0 + width + 18, y + height, 4, fill=_rgba(COAL, 0.45))
    canvas.rounded(x0, y, x0 + width + 18, y + height, 4, fill=fill)
    tx = x0 + 9
    if star:
        _star(canvas, tx + 4.5, y + height / 2, 4.8, ink)
        tx += 9 + 5
    canvas.text(tx, y + 5 + canvas.metrics(size)[0], label, size, ink, kerning=1.4)
    return y + height


def _chips(canvas: _Canvas, title: Optional[str], day: Optional[str], star: bool = False) -> float:
    bottom = 16.0
    if title:
        bottom = _chip(canvas, 16, 16, title, fill=LAMP, ink=COAL, star=star)
    if day:
        bottom = max(bottom, _chip(canvas, CARD_W - 16, 16, day, fill=_rgba(CHALK, 0.14), ink=CHALK,
                                   align_right=True, backing=True))
    return bottom


def _big_number(
    canvas: _Canvas, x: float, baseline: float, value: str, unit: str, *, size: float = 88,
    prefix: Optional[str] = None, max_width: float = CARD_W - 32, shadow: bool = False,
) -> None:
    """Главная цифра: число black с разрядкой −1.5, единица рядом мельче,
    префикс («e1RM») перед ним; не влезает — ужимается целиком до половины."""

    def widths(scale: float) -> float:
        total = canvas.text_width(value, size * scale, kerning=-1.5 * scale)
        if prefix:
            total += canvas.text_width(prefix, size * 0.28 * scale) + 5
        if unit:
            total += 5 + canvas.text_width(unit, size * 0.32 * scale)
        return total

    scale = 1.0
    while scale > 0.5 and widths(scale) > max_width:
        scale -= 0.02
    s = size * scale
    cursor = x
    if prefix:
        cursor += canvas.text(cursor, baseline, prefix, s * 0.28, _rgba(WHITE, 0.8)) + 5
    draw_text = canvas.shadowed_text if shadow else canvas.text
    cursor += draw_text(cursor, baseline, value, s, WHITE, kerning=-1.5 * scale)
    if unit:
        cursor += 5
        draw_text(cursor, baseline, unit, s * 0.32, WHITE)


def _facts_height(canvas: _Canvas) -> float:
    return 9 + canvas.line_height(16) + 2 + canvas.line_height(9.5, bold=False) + 9


def _facts_row(canvas: _Canvas, facts: Sequence[Fact], top: float) -> None:
    """До трёх ячеек поровну, тонкие линии между ними и сверху."""
    if not facts:
        return
    rule = _rgba(WHITE, 0.14)
    height = _facts_height(canvas)
    canvas.rect(0, top, CARD_W, top + 1, rule)
    cell = CARD_W / len(facts)
    for index, fact in enumerate(facts):
        x0 = cell * index
        if index > 0:
            canvas.rect(x0, top, x0 + 1, top + height, rule)
        left = x0 + (16 if index == 0 else 10)
        room = x0 + cell - 6 - left
        value_size = _fit_size(canvas, fact.value, 16, room, minimum=16 * 0.6)
        y = top + 9
        canvas.text(left, y + canvas.metrics(value_size)[0], fact.value, value_size,
                    LAMP if fact.gain else CHALK)
        y += canvas.line_height(16) + 2
        label = _upper(fact.label, canvas.lang)
        label_size = _fit_size(canvas, label, 9.5, room, minimum=9.5 * 0.7, bold=False, kerning=0.6)
        if canvas.text_width(label, label_size, False, 0.6) > room:
            label = _ellipsize(canvas, label, label_size, room, bold=False)
        canvas.text(left, y + canvas.metrics(label_size, False)[0], label, label_size,
                    _rgba(CHALK, 0.62), bold=False, kerning=0.6)


# --- штанга (FinishBarbellCard, FinishBarbellSlab, BarbellBend) --------------

_MAX_PLATES_PER_SIDE = 40
_FULL_BEND_KG = 10_000.0
_CRACK_KG = 8_000.0
_BARBELL_H = 124
_FLOOR_H = 10
_MAX_SAG = 30
_PLATE_H = 58
_MAX_PLATE_W = 7
_FLOOR_DEPTH = 150
PLATE = (77, 82, 89)
PLATE_RIM = (158, 163, 171)
STEEL = (199, 201, 204)
RACK = (41, 41, 43)


def plates_per_side(slide: BarbellSlide) -> tuple[int, int]:
    """(блинов на рукаве, всего блинов) — пара на каждые 100кг или 200lb
    тоннажа, рукав вмещает не больше 40 на сторону (`BarbellLoad`)."""
    if slide.kilograms <= 0:
        return 0, 0
    full = max(1, round(slide.kilograms * slide.units_per_kg / slide.units_per_pair))
    return min(_MAX_PLATES_PER_SIDE, full), full * 2


def _sag(x: float, width: float, hook_x: float, bend: float) -> float:
    center = width / 2
    u = min(abs(x - center) / center, 1)
    hook = min(max(abs(hook_x - center) / center, 0), 0.95)
    return bend * (u * u - hook * hook) / (1 - hook * hook)


def _barbell(canvas: _Canvas, slide: BarbellSlide, left: float, top: float, width: float) -> None:
    per_side, total = plates_per_side(slide)
    bend = math.sqrt(min(1.0, slide.kilograms / _FULL_BEND_KG)) if slide.kilograms > 0 else 0
    floor_y = top + _BARBELL_H - _FLOOR_H
    bar_y = floor_y - 64
    hook = width * 0.34

    def y_at(x: float) -> float:
        return bar_y + _sag(x, width, hook, bend) * _MAX_SAG

    d, p = canvas.draw, canvas.px
    # Пол под стойками.
    canvas.vgradient(left, floor_y, left + width, floor_y + _FLOOR_H,
                     [(0, (77, 71, 69, 255)), (1, (51, 48, 46, 255))])
    canvas.rect(left, floor_y, left + width, floor_y + 1, _rgba(WHITE, 0.12))
    if slide.kilograms >= _CRACK_KG:
        for hx, direction in ((hook, -1), (width - hook, 1)):
            x = left + hx
            lines = [
                [(x, 1), (x + direction * 6, 4), (x + direction * 3, 6), (x + direction * 12, 9)],
                [(x + direction * 6, 4), (x + direction * 16, 3), (x + direction * 22, 7)],
                [(x, 1), (x - direction * 5, 5), (x - direction * 9, 8)],
            ]
            for line in lines:
                d.line([(p(px_), p(floor_y + py)) for px_, py in line], fill=_rgba((0, 0, 0), 0.75),
                       width=max(1, p(1.2)))
    # Блин на полу лицом к зрителю и «× N».
    diameter = 46
    label = f"× {total}"  # l10n: число блинов, цифры одинаковы на всех языках
    label_w = canvas.text_width(label, 13)
    group_w = diameter + 5 + label_w
    gx = left + width / 2 - group_w / 2
    cy = floor_y - diameter / 2
    cx = gx + diameter / 2
    _radial_plate(canvas, cx, cy, diameter / 2)
    d.ellipse((p(cx - diameter / 2), p(cy - diameter / 2), p(cx + diameter / 2), p(cy + diameter / 2)),
              outline=PLATE_RIM, width=p(1.5))
    d.ellipse((p(cx - 4), p(cy - 4), p(cx + 4), p(cy + 4)), fill=_rgba((0, 0, 0), 0.6))
    ink = _rgba(WHITE, 0.92)
    weight = str(slide.plate_weight)
    w_size, m_size = 11, 7
    canvas.text(cx - canvas.text_width(weight, w_size) / 2, cy - 12 + canvas.metrics(w_size)[0]
                - canvas.line_height(w_size) / 2, weight, w_size, ink)
    canvas.text(cx - canvas.text_width(slide.plate_mark, m_size) / 2, cy + 12 + canvas.metrics(m_size)[0]
                - canvas.line_height(m_size) / 2, slide.plate_mark, m_size, ink)
    canvas.text(gx + diameter + 5, cy + canvas.metrics(13)[0] - canvas.line_height(13) / 2, label, 13, WHITE)
    # Стойки с J-крюком.
    for hx in (hook, width - hook):
        x = left + hx
        canvas.rect(x - 3.5, bar_y - 8, x + 3.5, floor_y, RACK)
        canvas.rect(x - 6.5, bar_y + 2 - 1.5, x + 6.5, bar_y + 2 + 1.5, RACK)
    # Гриф.
    collar = hook - 10
    count = max(per_side, 1)
    room = collar - 10
    pitch = min(_MAX_PLATE_W + 1, room / count)
    gap = 1 if pitch > 4 else 0.5
    plate_w = max(pitch - gap, 1)
    sleeve_end = max(collar - count * (plate_w + gap) - 8, 2)
    points = []
    x = sleeve_end
    while x < width - sleeve_end:
        points.append((p(left + x), p(y_at(x))))
        x += 1.5
    points.append((p(left + width - sleeve_end), p(y_at(width - sleeve_end))))
    d.line(points, fill=STEEL, width=p(4), joint="curve")
    for end in (points[0], points[-1]):
        r = p(2)
        d.ellipse((end[0] - r, end[1] - r, end[0] + r, end[1] + r), fill=STEEL)
    # Блины веером по касательной к грифу.
    for index in range(per_side):
        for side in (-1, 1):
            base = collar if side < 0 else width - collar
            x = base + side * (index * (plate_w + gap) + plate_w / 2)
            slope = (y_at(x + 1) - y_at(x - 1)) / 2
            _plate(canvas, left + x, y_at(x), plate_w, math.atan(slope))


def _radial_plate(canvas: _Canvas, cx: float, cy: float, r: float) -> None:
    """Железо с отливом: свет в точке (0.35, 0.3) круга к краю темнее."""
    from PIL import Image

    size = canvas.px(r * 2)
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pixels = layer.load()
    hx, hy = size * 0.35, size * 0.3
    end = r * 2 * 0.7 * _K
    half = size / 2
    for yy in range(size):
        for xx in range(size):
            if (xx - half) ** 2 + (yy - half) ** 2 > half * half:
                continue
            t = min(1.0, math.hypot(xx - hx, yy - hy) / end)
            pixels[xx, yy] = tuple(round(a + (b - a) * t) for a, b in zip(PLATE_RIM, PLATE, strict=True)) + (255,)
    canvas.image.paste(layer, (canvas.px(cx - r), canvas.px(cy - r)), layer)


def _plate(canvas: _Canvas, cx: float, cy: float, w: float, angle: float) -> None:
    hw, hh = w / 2, _PLATE_H / 2
    cut = min(2, hw)
    outline = [(-hw + cut, -hh), (hw - cut, -hh), (hw, -hh + cut), (hw, hh - cut),
               (hw - cut, hh), (-hw + cut, hh), (-hw, hh - cut), (-hw, -hh + cut)]
    cos, sin = math.cos(angle), math.sin(angle)

    def place(points):
        return [(canvas.px(cx + x * cos - y * sin), canvas.px(cy + x * sin + y * cos)) for x, y in points]

    canvas.draw.polygon(place(outline), fill=PLATE, outline=_rgba(PLATE_RIM, 0.7), width=max(1, canvas.px(1) // 2))
    if w > 4:
        canvas.draw.polygon(place([(-2.25, -hh + 1), (-0.75, -hh + 1), (-0.75, hh - 1), (-2.25, hh - 1)]),
                            fill=_rgba(WHITE, 0.18))


def _lamp(canvas: _Canvas) -> None:
    """Лампа на шнуре сверху по центру — тот же тёплый свет подвала."""
    from PIL import Image, ImageDraw, ImageFilter

    cx = CARD_W / 2
    canvas.rect(cx - 1, 0, cx + 1, 22, (41, 41, 41))
    box = 80  # окно свечения вокруг лампы, pt
    glow = Image.new("L", (canvas.px(box), canvas.px(box)), 0)
    gd = ImageDraw.Draw(glow)
    gd.ellipse((canvas.px(box / 2 - 8), canvas.px(box / 2 - 8), canvas.px(box / 2 + 8), canvas.px(box / 2 + 8)),
               fill=255)
    glow = glow.filter(ImageFilter.GaussianBlur(canvas.px(22) / 2))
    canvas.image.paste(Image.new("RGB", glow.size, LAMP), (canvas.px(cx - box / 2), canvas.px(30 - box / 2)),
                       glow.point(lambda v: round(v * 0.7)))
    canvas.draw.ellipse((canvas.px(cx - 8), canvas.px(22), canvas.px(cx + 8), canvas.px(38)), fill=LAMP)


def render_barbell(slide: BarbellSlide, signature: Signature, lang: str, icon: Optional[str] = None) -> bytes:
    canvas = _Canvas(lang)
    _brick(canvas, 0, 0, CARD_W, CONTENT_H, "finished")
    _lamp(canvas)
    floor_top = CONTENT_H - _FLOOR_DEPTH
    canvas.vgradient(0, floor_top, CARD_W, CONTENT_H, [
        (0, CONCRETE + (255,)), (0.35, _rgba(CONCRETE, 0.9)), (1, COAL + (255,)),
    ])
    _barbell(canvas, slide, 18, floor_top - _BARBELL_H, CARD_W - 36)
    y = _chips(canvas, None, slide.day) + 8
    title = _upper(slide.title, lang)
    title_size = _fit_size(canvas, title, 24, CARD_W - 32, minimum=24 * 0.6, kerning=0.6)
    canvas.shadowed_text(16, y + canvas.metrics(title_size)[0], title, title_size, WHITE, kerning=0.6)
    y += canvas.line_height(24)
    # Строка цифры — постоянной высоты 92, строка по центру.
    asc, desc = canvas.metrics(88)
    baseline = y + (92 - (asc + desc)) / 2 + asc
    _big_number(canvas, 16, baseline, slide.value, slide.unit, shadow=True)
    y += 92
    if slide.lifted:
        lifted = _upper(slide.lifted, lang)
        size = _fit_size(canvas, lifted, 13, CARD_W - 32, minimum=13 * 0.7, kerning=1.2)
        canvas.shadowed_text(16, y + canvas.metrics(size)[0], lifted, size, LAMP, kerning=1.2, radius=4, dy=1)
    if slide.facts:
        _facts_row(canvas, slide.facts, CONTENT_H - _facts_height(canvas))
    _signature(canvas, signature, icon)
    return canvas.finish()


# --- рекорд (FinishRecordCard) ----------------------------------------------


def _wrap(canvas: _Canvas, text: str, size: float, width: float, max_lines: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if canvas.text_width(candidate, size) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return [_ellipsize(canvas, line, size, width) for line in lines]


def _letter_tile(canvas: _Canvas, name: str, x0: float, y0: float, x1: float, y1: float, size: float) -> None:
    """Первая буква названия на кирпиче — у упражнения без кадра."""
    _brick(canvas, x0, y0, x1, y1, name)
    initial = _upper(name.strip()[:1], canvas.lang)
    if initial:
        asc, desc = canvas.metrics(size)
        w = canvas.text_width(initial, size)
        canvas.text((x0 + x1) / 2 - w / 2, (y0 + y1) / 2 - (asc + desc) / 2 + asc, initial, size, WHITE)


def render_record(slide: RecordSlide, signature: Signature, lang: str, icon: Optional[str] = None) -> bytes:
    canvas = _Canvas(lang)
    facts_h = _facts_height(canvas)
    name_lines = _wrap(canvas, slide.exercise_name, 17, CARD_W - 32, 2)
    name_h = canvas.line_height(17) * len(name_lines)
    number_h = canvas.line_height(88)
    text_h = name_h + 2 + number_h + 8
    hero_bottom = CONTENT_H - facts_h - text_h
    drawn = bool(slide.poster) and canvas.paste_cover(slide.poster, 0, 0, CARD_W, hero_bottom)
    if drawn:
        _fade(canvas, 0, 0, CARD_W, hero_bottom)
    elif slide.figure is not None:
        # Своё упражнение без кадра — его мышцы на схеме, на угле
        # (`FinishRecordCard.hero`: поля 54 сверху, 40 по бокам, 6 снизу).
        _body_pair(canvas, slide.figure, 40, 54, CARD_W - 40, hero_bottom - 6)
    else:
        _letter_tile(canvas, slide.exercise_name, 0, 0, CARD_W, hero_bottom, 150)
        _fade(canvas, 0, 0, CARD_W, hero_bottom)
    _chips(canvas, slide.chip, slide.day, star=True)
    y = hero_bottom
    for line in name_lines:
        canvas.text(16, y + canvas.metrics(17)[0], line, 17, CHALK)
        y += canvas.line_height(17)
    y += 2
    _big_number(canvas, 16, y + canvas.metrics(88)[0], slide.value, slide.unit, prefix=slide.prefix)
    _facts_row(canvas, slide.facts, CONTENT_H - facts_h)
    _signature(canvas, signature, icon)
    return canvas.finish()


# --- «Тренировка» (FinishSummaryCard) ---------------------------------------


def render_summary(slide: SummarySlide, signature: Signature, lang: str, icon: Optional[str] = None) -> bytes:
    canvas = _Canvas(lang)
    _chips(canvas, slide.chip, slide.day)
    y = 50.0
    _big_number(canvas, 16, y + canvas.metrics(64)[0], slide.value, slide.unit, size=64)
    y += canvas.line_height(64)
    if slide.meta:
        size = _fit_size(canvas, slide.meta, 12, CARD_W - 32, minimum=12 * 0.8, bold=False)
        meta = _ellipsize(canvas, slide.meta, size, CARD_W - 32, bold=False)
        canvas.text(16, y + canvas.metrics(size, False)[0], meta, size, _rgba(CHALK, 0.62), bold=False)
        y += canvas.line_height(12, bold=False)
    y += 6
    name_h = canvas.line_height(13)
    sets_h = canvas.line_height(11, bold=False)
    row_h = 5 + max(30, name_h + 1 + sets_h) + 5
    for index, row in enumerate(slide.rows):
        top = y + index * row_h
        mid = top + row_h / 2
        x = 16.0
        if row.thumb and canvas.paste_cover(row.thumb, x, mid - 15, x + 30, mid + 15):
            pass
        elif row.region is not None:
            _region_tile(canvas, row.region, x, mid - 15, 30)
        else:
            _letter_tile(canvas, row.name, x, mid - 15, x + 30, mid + 15, 15)
        _round_corners(canvas, x, mid - 15, 30, 4)
        right = CARD_W - 16
        if row.badge:
            bw = canvas.text_width(row.badge, 11) + 12
            bh = canvas.line_height(11) + 6
            canvas.rounded(right - bw, mid - bh / 2, right, mid + bh / 2, 3, fill=LAMP)
            canvas.text(right - bw + 6, mid - bh / 2 + 3 + canvas.metrics(11)[0], row.badge, 11, COAL)
            right -= bw + 10
        tx = x + 30 + 10
        room = right - tx
        text_top = mid - (name_h + 1 + sets_h) / 2
        canvas.text(tx, text_top + canvas.metrics(13)[0], _ellipsize(canvas, row.name, 13, room), 13, CHALK)
        sets_size = _fit_size(canvas, row.sets, 11, room, minimum=11 * 0.8, bold=False)
        canvas.text(tx, text_top + name_h + 1 + canvas.metrics(sets_size, False)[0],
                    _ellipsize(canvas, row.sets, sets_size, room, bold=False), sets_size,
                    _rgba(CHALK, 0.6), bold=False)
        if index < len(slide.rows) - 1:
            canvas.rect(16, top + row_h - 1, CARD_W - 16, top + row_h, _rgba(WHITE, 0.1))
    if slide.more:
        y += len(slide.rows) * row_h + 4
        canvas.text(16, y + canvas.metrics(11)[0], slide.more, 11, _rgba(CHALK, 0.6))
    _signature(canvas, signature, icon)
    return canvas.finish()


def _round_corners(canvas: _Canvas, x: float, y: float, size: float, radius: float) -> None:
    """Скруглить углы квадратного превью — закрасить уголки углём."""
    from PIL import Image, ImageDraw

    s = canvas.px(size)
    mask = Image.new("L", (s, s), 255)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=canvas.px(radius), fill=0)
    canvas.image.paste(Image.new("RGB", (s, s), COAL), (canvas.px(x), canvas.px(y)), mask)


# --- схема тела («Сегодня работали», FinishBodyCard) -------------------------


def _fill_polygons(draw, polygons, transform, fill, offset: tuple[float, float] = (0, 0)) -> None:
    """Многоугольники поля схемы — в пиксели холста (масштаб уже с `_K`)."""
    ox, oy = offset
    for polygon in body_map.placed(polygons, transform):
        draw.polygon([(x - ox, y - oy) for x, y in polygon], fill=fill)


def _body_side(canvas: _Canvas, side: str, figure: body_map.Figure, x0: float, y0: float,
               x1: float, y1: float) -> None:
    """Одна сторона в два тона (`BodyMapTwoToneFigure`), вписана в рамку."""
    frame = (canvas.px(x0), canvas.px(y0), canvas.px(x1) - canvas.px(x0), canvas.px(y1) - canvas.px(y0))
    transform = body_map.fit(body_map.FULL_FIGURE, frame)
    if transform is None:
        return
    colors = {"base": BODY_BASE, "secondary": BODY_SECONDARY, "primary": BODY_PRIMARY}
    for layer, polygons in body_map.layers(side, figure.primary(side), figure.secondary(side)):
        _fill_polygons(canvas.draw, polygons, transform, colors[layer])


def _body_pair(canvas: _Canvas, figure: body_map.Figure, x0: float, y0: float, x1: float, y1: float,
               spacing: float = 18) -> None:
    """Спереди и сзади рядом, поровну, через `spacing` (`FinishShareBodyPair`)."""
    if x1 - x0 <= spacing or y1 <= y0:
        return
    half = (x1 - x0 - spacing) / 2
    _body_side(canvas, body_map.FRONT, figure, x0, y0, x0 + half, y1)
    _body_side(canvas, body_map.BACK, figure, x1 - half, y0, x1, y1)


def _region_tile(canvas: _Canvas, region: str, x: float, y: float, size: float) -> None:
    """Мини-плитка группы (`GroupTilePlaceholder`): песок, зона группы крупно —
    мышцы кирпичом, соседние тоном темнее песка; руки — одной рукой."""
    from PIL import Image, ImageDraw

    s = canvas.px(size)
    tile = Image.new("RGB", (s, s), THUMB_FACE)
    draw = ImageDraw.Draw(tile)
    zone = body_map.REGIONS[region]
    transform = body_map.fit(body_map.crop_rect(region, 1.0), (0, 0, s, s))
    if transform is not None:
        for layer, polygons in body_map.layers(zone.side, zone.slugs):
            if layer == "secondary":
                continue
            _fill_polygons(draw, polygons, transform, THUMB_MUSCLE if layer == "primary" else THUMB_FIGURE)
    canvas.image.paste(tile, (canvas.px(x), canvas.px(y)))


def _title_lines(canvas: _Canvas, groups: Sequence[str], width: float) -> tuple[float, list[str]]:
    """Заголовок «Сегодня работали» в две строки: 26 black, не влезает —
    ужимается до 0.6 (`lineLimit(2)`, `minimumScaleFactor(0.6)`). Переносится
    между группами, точка-разделитель остаётся в конце строки: строка с «·»
    в начале читалась как оборванная."""
    tokens = [f"{group} ·" for group in groups[:-1]] + list(groups[-1:])
    size = 26.0
    while True:
        lines: list[str] = []
        for token in tokens:
            if lines and canvas.text_width(f"{lines[-1]} {token}", size) <= width:
                lines[-1] = f"{lines[-1]} {token}"
            else:
                lines.append(token)
        fits = len(lines) <= 2 and all(canvas.text_width(line, size) <= width for line in lines)
        if fits or size <= 26 * 0.6:
            if len(lines) > 2:
                lines = lines[:1] + [" ".join(lines[1:])]
            return size, [_ellipsize(canvas, line, size, width) for line in lines]
        size = max(26 * 0.6, size - 0.5)


def render_body(slide: BodySlide, signature: Signature, lang: str, icon: Optional[str] = None) -> bytes:
    canvas = _Canvas(lang)
    facts_h = _facts_height(canvas) if slide.facts else 0
    size, lines = _title_lines(canvas, slide.groups, CARD_W - 32)
    line_h = canvas.line_height(size)
    title_bottom = CONTENT_H - facts_h - 12
    title_top = title_bottom - line_h * len(lines)
    _body_pair(canvas, slide.figure, 28, 54, CARD_W - 28, title_top - 8)
    y = title_top
    for line in lines:
        canvas.text(16, y + canvas.metrics(size)[0], line, size, WHITE)
        y += line_h
    if slide.facts:
        _facts_row(canvas, slide.facts, CONTENT_H - facts_h)
    _chips(canvas, slide.chip, slide.day)
    _signature(canvas, signature, icon)
    return canvas.finish()


# --- колода целиком ---------------------------------------------------------

APP_ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media", "site", "icons", "icon-512.png")


def app_icon() -> Optional[str]:
    """Иконка приложения для подписи, если файл на месте."""
    return APP_ICON if os.path.isfile(APP_ICON) else None


def render_slide(slide: Slide, signature: Signature, lang: str) -> bytes:
    icon = app_icon()
    if isinstance(slide, BarbellSlide):
        return render_barbell(slide, signature, lang, icon)
    if isinstance(slide, RecordSlide):
        return render_record(slide, signature, lang, icon)
    if isinstance(slide, BodySlide):
        return render_body(slide, signature, lang, icon)
    return render_summary(slide, signature, lang, icon)


def render_deck(deck: Deck) -> list[bytes]:
    return [render_slide(slide, deck.signature, deck.lang) for slide in deck.slides]


def _poster_path(exercise, style: str) -> Optional[str]:
    """Кадр постера рекорда: своё фото атлета, потом первый кадр каталога."""
    own = exercise_photos.path_for_exercise(exercise)
    if own:
        return own
    images = exercise_media.get_images_for(exercise, style)
    return images[0] if images else None


def _thumb_path(exercise, style: str) -> Optional[str]:
    """Превью ряда «Тренировки»: миниатюра каталога, потом своё фото."""
    images = exercise_media.get_images_for(exercise, style)
    if images:
        small = os.path.join(exercise_media.THUMBS_DIR, os.path.basename(images[0]))
        return small if os.path.isfile(small) else images[0]
    return exercise_photos.path_for_exercise(exercise)


async def build(workout_id: int, user) -> list[bytes]:
    """PNG слайдов завершённой тренировки по порядку колоды; пусто — нечего
    показать (нет тренировки или ни одного подхода)."""
    workout = await db.get_workout(workout_id)
    if workout is None:
        return []
    blocks = await view_builder.build_block_views(
        workout_id, _field(user, "e1rm_formula", "epley"),
        previous_before=workout["started_at"], mark_records=True,
    )
    exercises = [b for b in blocks if isinstance(b, formatting.ExerciseBlockView) and b.sets]
    if not exercises:
        return []
    duration = await view_builder.workout_duration_seconds(workout)
    style = _field(user, "exercise_images", "drawing")
    if style not in exercise_media.IMAGE_STYLES:
        style = "drawing"
    posters: dict[int, str] = {}
    thumbs: dict[int, str] = {}
    muscles: dict[int, body_map.Muscles] = {}
    for block in exercises:
        if block.exercise_id is None:
            continue
        row = await db.get_exercise(block.exercise_id)
        if row is None:
            continue
        markup = exercise_muscles.for_exercise(row)
        if markup is not None:
            muscles[block.exercise_id] = (markup["primary"], markup["secondary"])
        if block.record_reps is not None or block.record_e1rm_delta is not None:
            poster = _poster_path(row, style)
            if poster:
                posters[block.exercise_id] = poster
        thumb = _thumb_path(row, style)
        if thumb:
            thumbs[block.exercise_id] = thumb
    with i18n.use_lang(_field(user, "lang", i18n.DEFAULT_LANG)):
        deck = assemble(
            blocks, user, started_at=dt.datetime.fromisoformat(workout["started_at"]),
            duration_seconds=duration, posters=posters, thumbs=thumbs, muscles=muscles,
        )
    if not deck.slides:
        return []
    return await asyncio.to_thread(render_deck, deck)
