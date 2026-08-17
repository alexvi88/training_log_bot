"""Два блокера, найденных независимым аудитом уже после «готово».

Оба — про данные, а не про литералы, и оба про ШОВ: перевод существовал, один
вход к нему научили, соседний остался.

1. Шаблоны каталога в клавиатуре упражнений. Шаблон — общая строка на всех
   (`exercises.is_template=1`), персональной копии с переведённым именем у него
   нет, в базе имя навсегда русское. Каталожный браузер по группе локализацию
   получил, а три экрана ПОИСКА (живая тренировка, правка прошлой, добавление в
   программу) ходят в общую keyboards.exercises_keyboard — и англоязычный на
   запрос «bench» получал «📋 Жим штанги лёжа».

2. Мини-игры. Страница статическая, свой язык берёт из Telegram WebApp — а там
   лежит язык КЛИЕНТА, не наш users.lang. Человек с русским телефоном, выбравший
   English в настройках, получал игру целиком по-русски.

Храповик по кириллическим литералам не ловит ни то, ни другое: в первом случае
русский приезжает из базы, во втором живёт в HTML.
"""

import pathlib
import re

import i18n
import keyboards
import seed_data

CYRILLIC = re.compile("[А-Яа-яЁё]")


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def test_catalog_templates_in_the_picker_are_localized():
    """Шаблон приходит в клавиатуру сырой строкой из базы — язык выбирается
    здесь, иначе каждый новый экран с шаблонами протечёт заново."""
    templates = [
        {"id": i, "display_name": name}
        for i, name in enumerate(("Жим штанги лёжа", "Присед со штангой"), start=1)
    ]
    with i18n.use_lang("en"):
        markup = keyboards.exercises_keyboard([], templates=templates, prefix="exm")
    leaked = [label for label in _labels(markup) if CYRILLIC.search(label)]
    assert not leaked, f"русские имена шаблонов у англоязычного: {leaked}"

    with i18n.use_lang("ru"):
        markup_ru = keyboards.exercises_keyboard([], templates=templates, prefix="exm")
    assert any("Жим штанги лёжа" in label for label in _labels(markup_ru))


def test_user_exercises_are_never_translated():
    """Своё упражнение — данные пользователя: как назвал, так и показываем, на
    любом языке интерфейса."""
    own = [{"id": 1, "display_name": "Жим штанги лёжа"}]
    with i18n.use_lang("en"):
        markup = keyboards.exercises_keyboard(own, templates=[], prefix="exm")
    assert any("Жим штанги лёжа" in label for label in _labels(markup))


def test_every_catalog_template_has_an_english_name():
    """Сквозная проверка: перевод есть у всех ста, а не у тех, что попались."""
    for _group, name in seed_data.EXERCISE_TEMPLATES:
        localized = seed_data.localized_exercise_name(name, "en")
        assert localized, name
        assert not CYRILLIC.search(localized), f"{name} → {localized!r}"


def test_game_links_carry_the_users_language():
    """Ссылку строит хендлер, где язык уже выставлен, — значит передать его в
    статическую страницу ничего не стоит."""
    from handlers import game

    with i18n.use_lang("en"):
        assert game.game_url().endswith("?lang=en")
        assert game.squad_url().endswith("?lang=en")
    with i18n.use_lang("ru"):
        assert game.game_url().endswith("?lang=ru")


def test_game_pages_prefer_the_explicit_lang_over_the_client_guess():
    """Порядок важен: ?lang= — это осознанный выбор пользователя, а
    language_code клиента лишь догадка. Проверяем форму записи, потому что
    исполнить JS в тестах нечем."""
    for name in ("game.html", "game_squad.html"):
        source = pathlib.Path(name).read_text(encoding="utf-8")
        assert "URLSearchParams(window.location.search).get('lang')" in source, name
        url_at = source.index("URLSearchParams(window.location.search).get('lang')")
        client_at = source.index("initDataUnsafe?.user?.language_code")
        assert url_at < client_at, f"{name}: догадка по клиенту стоит раньше явного выбора"
