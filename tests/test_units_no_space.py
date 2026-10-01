"""Единица веса и тоннажа — вплотную к числу, на обоих языках: «80кг», «10.9т»,
«40kg», «50t», «7,050lb» (TONE_OF_VOICE.md, «Вес и тоннаж — единица вплотную к
числу»). Раньше рядом жили «20kg to go» и «осталось 10.9 т», «76kg vs 63kg» и
«39.1 т» — один экран говорил двумя почерками.

Слово целиком («10 тонн», «5 tons») — обычное слово и идёт через пробел; счётчики,
время и повторы правило не трогает.
"""

import json
import re
from pathlib import Path

import analytics
import formatting
import i18n

ROOT = Path(__file__).resolve().parent.parent

# Число или плейсхолдер, пробел (обычный или неразрывный), сокращённая единица
# веса/тоннажа, за которой не продолжается слово.
_SPACED_UNIT = re.compile(r"(?:\d|\})[  ](?:кг|фнт|т|kg|lbs?|t)(?![\wа-яё])", re.IGNORECASE)


def test_catalogs_keep_weight_units_next_to_the_number():
    offenders = []
    for lang in ("ru", "en"):
        catalog = json.loads((ROOT / "locales" / f"{lang}.json").read_text(encoding="utf-8"))
        for key, value in catalog.items():
            if isinstance(value, str) and _SPACED_UNIT.search(value):
                offenders.append(f"{lang}:{key}: {_SPACED_UNIT.search(value).group(0)!r}")
    assert offenders == []


def test_formatted_weights_have_no_space_before_the_unit():
    with i18n.use_lang("ru"):
        assert formatting.format_rank_gap(analytics.RankGap("tonnage", 10_900)) == "ещё 10.9т"
        assert formatting.format_rank_gap(analytics.RankGap("tonnage", 20)) == "ещё 20кг"
        assert formatting.format_tonnage(800, "kg") == "800кг"
    with i18n.use_lang("en"):
        assert formatting.format_rank_gap(analytics.RankGap("tonnage", 10_900)) == "10.9t to go"
        assert formatting.format_rank_gap(analytics.RankGap("tonnage", 20)) == "20kg to go"
        assert formatting.format_tonnage(800, "lb") == "800lb"
        # Полное слово — через пробел: это слово, а не сокращение единицы.
        assert formatting.format_tonnage(3200, "kg") == "3.2 tons"

