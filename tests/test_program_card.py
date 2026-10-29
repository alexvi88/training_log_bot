"""Визитка программы картинкой (program_card.py): что из снапшота попадает на
картинку, размер PNG, рост высоты с содержимым и её предел, язык и глифы
всего, что уезжает в PNG. Отправку из «Поделиться» проверяет
tests/test_sharing.py."""

import io
import re

import pytest
from PIL import Image

import finish_slides as fs
import i18n
import program_card as pc

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def setup_function(_):
    i18n.reload()


def _payload(days: int, exercises: int, *, total=None, name="Split", day_name="Day", ex_name="Bench press"):
    return {
        "v": 4,
        "name": name,
        "days": [
            {"name": f"{day_name} {d + 1}",
             "exercises": [{"name": f"{ex_name} {i + 1}", "target": "3×8–10"} for i in range(exercises)]}
            for d in range(days)
        ],
        "total_days": total if total is not None else days,
        "empty_days": 0,
    }


def _card(payload, lang="en"):
    with i18n.use_lang(lang):
        return pc.assemble(payload)


def _size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    return Image.open(io.BytesIO(png)).size


# ---------- что на картинке ----------


def test_card_mirrors_app_limits_and_more_lines():
    card = _card(_payload(6, 8, total=8))
    assert card.chip == "Program" and card.size == "8 days"
    assert len(card.days) == pc.MAX_DAYS
    assert all(len(day.rows) == pc.MAX_EXERCISES_PER_DAY for day in card.days)
    assert card.days[0].more == "3 more exercises"
    # «Ещё N дней» — от полного числа дней программы, как у приложения.
    assert card.more_days == "4 more days"


def test_small_program_has_no_more_lines():
    card = _card(_payload(2, 3))
    assert card.more_days is None and all(day.more is None for day in card.days)
    assert card.days[0].rows[0] == pc.CardRow("Bench press 1", "3×8–10")


def test_program_name_loses_leading_emoji_and_undrawable_glyphs():
    card = _card(_payload(1, 1, name="💪 Split 🔥 A", ex_name="Squat 🦵"))
    assert card.title == "Split A"
    assert card.days[0].rows[0].name == "Squat 1"
    assert not fs.missing_glyphs(" ".join(pc.card_texts(card)))


def test_catalog_names_and_targets_follow_the_owner_language():
    """Те же `shown_exercise_name`/`shown_target`, что у текста визитки: имя
    каталога и «сек» в схеме — на языке владельца."""
    payload = {"v": 4, "name": "Core", "total_days": 1, "empty_days": 0,
               "days": [{"name": "A", "exercises": [{"name": "Планка", "target": "3×30–60 сек"}]}]}
    row = _card(payload, "en").days[0].rows[0]
    assert not CYRILLIC.search(row.name) and row.target == "3×30–60 sec"
    row_ru = _card(payload, "ru").days[0].rows[0]
    assert row_ru.name == "Планка"


# ---------- картинка ----------


def test_renders_png_1080_wide_and_grows_with_content():
    small = _size(pc.render(_card(_payload(1, 2))))
    medium = _size(pc.render(_card(_payload(3, 4))))
    assert small[0] == medium[0] == 1080
    assert small[1] < medium[1]
    # Высота по содержимому, а не кадр 640 (`FinishShareFrame(height: nil)`).
    assert small[1] < fs.HEIGHT


def test_height_is_capped_by_the_limits():
    """Сверх `MAX_DAYS`×`MAX_EXERCISES_PER_DAY` картинка не растёт: остаток —
    строками «ещё N»."""
    at_limit = pc.height_pt(_card(_payload(pc.MAX_DAYS + 2, pc.MAX_EXERCISES_PER_DAY + 3, total=12,
                                           name="W" * 48)))
    huge = pc.height_pt(_card(_payload(6, 30, total=40, name="W" * 48)))
    assert huge == at_limit
    # Длиннее трёх экранов картинка не бывает: Telegram её ещё показывает целиком.
    assert at_limit * fs.SCALE <= 3000
    png = pc.render(_card(_payload(6, 30, total=40, name="W" * 48)))
    assert _size(png) == (1080, round(huge * fs.SCALE))


def test_long_names_do_not_break_rendering():
    payload = _payload(4, 5, name="Очень длинное название программы для продвинутых атлетов",
                       day_name="День тяжёлого верха с акцентом на грудь и плечи",
                       ex_name="Жим штанги лёжа с паузой на груди и узким хватом")
    card = _card(payload, "ru")
    assert _size(pc.render(card))[0] == 1080


@pytest.mark.parametrize("lang", i18n.SUPPORTED)
def test_card_text_has_glyphs_and_right_language(lang):
    payload = {
        "v": 4, "name": "💪 PPL", "total_days": 7, "empty_days": 0,
        "days": [
            {"name": "Push", "exercises": [{"name": "Жим штанги лёжа", "target": "4×6–8"}] * 7},
            {"name": "Pull", "exercises": [{"name": "Планка", "target": "3×30–60 сек"}]},
        ] * 3,
    }
    card = _card(payload, lang)
    texts = pc.card_texts(card)
    joined = " ".join(texts + [fs._upper(t, lang) for t in texts])
    assert not fs.missing_glyphs(joined, bold=True), (lang, fs.missing_glyphs(joined))
    assert not fs.missing_glyphs(joined, bold=False), (lang, fs.missing_glyphs(joined, bold=False))
    assert "&" not in joined and "<" not in joined, joined
    if lang != "ru":
        assert not CYRILLIC.search(joined), (lang, joined)
    assert card.more_days and card.days[0].more
    assert _size(pc.render(card))[0] == 1080
