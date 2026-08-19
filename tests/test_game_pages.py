"""Страницы мини-игр: один персонаж на обе игры и честный пересчёт голов врага.

Тесты сторожат ровно те две вещи, которые уже успели разъехаться живьём и
которые не видно ни в одном другом тесте (страницы — статические HTML, их
никто не парсит):

1. Персонаж один — векторный. Раньше поверх него подгружались фотореалистичные
   ``assets/game/<key>.jpg``, и в продукте жили ДВА разных качка: «под фото» на
   карточке выбора и на экране итога, минималистичная фигура — в самой игре.
2. Видимое число голов в группе врагов считается от цены ОДНОЙ головы с учётом
   множителя живучести от дистанции. Деление на голый ``hpUnit`` держало цифру
   на месте, пока не сожжён весь множитель, — на дистанции это выглядело как
   «стреляю в бетон, счётчик не двигается».
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PAGES = [BASE_DIR / "game.html", BASE_DIR / "game_squad.html"]


def test_game_pages_do_not_load_character_photos():
    for page in PAGES:
        text = page.read_text(encoding="utf-8")
        # Комментарии про историю правки упоминать .jpg могут, код — нет.
        code_lines = [ln for ln in text.split("\n") if not ln.strip().startswith("//")]
        assert ".jpg" not in "\n".join(code_lines), page.name


def test_no_character_photos_left_in_assets():
    assets = BASE_DIR / "assets" / "game"
    assert list(assets.glob("*.jpg")) == []


def test_both_pages_draw_the_same_vector_portrait():
    for page in PAGES:
        text = page.read_text(encoding="utf-8")
        assert "function fighterPortrait(" in text, page.name
        # Портрет — тот же атлет, что бежит в игре, а не отдельная картинка.
        assert "drawAthlete(cc, key" in text, page.name


def test_enemy_head_count_is_derived_from_price_of_one_head():
    text = (BASE_DIR / "game_squad.html").read_text(encoding="utf-8")
    assert "hpPerHead" in text
    # Старая форма: делили на hpUnit, мимо множителя живучести от дистанции.
    assert "hitFg.hp / (def.hpUnit" not in text
