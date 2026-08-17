"""Язык интерфейса пользователя: дефолт, запись, санитайз мусора и миграция
старой базы без колонки `lang` (см. db.SCHEMA и db._migrate_schema)."""
import aiosqlite

import db as db_module


async def test_new_user_defaults_to_ru(user_id, fresh_db):
    row = await fresh_db.get_user(user_id)
    assert row["lang"] == "ru"


async def test_set_user_lang_en_reads_back(user_id, fresh_db):
    await fresh_db.set_user_lang(user_id, "en")
    row = await fresh_db.get_user(user_id)
    assert row["lang"] == "en"


async def test_garbage_value_is_coerced_to_ru(user_id, fresh_db):
    await fresh_db.set_user_lang(user_id, "en")  # чтобы убедиться, что мусор не просто «не менял» значение
    await fresh_db.set_user_lang(user_id, "de")
    row = await fresh_db.get_user(user_id)
    assert row["lang"] == "ru"

    await fresh_db.set_user_lang(user_id, "en")
    await fresh_db.set_user_lang(user_id, "")
    row = await fresh_db.get_user(user_id)
    assert row["lang"] == "ru"


async def test_migration_adds_lang_column_with_ru_default(tmp_path):
    """Старая база без колонки `lang` — минимально нужный набор колонок users
    (те, у которых нет ALTER-миграции ниже по файлу и без которых init_db не
    поднимется). После init_db колонка должна появиться со значением 'ru' у
    уже существующей строки."""
    path = tmp_path / "legacy.sqlite3"
    async with aiosqlite.connect(str(path)) as conn:
        await conn.executescript(
            """
            CREATE TABLE users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                created_at TEXT NOT NULL,
                unit TEXT NOT NULL DEFAULT 'kg',
                e1rm_formula TEXT NOT NULL DEFAULT 'epley',
                show_extra_stats INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO users (telegram_id, username, created_at)
            VALUES (4242, 'tester', '2026-01-01T00:00:00');
            """
        )
        await conn.commit()

    await db_module.init_db(str(path))
    try:
        user = await db_module.get_user(4242)
        assert user is not None
        assert user["lang"] == "ru"
    finally:
        await db_module.close_db()


def test_whitelist_matches_i18n_supported():
    """Белый список в db.set_user_lang продублирован из i18n.SUPPORTED намеренно
    (db не тянет в себя слой представления) — этот тест держит дубль в согласии.
    Иначе новый язык, добавленный в i18n, молча приводился бы к 'ru' при записи.
    """
    import inspect

    import i18n

    source = inspect.getsource(db_module.set_user_lang)
    for lang in i18n.SUPPORTED:
        assert f'"{lang}"' in source, f"язык {lang!r} есть в i18n.SUPPORTED, но не в db.set_user_lang"


async def test_new_user_language_is_guessed_from_the_client(fresh_db):
    """Догадка живёт в get_or_create_user, а не у вызывающих.

    Аккаунт заводится из восьми разных мест — не только /start, но и
    ссылка-приглашение, дневник еды, вес, сообщество, игра, MCP, нижняя
    клавиатура. Правило «угадай язык новичку», повторённое восемь раз, забудется
    в девятом: ровно так и вышло — язык выставлял только /start, а пришедший по
    ссылке навсегда оставался на русском, потому что запись уже есть и экран
    выбора языка ему больше не покажут.
    """
    row = await fresh_db.get_or_create_user(910001, "en_user", "en-US")
    assert row["lang"] == "en"
    row = await fresh_db.get_or_create_user(910002, "ru_user", "ru")
    assert row["lang"] == "ru"
    # Незнакомый язык клиента — английский, как решает i18n.normalize.
    row = await fresh_db.get_or_create_user(910003, "de_user", "de")
    assert row["lang"] == "en"
    # Клиент языка не прислал — остаёмся на дефолте, а не гадаем.
    row = await fresh_db.get_or_create_user(910004, "silent", None)
    assert row["lang"] == "ru"


async def test_guess_never_overwrites_an_existing_choice(fresh_db):
    """Догадка применяется ТОЛЬКО при вставке. Иначе человек, выбравший русский
    на английском телефоне, получал бы английский обратно при каждом заходе в
    дневник еды."""
    await fresh_db.get_or_create_user(910005, "user", "en")
    await fresh_db.set_user_lang(910005, "ru")
    again = await fresh_db.get_or_create_user(910005, "user", "en")
    assert again["lang"] == "ru"


async def test_every_account_creation_site_passes_the_client_language():
    """Сторож против девятого места: если новый хендлер заведёт пользователя
    без языка клиента, догадка для него молча не сработает."""
    import pathlib
    import re

    offenders = []
    for path in sorted(pathlib.Path("handlers").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for call in re.finditer(r"get_or_create_user\(([^)]*)\)", source, re.S):
            if "language_code" not in call.group(1):
                offenders.append(f"{path}: {' '.join(call.group(1).split())[:70]}")
    assert not offenders, "заводят пользователя без языка клиента:\n" + "\n".join(offenders)
