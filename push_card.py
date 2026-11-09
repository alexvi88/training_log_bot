"""«Пуш как мем» — пуш о пропуске картинкой-сторис, которой не стыдно поделиться.

Тренер из подвала сидит на скамье и ждёт (`media/push/coach_reference.jpg` —
тот же персонаж, что в пушах, и без надписей в кадре: у «входящего звонка»
русское «ТРЕНЕР входящий» вшито в картинку и протекло бы англоязычному)
наверху кадра, под ним — уведомление, как его видит телефон: иконка, имя
приложения, «сейчас», заголовок и текст пуша дословно. Ниже — одна строка
от атлета «Мой дневник тренировок пишет мне вот это» и подпись приложения с
плашкой App Store. Холст, шрифт и подпись — finish_slides, так что карточка
стоит в одном ряду со слайдами итогов.

Делиться можно только пушами-пропусками (`SHAREABLE`): подколки в продукте —
только про пропуски (TONE_OF_VOICE.md), и смешное в них — тренер, который
считает дни. Рекорд, звание и итог недели — уже свои карточки.

Текст пуша приходит уже готовым на языке атлета (подпись Telegram-пуша или
строка «Уведомлений»), своё здесь — только подпись атлета и «сейчас».
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from typing import Optional

import finish_slides as fs
import i18n
import push_texts

COACH_IMAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media", "push", "coach_reference.jpg")

SHAREABLE = frozenset({
    push_texts.SKIP_3, push_texts.SKIP_5, push_texts.SKIP_7, push_texts.SKIP_10, push_texts.SKIP_14,
    push_texts.WIN_BACK,
})

# Длиннее — обрезаем по слову с «…»: телефон тоже не показывает простыню.
MAX_BODY_CHARS = 260


@dataclasses.dataclass(frozen=True)
class Card:
    app_name: str
    when: str
    title: str
    body: str
    caption: str


def shareable(category: Optional[str]) -> bool:
    return category in SHAREABLE


def _clip(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= MAX_BODY_CHARS:
        return text
    cut = text[:MAX_BODY_CHARS].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:—- ") + "…"


def assemble(body: str, title: str = "") -> Card:
    """Строки карточки на текущем языке (вызывающий — под i18n.use_lang)."""
    return Card(
        app_name=i18n.t("finish_slides.brand"),
        when=i18n.t("push_card.now"),
        title=title.strip(),
        body=_clip(body),
        caption=i18n.t("push_card.caption"),
    )


def card_texts(card: Card, signature: fs.Signature) -> list[str]:
    texts = [signature.brand, signature.tagline, signature.store_small, signature.store_big,
             card.app_name, card.when, card.title, card.body, card.caption]
    return [t for t in texts if t]


def render(card: Card, signature: fs.Signature, lang: str) -> bytes:
    canvas = fs._Canvas(lang)
    width = fs.CARD_W - 32
    # Тренер — квадратом во всю ширину сверху, уход в уголь к уведомлению.
    hero_bottom = 390.0
    if not canvas.paste_cover(COACH_IMAGE, 0, 0, fs.CARD_W, hero_bottom):
        fs._brick(canvas, 0, 0, fs.CARD_W, hero_bottom, "push_card")
        fs._lamp(canvas)
    canvas.vgradient(0, hero_bottom - 90, fs.CARD_W, hero_bottom + 2, [
        (0, fs._rgba(fs.COAL, 0)), (0.75, fs._rgba(fs.COAL, 0.92)), (1, fs._rgba(fs.COAL, 1)),
    ])
    # Уведомление: светлая плашка как на экране блокировки.
    body_size = 13.5
    body_lines = fs._wrap(canvas, card.body, body_size, width - 28, 7)
    while len(body_lines) > 6 and body_size > 11:
        body_size -= 0.5
        body_lines = fs._wrap(canvas, card.body, body_size, width - 28, 7)
    head_h = 22
    title_h = canvas.line_height(13) + 2 if card.title else 0
    lines_h = canvas.line_height(body_size, bold=False) * len(body_lines)
    box_h = 12 + head_h + title_h + lines_h + 14
    # Уведомление — на коленях у тренера и уходе в уголь, а не на лице.
    box_top = hero_bottom - 50
    x0, x1 = 16, fs.CARD_W - 16
    canvas.rounded(x0, box_top, x1, box_top + box_h, 16, fill=fs._rgba((246, 240, 232), 0.96))
    icon = fs.app_icon()
    tx = x0 + 14
    head_mid = box_top + 12 + head_h / 2
    if icon:
        from PIL import Image, ImageDraw

        try:
            with Image.open(icon) as source:
                size = canvas.px(20)
                tile = source.convert("RGB").resize((size, size), Image.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=canvas.px(5), fill=255)
            canvas.image.paste(tile, (canvas.px(tx), canvas.px(head_mid - 10)), mask)
            tx += 20 + 8
        except Exception:
            pass
    ink = (34, 28, 25)
    muted = fs._rgba(ink, 0.55)
    app = fs._upper(card.app_name, lang)
    canvas.text(tx, head_mid - canvas.line_height(10.5, bold=False) / 2 + canvas.metrics(10.5, False)[0],
                app, 10.5, muted, bold=False, kerning=0.6)
    when_w = canvas.text_width(card.when, 10.5, bold=False)
    canvas.text(x1 - 14 - when_w, head_mid - canvas.line_height(10.5, bold=False) / 2
                + canvas.metrics(10.5, False)[0], card.when, 10.5, muted, bold=False)
    y = box_top + 12 + head_h + 2
    if card.title:
        title = fs._ellipsize(canvas, card.title, 13, width - 28)
        canvas.text(x0 + 14, y + canvas.metrics(13)[0], title, 13, ink)
        y += title_h
    for line in body_lines:
        canvas.text(x0 + 14, y + canvas.metrics(body_size, False)[0], line, body_size, ink, bold=False)
        y += canvas.line_height(body_size, bold=False)
    # Подпись атлета под уведомлением.
    y = box_top + box_h + 22
    caption = fs._upper(card.caption, lang)
    size = fs._fit_size(canvas, caption, 15, width, minimum=10, kerning=1)
    for line in fs._wrap(canvas, caption, size, width, 2):
        if y + canvas.line_height(size) > fs.CONTENT_H - 8:
            break
        canvas.text(16, y + canvas.metrics(size)[0], line, size, fs.LAMP, kerning=1)
        y += canvas.line_height(size)
    fs._signature(canvas, signature, icon)
    return canvas.finish()


async def build(body: str, title: str = "", lang: Optional[str] = None) -> bytes:
    lang = lang or i18n.get_lang()
    with i18n.use_lang(lang):
        card = assemble(body, title)
        signature = fs.current_signature()
    return await asyncio.to_thread(render, card, signature, lang)
