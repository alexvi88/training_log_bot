"""Токены доступа по MCP (db.mcp_tokens): выдача, отзыв, перевыпуск.

Токен — единственное, что отделяет чужого человека от всей истории тренировок
пользователя, поэтому проверяется не «функция вернула строку», а границы: старый
токен после перевыпуска мёртв, отозванный мёртв, чужой ничего не открывает.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def test_issue_returns_token_that_resolves_to_owner(fresh_db, user_id):
    token = await fresh_db.issue_mcp_token(user_id)
    assert token
    assert await fresh_db.resolve_mcp_token(token) == user_id


async def test_unknown_and_empty_tokens_resolve_to_nobody(fresh_db, user_id):
    await fresh_db.issue_mcp_token(user_id)
    assert await fresh_db.resolve_mcp_token("не-тот-токен") is None
    assert await fresh_db.resolve_mcp_token("") is None


async def test_reissue_kills_the_previous_token(fresh_db, user_id):
    """Перевыпуск и есть отзыв: иначе «токен утёк, выпустил новый» оставляет
    утёкший в живых, и человек уверен, что закрылся."""
    old = await fresh_db.issue_mcp_token(user_id)
    new = await fresh_db.issue_mcp_token(user_id)
    assert new != old
    assert await fresh_db.resolve_mcp_token(old) is None
    assert await fresh_db.resolve_mcp_token(new) == user_id
    # Один живой токен на человека, а не история всех выданных.
    row = await fresh_db.get_mcp_token(user_id)
    assert row["token"] == new


async def test_revoke_closes_access(fresh_db, user_id):
    token = await fresh_db.issue_mcp_token(user_id)
    assert await fresh_db.revoke_mcp_token(user_id) is True
    assert await fresh_db.resolve_mcp_token(token) is None
    assert await fresh_db.get_mcp_token(user_id) is None
    # Второй отзыв уже нечего отзывать — но и падать не должен.
    assert await fresh_db.revoke_mcp_token(user_id) is False


async def test_token_of_one_user_never_resolves_to_another(fresh_db, user_id):
    other = await fresh_db.get_or_create_user(telegram_id=222, username="other")
    mine = await fresh_db.issue_mcp_token(user_id)
    theirs = await fresh_db.issue_mcp_token(other["telegram_id"])
    assert await fresh_db.resolve_mcp_token(mine) == user_id
    assert await fresh_db.resolve_mcp_token(theirs) == other["telegram_id"]


async def test_resolve_marks_last_use(fresh_db, user_id):
    """Отметка последнего обращения — единственный признак, по которому владелец
    заметит, что токеном пользуется кто-то ещё (её показывает экран /mcp)."""
    token = await fresh_db.issue_mcp_token(user_id)
    assert (await fresh_db.get_mcp_token(user_id))["last_used_at"] is None
    await fresh_db.resolve_mcp_token(token)
    assert (await fresh_db.get_mcp_token(user_id))["last_used_at"] is not None
