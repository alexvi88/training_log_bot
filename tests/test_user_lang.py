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
