"""AI-перевод чужих названий упражнений (Hevy и т.п.) в имена нашего каталога
при импорте — на лету, а не по зашитому вручную словарю.

Hevy пишет по-английски («Bench Press (Barbell)»), и текстовый поиск по
русскому каталогу (db.search_exercise_templates) с этим не пересекается ни
разу — раньше такое имя всегда уходило в ручное разрешение и создавалось
голым, без фото и описания техники, которые уже есть у совпадающего шаблона.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import ai_trainer
import handlers.csv_import as csv_import
from fsm import ImportFlow

pytestmark = pytest.mark.asyncio


def _fake_client(content: str):
    message = SimpleNamespace(content=content)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response)))
    )


# ---------- ai_trainer.match_exercise_names_to_catalog ----------


async def test_matches_confident_pairs_and_ignores_the_rest(monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = _fake_client(json.dumps({
        "matches": [
            {"import_name": "Bench Press (Barbell)", "catalog_name": "Жим штанги лёжа"},
            {"import_name": "Running", "catalog_name": "Не знаю что"},  # не в каталоге
        ]
    }))
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    result = await ai_trainer.match_exercise_names_to_catalog(
        1, ["Bench Press (Barbell)", "Running"]
    )

    assert result == {"Bench Press (Barbell)": "Жим штанги лёжа"}


async def test_hallucinated_import_name_is_dropped(monkeypatch):
    """Модель отвечает по своей схеме, но не обязана ограничиться тем, что
    реально прислали — имя не из запроса ничем не лучше выдумки."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = _fake_client(json.dumps({
        "matches": [{"import_name": "Something Else", "catalog_name": "Жим штанги лёжа"}]
    }))
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    result = await ai_trainer.match_exercise_names_to_catalog(1, ["Bench Press (Barbell)"])

    assert result == {}


async def test_returns_empty_when_not_configured(monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)

    result = await ai_trainer.match_exercise_names_to_catalog(1, ["Bench Press (Barbell)"])

    assert result == {}


async def test_returns_empty_on_provider_error(monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def boom(**kwargs):
        raise RuntimeError("provider down")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom)))
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    result = await ai_trainer.match_exercise_names_to_catalog(1, ["Bench Press (Barbell)"])

    assert result == {}


async def test_returns_empty_for_unparsable_response(monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = _fake_client("не json вовсе")
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    result = await ai_trainer.match_exercise_names_to_catalog(1, ["Bench Press (Barbell)"])

    assert result == {}


# ---------- сквозной путь импорта ----------


def _message(user_id: int, filename: str, raw: bytes):
    message = MagicMock()
    message.document = SimpleNamespace(file_name=filename)
    message.from_user = SimpleNamespace(id=user_id, username="tester")
    message.reply = AsyncMock()
    message.answer = AsyncMock()
    message.bot = MagicMock()
    message.bot.download = AsyncMock(return_value=SimpleNamespace(read=lambda: raw))
    return message


async def test_aliased_exercise_keeps_its_own_name_but_links_template_media(
    fresh_db, user_id, monkeypatch
):
    """Совпавшее по смыслу имя не переименовывается в русское название каталога
    — человек не должен терять привычное имя из своей истории только потому,
    что оно нашлось в каталоге на другом языке. Фото и описание техники при
    этом подтягиваются от шаблона через original_name (тот же rename-proof
    ключ, что и у обычного форка)."""
    db = fresh_db
    import exercise_media

    async def fake_match(uid, names):
        assert names == ["Bench Press (Barbell)"]
        return {"Bench Press (Barbell)": "Жим штанги лёжа"}

    monkeypatch.setattr(ai_trainer, "match_exercise_names_to_catalog", fake_match)

    raw = (
        b'"title","start_time","end_time","description","exercise_title","superset_id",'
        b'"exercise_notes","set_index","set_type","weight_kg","reps","distance_km",'
        b'"duration_seconds","rpe"\n'
        b'"Push","7 Aug 2026, 08:27","7 Aug 2026, 08:28","","Bench Press (Barbell)",,'
        b'"",0,"normal",100,8,,,\n'
    )
    message = _message(user_id, "workout_data.csv", raw)
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))

    await csv_import.import_file_received(message, state)

    # Никакого ручного разрешения — сразу подтверждение.
    assert await state.get_state() == ImportFlow.confirming
    # Своё оригинальное имя осталось как было — не подменилось русским каталожным.
    assert await db.find_exercise_by_name(user_id, "Жим штанги лёжа") is None
    ex = await db.find_exercise_by_name(user_id, "Bench Press (Barbell)")
    assert ex is not None
    assert ex["is_template"] == 0
    assert exercise_media.catalog_key(ex) == "Жим штанги лёжа"


async def test_matching_unresolved_names_sends_a_progress_message_first(
    fresh_db, user_id, monkeypatch
):
    """Модель отвечает не мгновенно — импорт из Hevy почти всегда идёт этим
    путём (английские имена против русского каталога), и без знака, что файл
    вообще читается, выглядит как зависший бот."""
    async def fake_match(uid, names):
        return {"Bench Press (Barbell)": "Жим штанги лёжа"}

    monkeypatch.setattr(ai_trainer, "match_exercise_names_to_catalog", fake_match)

    raw = (
        b'"title","start_time","end_time","description","exercise_title","superset_id",'
        b'"exercise_notes","set_index","set_type","weight_kg","reps","distance_km",'
        b'"duration_seconds","rpe"\n'
        b'"Push","7 Aug 2026, 08:27","7 Aug 2026, 08:28","","Bench Press (Barbell)",,'
        b'"",0,"normal",100,8,,,\n'
    )
    message = _message(user_id, "workout_data.csv", raw)
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))

    await csv_import.import_file_received(message, state)

    texts = [call.args[0] for call in message.answer.await_args_list]
    assert any("Сверяю названия" in t for t in texts[:-1]), texts


async def test_alias_does_not_relink_an_already_existing_exercise(fresh_db, user_id, monkeypatch):
    """Имя уже есть в списке пользователя под своей историей — импорт не должен
    тихо перепривязывать его медиа к шаблону задним числом."""
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Спина")
    own_id = await db.create_exercise(user_id, "Bench Press (Barbell)", gid)

    ex_id = await db.create_exercise_matching_catalog_name(
        user_id, "Bench Press (Barbell)", "Жим штанги лёжа"
    )

    assert ex_id == own_id


async def test_unmatched_names_still_go_through_manual_resolve(fresh_db, user_id, monkeypatch):
    async def fake_match(uid, names):
        return {}

    monkeypatch.setattr(ai_trainer, "match_exercise_names_to_catalog", fake_match)

    raw = (
        b'"title","start_time","end_time","description","exercise_title","superset_id",'
        b'"exercise_notes","set_index","set_type","weight_kg","reps","distance_km",'
        b'"duration_seconds","rpe"\n'
        b'"Push","7 Aug 2026, 08:27","7 Aug 2026, 08:28","","Some Unknown Move",,'
        b'"",0,"normal",100,8,,,\n'
    )
    message = _message(user_id, "workout_data.csv", raw)
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))

    await csv_import.import_file_received(message, state)

    from fsm import ResolveFlow
    assert await state.get_state() == ResolveFlow.picking
