"""Реестр локализации: какие модули уже переведены, какие никогда не будут, и
что ещё не решено. Это храповик для `tests/test_i18n_no_leaks.py`, а не просто
документация — сам файл не запрещает и не разрешает ничего, запрет утечек
живёт в тесте, здесь только источник правды о том, к какому модулю какое
правило применять.

Почему это важно как отдельный модуль, а не комментарий в тесте: список
`LOCALIZED` обязан РАСТИ по мере перевода экранов, и разработчик, переводящий
следующий модуль, должен одной строкой сюда его добавить — тест тут же начнёт
запрещать в нём кириллицу навсегда. Без реестра эта защита существовала бы
только «пока помним».

Три списка обязаны в сумме покрывать каждый .py-файл из ROOTS (см. ниже) ровно
один раз — за этим следит test_every_module_is_classified в тесте: новый файл,
про который никто не принял решение "переводим/не переводим", не должен
проскочить молча.

Область действия (ROOTS) — только продуктовый код (корень репозитория и
handlers/): тесты и scripts/ — это инструменты разработки, а не то, что видит
пользователь бота, поэтому в реестр их сознательно не включаем.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent

# Каталоги, из которых модули должны быть классифицированы. Тесты (tests/) и
# служебные скрипты (scripts/) сюда не входят: это код для разработчика, а не
# для атлета, и локализация продукта его не касается.
ROOTS: tuple[Path, ...] = (_ROOT, _ROOT / "handlers")


def discover_modules() -> list[str]:
    """Список модулей репозитория в области действия реестра — относительные
    пути со слэшем (`handlers/admin.py`), как они записаны в списках ниже.
    Рекурсии внутри ROOTS нет специально: `handlers/` — плоская директория, а
    новый вложенный пакет — это уже решение, которое стоит заметить руками, а
    не проглотить рекурсивным glob'ом.
    """
    modules: list[str] = []
    for root in ROOTS:
        for path in sorted(root.glob("*.py")):
            rel = path.relative_to(_ROOT).as_posix()
            modules.append(rel)
    return modules


# --- LOCALIZED --------------------------------------------------------------
#
# Модули, в которых ЦЕЛИКОМ (весь файл, а не отдельная функция) нет ни одного
# кириллического строкового литерала вне докстрингов/комментариев. Список
# только растёт: попадание модуля сюда — обещание, что новый русский текст в
# нём — баг, а не "ещё не успели".
#
# Пусто НЕ потому, что ничего не переведено: экран выбора языка уже в каталоге
# (locales/*.json, ключи screen.language.*) и рендерится через i18n.t_in. Но
# модули, которые его показывают — handlers/settings.py и keyboards.py — пока
# смешанные: помимо этого экрана в них полно ещё не переведённых настроек
# (единицы, формула e1RM, пуши и т.д.), так что весь файл целиком в LOCALIZED
# не попадает — это было бы враньём, и храповик сломался бы в первый же день
# следующей правки. Как только настройки в них дочистят целиком, впишите файл
# сюда одной строкой.
LOCALIZED: list[str] = []


# --- NEVER_LOCALIZED ---------------------------------------------------------
#
# Модули, у которых кириллица в коде — не долг, а осознанное решение навсегда,
# у каждого — своя причина. Тест никак их не проверяет (ни на кириллицу, ни на
# что-либо ещё) — они просто выведены из-под локализации.
NEVER_LOCALIZED: dict[str, str] = {
    # Админские экраны и инструменты — аудитория один человек (ADMIN_ID),
    # переводить их означает переводить самому себе.
    "handlers/admin.py": "админ-панель бота, аудитория — один человек (ADMIN_ID)",
    "admin_tasks.py": "ежедневная админ-задача (статистика + бэкап), уходит только ADMIN_ID",
    "acquisition.py": "разбор источников трафика и воронка /growth — админская аналитика",
    "announcements.py": (
        "инструмент релизных рассылок: экран подтверждения видит только админ, "
        "а сам текст анонса — свободный ввод админа на каждый релиз, не литерал каталога"
    ),
    "activity_log.py": (
        "лог действий пользователя для админской аналитики (кто как жмёт кнопки), "
        "сами подписи не показываются атлету"
    ),
    # Модули без единого пользовательского текста: чистая логика, структуры
    # данных или инфраструктура — им либо нечего переводить, либо то, что в
    # них есть по кириллице, до Telegram-чата пользователя не долетает.
    "achievement_sync.py": "синхронизация ачивок с БД, не формирует текст",
    "chat_bottom.py": "учёт последнего сообщения в чате, не формирует текст",
    "config.py": "загрузка/валидация конфигурации при старте — лог для деплоя, не для чата",
    "fsm.py": "перечисление FSM-состояний (StatesGroup), текста не содержит",
    "fsm_storage.py": "движок хранения FSM-состояния на диске, не формирует текст",
    "state_scaffold.py": "сброс каркаса FSM между разделами, не формирует текст",
    "timeutil.py": "арифметика дат/времени в часовом поясе пользователя, не формирует текст",
    "i18n.py": (
        "сам модуль локализации: кириллица в нём — только внутренние WARNING/"
        "исключения для разработчика (битый ключ каталога, битый ICU), пользователь их не видит"
    ),
    "handlers/__init__.py": "пустой пакетный файл",
    # Сам реестр — служебный инструмент для разработчика, а не экран бота.
    "i18n_coverage.py": "реестр локализации — служебный инструмент, не показывается пользователю",
}


# --- TODO ---------------------------------------------------------------
#
# Всё остальное: реально пользовательский текст, который ещё не переведён.
# Список существует, чтобы новый файл нельзя было завести молча — он обязан
# попасть либо сюда, либо (после решения) в LOCALIZED/NEVER_LOCALIZED.
# Дальнейшая сортировка внутри TODO не нужна — это рабочий список, а не
# приоритет; приоритет перевода — отдельный разговор.
TODO: list[str] = [
    "achievements.py",
    "ai_limits.py",
    "ai_trainer.py",
    "analytics.py",
    "bot_profile.py",
    "charts.py",
    "db.py",
    "engagement.py",
    "exercise_descriptions.py",
    "exercise_media.py",
    "exercise_mentions.py",
    "formatting.py",
    "game_server.py",
    "keyboards.py",
    "main.py",
    "mcp_oauth.py",
    "mcp_server.py",
    "parser.py",
    "program_mentions.py",
    "progress_ui.py",
    "push_texts.py",
    "running_texts.py",
    "search_terms.py",
    "seed_data.py",
    "ui.py",
    "video_analysis.py",
    "view_builder.py",
    "voice_parse.py",
    "handlers/ai_trainer.py",
    "handlers/backfill.py",
    "handlers/bodyweight.py",
    "handlers/community.py",
    "handlers/csv_import.py",
    "handlers/donate.py",
    "handlers/edit_workout.py",
    "handlers/exercise_resolve.py",
    "handlers/exercises.py",
    "handlers/factcheck.py",
    "handlers/fallback.py",
    "handlers/feedback.py",
    "handlers/food_diary.py",
    "handlers/game.py",
    "handlers/history.py",
    "handlers/mcp_access.py",
    "handlers/persistent_menu.py",
    "handlers/routines.py",
    "handlers/settings.py",
    "handlers/sharing.py",
    "handlers/workout.py",
]


# --- подсчёт кириллических литералов (как scripts/i18n_extract.py) ----------
#
# Своя маленькая копия, а не импорт scripts.i18n_extract: тот модуль — CLI-
# утилита с argparse в module-level `main()`, тянуть её ради двух функций
# лишний импорт, который к тому же зависит от scripts/ (вне ROOTS реестра).

_CYRILLIC = set("АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя")


def has_cyrillic(text: str) -> bool:
    return any(ch in _CYRILLIC for ch in text)


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    owners = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if isinstance(node, owners):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(
                first.value.value, str
            ):
                ids.add(id(first.value))
    return ids


def cyrillic_literals(module_rel_path: str) -> list[tuple[int, str]]:
    """(номер строки, литерал) для кириллических строковых констант модуля,
    докстринги пропущены. f-строки берутся целиком по своим Constant-кускам
    (совпадает с тем, что реально попадёт в скомпилированный вывод)."""
    source = (_ROOT / module_rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=module_rel_path)
    doc_ids = _docstring_ids(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                if (
                    isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                    and has_cyrillic(part.value)
                ):
                    found.append((node.lineno, part.value))
            continue
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in doc_ids
            and has_cyrillic(node.value)
        ):
            found.append((node.lineno, node.value))
    return found


def report() -> None:
    """Счётчик прогресса локализации: сколько модулей переведено/в очереди, и
    сколько кириллических литералов ещё осталось всего (по модулям из TODO —
    в LOCALIZED их по определению 0, а NEVER_LOCALIZED не считаем, это не долг)."""
    all_modules = set(discover_modules())
    classified = set(LOCALIZED) | set(NEVER_LOCALIZED) | set(TODO)
    unclassified = sorted(all_modules - classified)
    stray = sorted(classified - all_modules)  # в списках, но файла уже нет

    total_leftover = 0
    per_module: list[tuple[str, int]] = []
    for mod in sorted(TODO):
        if mod not in all_modules:
            continue
        n = len(cyrillic_literals(mod))
        total_leftover += n
        per_module.append((mod, n))

    print("=== i18n coverage ===")
    print(f"LOCALIZED:      {len(LOCALIZED)}")
    print(f"NEVER_LOCALIZED:{len(NEVER_LOCALIZED):>4}")
    print(f"TODO:           {len(TODO)}")
    print(f"кириллических литералов осталось (в TODO-модулях): {total_leftover}")
    if unclassified:
        print(f"\nНЕ КЛАССИФИЦИРОВАНЫ ({len(unclassified)}) — храповик должен был это поймать:")
        for m in unclassified:
            print(f"  {m}")
    if stray:
        print(f"\nВ СПИСКАХ, НО ФАЙЛА НЕТ ({len(stray)}) — подчистить реестр:")
        for m in stray:
            print(f"  {m}")
    print("\nТоп-10 модулей TODO по числу кириллических литералов:")
    for mod, n in sorted(per_module, key=lambda x: -x[1])[:10]:
        print(f"  {n:5d}  {mod}")


if __name__ == "__main__":
    report()
