"""Языковая директива в системных промптах AI-тренера.

Хендлерный путь (лежит на i18n.get_lang() — middleware уже выставил язык)
проверяется прямо на _system_prompt(). Фоновый путь (нет апдейта, значит нет
контекста) проверяется отдельно: comment_on_workout и weekly_digest обязаны
взять язык из users.lang сами, а не молча унаследовать то, что случайно стоит
в контексте на момент вызова.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import ai_trainer
import config
import i18n

# asyncio_mode=auto (pytest.ini) — маркер async-тестам не нужен; часть тестов
# здесь синхронная (промпт — чистая функция), поэтому module-level pytestmark
# не ставим, чтобы не ловить предупреждение на них.


def _response(content=None):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _fake_client(responses):
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(side_effect=responses))
        )
    )


def test_ru_system_prompt_has_the_russian_directive_and_not_the_english_one():
    with i18n.use_lang("ru"):
        prompt = ai_trainer._system_prompt()
    assert "Отвечай ТОЛЬКО по-русски" in prompt
    assert "Reply ONLY in English" not in prompt


def test_en_system_prompt_has_the_english_directive_and_not_the_russian_one():
    with i18n.use_lang("en"):
        prompt = ai_trainer._system_prompt()
    assert "Reply ONLY in English" in prompt
    assert "Отвечай ТОЛЬКО по-русски" not in prompt


def test_language_tail_is_the_very_last_thing_in_the_prompt():
    with i18n.use_lang("en"):
        prompt = ai_trainer._system_prompt()
    tail = i18n.t_in("en", "ai.language_tail")
    assert prompt.endswith(tail)


def test_prompt_prefix_is_byte_identical_across_languages():
    """Гарантия кэша (см. CLAUDE.md, «Сколько это стоит»): всё, что стоит ДО
    языкового хвоста, должно быть одним и тем же набором байт для любого
    языка — иначе кэшированный префикс расщепляется на ru/en и переплачивают
    все, а не только те, у кого язык не дефолтный.
    """
    with i18n.use_lang("ru"):
        ru_prompt = ai_trainer._system_prompt()
        ru_tail = i18n.t_in("ru", "ai.language_tail")
    with i18n.use_lang("en"):
        en_prompt = ai_trainer._system_prompt()
        en_tail = i18n.t_in("en", "ai.language_tail")

    ru_prefix = ru_prompt[: -len(ru_tail)]
    en_prefix = en_prompt[: -len(en_tail)]
    assert ru_prefix == en_prefix
    # И у обоих хвост приклеен через один и тот же разделитель, а не просто
    # где-то внутри промпта.
    assert ru_prompt == ru_prefix + ru_tail
    assert en_prompt == en_prefix + en_tail


async def test_workout_comment_uses_the_users_stored_language_not_the_context(
    fresh_db, user_id, monkeypatch
):
    """comment_on_workout запускается и из фонового таска сразу после финиша
    тренировки (handlers/workout._attach_ai_comment), где апдейта уже нет и
    полагаться на i18n.get_lang() из контекста нельзя. Явно оставляем
    контекст на ru, а пользователя делаем англоязычным — комментарий обязан
    выйти на английской директиве."""
    await fresh_db.set_user_lang(user_id, "en")
    workout_id = await fresh_db.create_workout(user_id)
    await fresh_db.finish_workout(workout_id)
    client = _fake_client([_response(content="Solid session.")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    with i18n.use_lang("ru"):  # контекст нарочно "неправильный"
        await ai_trainer.comment_on_workout(user_id, workout_id)

    system_content = client.chat.completions.create.await_args.kwargs["messages"][0]["content"]
    assert "Reply ONLY in English" in system_content
    assert "Отвечай ТОЛЬКО по-русски" not in system_content


async def test_weekly_digest_uses_the_users_stored_language_not_the_context(
    fresh_db, user_id, monkeypatch
):
    """weekly_digest — воскресная рассылка движка (engagement.py), которая
    крутит цикл по многим telegram_id вообще без апдейта. Тот же инвариант,
    что и у comment_on_workout: язык — из базы, а не из контекста."""
    await fresh_db.set_user_lang(user_id, "en")
    monkeypatch.setattr(config, "XAI_API_KEY", "test-key")
    client = _fake_client([_response(content="Solid week.")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    with i18n.use_lang("ru"):  # контекст нарочно "неправильный"
        await ai_trainer.weekly_digest(user_id)

    system_content = client.chat.completions.create.await_args.kwargs["messages"][0]["content"]
    assert "Reply ONLY in English" in system_content
    assert "Отвечай ТОЛЬКО по-русски" not in system_content
