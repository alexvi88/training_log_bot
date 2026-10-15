"""Дневника еды больше нет нигде: ни в боте, ни у тренера, ни в MCP, ни в /v1.

Старые строки `food_entries` живут (выгрузка, «Удалить аккаунт»), но ни один
путь в коде их больше не пополняет. Тесты здесь — опись того, что ушло, чтобы
кусок дневника не вернулся молча одной правкой (кнопка в меню, команда в
«/»-меню, инструмент тренера, ветка отката).
"""

import json
import re
from pathlib import Path

import legacy_food
import pytest

import ai_undo
import i18n
import keyboards
import main

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.asyncio


def _production_sources() -> list[Path]:
    return [
        p for p in ROOT.rglob("*.py")
        if "tests" not in p.parts and ".venv" not in p.parts and "scripts" not in p.parts
    ]


async def test_nothing_in_the_code_writes_food_entries():
    """Таблица осталась ради старых записей — но писать в неё нечему."""
    writes = re.compile(r"(INSERT\s+INTO|UPDATE)\s+food_entries", re.IGNORECASE)
    offenders = [p.name for p in _production_sources() if writes.search(p.read_text())]
    # Слияние аккаунтов переносит старые строки владельцу общим циклом по
    # таблицам (db._MERGE_CONTENT_TABLES) — это перенос, а не новая запись.
    assert offenders == []


async def test_bot_has_no_food_diary_router_or_command():
    assert not (ROOT / "handlers" / "food_diary.py").exists()
    assert "food_diary" not in (ROOT / "main.py").read_text()
    for lang in ("ru", "en"):
        assert "food_diary" not in {c.command for c in main._public_commands(lang)}


async def test_settings_keyboard_has_no_macros_toggle():
    kb = keyboards.settings_keyboard("kg", "epley", True, True, True)
    texts = [b.text for row in kb.inline_keyboard for b in row]
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert not any("КБЖУ" in t or "ккал" in t for t in texts)
    assert not any("macro" in (cb or "") or "food" in (cb or "") for cb in callbacks)


async def test_catalogs_have_no_food_diary_strings():
    for lang in ("ru", "en"):
        catalog = json.loads((ROOT / "locales" / f"{lang}.json").read_text())
        dead = [
            k for k in catalog
            if k.startswith(("food.", "limit.food.", "achievement.food7."))
            or k in {"bot.commands.food_diary", "btn.food_diary", "btn.kcal_goal"}
            or k.startswith("ai.tool_status.") and "food" in k
        ]
        assert dead == [], (lang, dead)
        # Остались ровно два «еды»-ключа: 410 для старой сборки и строка
        # README выгрузки, где лежат старые записи.
        assert "api.error.food_gone" in catalog
        assert "export.readme.food" in catalog


async def test_an_old_food_undo_button_does_nothing(fresh_db, user_id):
    """Кнопка отката под старым ответом тренера («↩️ Убрать из дневника» /
    «↩️ Вернуть») — честное «не вышло», без записи и без удаления."""
    entry_id = await legacy_food.insert_food_row(user_id, "2026-08-08", "каша")

    with i18n.use_lang("ru"):
        removed = await ai_undo.apply(user_id, {"kind": "food", "id": entry_id})
        restored = await ai_undo.apply(
            user_id, {"kind": "food_restore", "eaten_on": "2026-08-08", "description": "омлет"}
        )

    assert removed is None and restored is None
    cur = await fresh_db.conn().execute(
        "SELECT description FROM food_entries WHERE telegram_id = ?", (user_id,)
    )
    assert [r["description"] for r in await cur.fetchall()] == ["каша"]
