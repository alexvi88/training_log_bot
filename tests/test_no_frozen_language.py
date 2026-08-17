"""Текст не застывает на языке того, кто первым дёрнул модуль.

Это самая частая ошибка всей локализации: процесс один на всех пользователей, а
язык известен только в момент рендера. Значение, разрешённое в строку РАНЬШЕ —
на импорте модуля, при определении функции, при `from X import Y` — навсегда
запоминает язык, который был активен тогда, и все остальные видят чужой.

За время работы она встретилась шесть раз в шести разных обличьях, и каждое
лечится по-своему:

1. Поля dataclass в каталоге достижений и званий → свойства
   (achievements.Achievement, analytics.Rank).
2. Модульные константы, читаемые снаружи как атрибуты → __getattr__ модуля,
   PEP 562 (formatting.E1RM_HINT и соседи, handlers.ai_trainer.INTRO_TEXT).
3. Подписи reply-кнопок, по тексту которых ловятся нажатия → объект, сравнивающий
   себя со всеми языками сразу (keyboards.BTN_*).
4. `from X import CONST` на верхнем уровне — связывает ЗНАЧЕНИЕ один раз, и
   никакой __getattr__ отдающего модуля уже не поможет → квалифицированный
   доступ в момент использования (handlers/persistent_menu.py).
5. Значение по умолчанию у параметра функции — вычисляется один раз при
   определении функции, худший случай: не спасает даже __getattr__ →
   сентинел None и разрешение при вызове (progress_ui).
6. Пулы вариантов, собранные на импорте → выбор пула по текущему языку
   (push_texts, running_texts).

Тест держит все шесть: каждое значение обязано различаться между ru и en в ОДНОМ
процессе. Новый способ заморозить текст, скорее всего, попадёт в один из этих
шести шаблонов — тогда его поймает соответствующая проверка.
"""

import pathlib

import achievements
import analytics
import formatting
import i18n
import keyboards
import progress_ui
import push_texts
import running_texts
from handlers import ai_trainer as ai_trainer_handlers


def _both_languages(get):
    with i18n.use_lang("ru"):
        ru = get()
    with i18n.use_lang("en"):
        en = get()
    return ru, en


def test_achievement_catalog_is_not_frozen():
    """CATALOG собирается один раз на импорт — значит title/description обязаны
    разрешаться при обращении, а не быть полями."""
    first = achievements.CATALOG[0]
    ru, en = _both_languages(lambda: first.title)
    assert ru != en, f"название достижения застыло: {ru!r}"
    ru_d, en_d = _both_languages(lambda: first.description)
    assert ru_d != en_d


def test_rank_ladder_is_not_frozen():
    rank = analytics.RANKS[-1]
    ru, en = _both_languages(lambda: rank.name)
    assert ru != en, f"название звания застыло: {ru!r}"


def test_module_level_constants_are_not_frozen():
    """Читаются снаружи как атрибуты (formatting.E1RM_HINT), поэтому лечатся
    __getattr__ модуля, а не превращением в функцию."""
    for name in ("E1RM_HINT", "UNGROUPED_LABEL", "MENU_LIFTS_NOTE"):
        ru, en = _both_languages(lambda n=name: getattr(formatting, n))
        assert ru != en, f"formatting.{name} застыла: {ru!r}"


def test_ai_trainer_intro_is_not_frozen_through_qualified_access():
    """handlers/persistent_menu.py читает это через модуль, а НЕ через
    `from ... import INTRO_TEXT`: второе связало бы значение на импорте, и
    __getattr__ отдающего модуля уже не позвался бы никогда."""
    for name in ("INTRO_TEXT", "RESUME_TEXT"):
        ru, en = _both_languages(lambda n=name: getattr(ai_trainer_handlers, n))
        assert ru != en, f"handlers.ai_trainer.{name} застыла: {ru!r}"


def test_persistent_menu_does_not_bind_the_text_at_import():
    """Прямая проверка формы записи, а не следствия: `from X import CONST` на
    верхнем уровне невозможно вылечить со стороны X, поэтому запрещаем сам приём.
    """
    source = pathlib.Path("handlers/persistent_menu.py").read_text(encoding="utf-8")
    assert "from handlers.ai_trainer import INTRO_TEXT" not in source
    assert "ai_trainer_handlers.INTRO_TEXT" in source


def test_reply_button_labels_match_every_language_at_once():
    """Нижняя клавиатура у сменившего язык ещё показывает прежние подписи, и его
    нажатие обязано сработать — поэтому сравнение знает оба языка сразу."""
    for button in (keyboards.BTN_MENU, keyboards.BTN_WORKOUT, keyboards.BTN_AI):
        ru_label = i18n.t_in("ru", button._key)
        en_label = i18n.t_in("en", button._key)
        assert ru_label != en_label, f"подписи совпали, тест бессмысленен: {ru_label!r}"
        assert button == ru_label
        assert button == en_label


def test_progress_screen_title_is_not_a_frozen_default_argument():
    """Худший случай ловушки: дефолт параметра вычисляется при определении
    функции. Проверяем и результат, и что в подписи не осталось значения."""
    ru, en = _both_languages(lambda: progress_ui.render(0, ["a"], 0))
    assert ru != en, "заголовок экрана прогресса застыл в подписи функции"
    import inspect

    for func in (progress_ui.render, progress_ui.initial_text, progress_ui.run_progress):
        default = inspect.signature(func).parameters["title"].default
        assert default is None, f"{func.__name__}: заголовок снова вписан в дефолт ({default!r})"


def test_variant_pools_follow_the_current_language():
    """Пулы собираются на импорте, но ВЫБОР пула обязан идти по текущему языку.

    Проверяем сами пулы, а не pick_text: он асинхронный и ходит в базу за
    ротацией, а объект корутины правдив сам по себе — такой assert прошёл бы, ни
    разу не выполнив функцию.
    """
    ru_pool = push_texts.TEXTS_BY_LANG["ru"][push_texts.SKIP_3]
    en_pool = push_texts.TEXTS_BY_LANG["en"][push_texts.SKIP_3]
    assert ru_pool and en_pool
    assert ru_pool != en_pool
    # Размеры равны намеренно: индекс ротации лежит в базе, и после смены языка
    # посреди «мешка» он обязан указывать на существующий вариант.
    assert len(ru_pool) == len(en_pool)

    ru_thinking, en_thinking = _both_languages(lambda: list(running_texts.pool_for("")))
    assert ru_thinking != en_thinking, "пул плейсхолдеров не следует за языком"
