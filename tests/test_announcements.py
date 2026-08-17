"""Разовая релизная рассылка (announcements.py): сначала админу на проверку, потом всем."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError

import announcements
import config
import db as db_module
from handlers import admin

pytestmark = pytest.mark.asyncio

ADMIN_ID = 999


class FakeBot:
    """Считает отправки и умеет падать на конкретном получателе."""

    def __init__(self, fail_for: dict[int, Exception] | None = None):
        self.sent: list[tuple[int, str]] = []
        self.fail_for = fail_for or {}

    async def send_message(self, chat_id: int, text: str, **kwargs):
        return await self._record(chat_id, text)

    async def send_photo(self, chat_id: int, caption: str, **kwargs):
        return await self._record(chat_id, caption)

    async def _record(self, chat_id: int, text: str):
        exc = self.fail_for.get(chat_id)
        if exc is not None:
            raise exc
        self.sent.append((chat_id, text))
        return SimpleNamespace(photo=None)

    def texts_to(self, chat_id: int) -> list[str]:
        return [text for cid, text in self.sent if cid == chat_id]


def _announcement() -> announcements.Announcement:
    return announcements.Announcement(
        key="test_release",
        text="ПРИВЕТ АТЛЕТ! Новая штука.",
        buttons=[("🤖 Собрать программу", "ai:buildprog")],
        image=None,
    )


async def _users(db, *ids: int) -> None:
    for telegram_id in ids:
        await db.get_or_create_user(telegram_id=telegram_id, username=f"u{telegram_id}")


def _callback(user_id: int, data: str, bot):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.edit_text = AsyncMock(return_value=message)
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="admin", language_code=None)
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    callback.bot = bot
    return callback


def _message(user_id: int, bot):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="admin", language_code=None)
    message.chat = SimpleNamespace(id=user_id)
    message.answer = AsyncMock()
    message.bot = bot
    return message


@pytest.fixture(autouse=True)
def _admin(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", ADMIN_ID)
    monkeypatch.setattr(config, "ANNOUNCEMENTS_ENABLED", True)


# ---------- шаг 1: превью админу ----------


async def test_startup_shows_the_release_to_admin_and_nobody_else(fresh_db, monkeypatch):
    await _users(fresh_db, ADMIN_ID, 1, 2)
    monkeypatch.setattr(announcements.asyncio, "sleep", AsyncMock())
    ann = _announcement()
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])

    bot = FakeBot()
    await announcements.run_pending_announcements(bot)

    assert {chat_id for chat_id, _ in bot.sent} == {ADMIN_ID}
    # Сам анонс, затем вопрос «разослать?».
    assert bot.texts_to(ADMIN_ID)[0] == ann.text
    assert "Разослать 2" in bot.texts_to(ADMIN_ID)[1]
    assert await db_module.get_announcement_status(ann.key) == announcements.STATUS_PREVIEW


async def test_restart_while_waiting_does_not_nag_the_admin_again(fresh_db, monkeypatch):
    await _users(fresh_db, ADMIN_ID, 1)
    monkeypatch.setattr(announcements.asyncio, "sleep", AsyncMock())
    ann = _announcement()
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])
    await announcements.run_pending_announcements(FakeBot())

    bot = FakeBot()
    await announcements.run_pending_announcements(bot)

    assert bot.sent == []


async def test_admin_copy_is_not_sent_twice(fresh_db, monkeypatch):
    """Превью — это и есть экземпляр релиза для админа."""
    await _users(fresh_db, ADMIN_ID, 1)
    ann = _announcement()
    await announcements.send_preview(FakeBot(), ann)

    bot = FakeBot()
    await announcements.send_announcement(bot, ann)

    assert [chat_id for chat_id, _ in bot.sent] == [1]


async def test_no_admin_means_no_announcement_at_all(fresh_db, monkeypatch):
    await _users(fresh_db, 1, 2)
    monkeypatch.setattr(config, "ADMIN_ID", None)
    monkeypatch.setattr(announcements.asyncio, "sleep", AsyncMock())
    ann = _announcement()
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])

    bot = FakeBot()
    await announcements.run_pending_announcements(bot)

    assert bot.sent == []
    assert await db_module.get_announcement_status(ann.key) is None


# ---------- шаг 2: решение админа ----------


async def test_approve_button_sends_to_everyone(fresh_db, monkeypatch):
    await _users(fresh_db, ADMIN_ID, 1, 2)
    ann = _announcement()
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])
    bot = FakeBot()
    await announcements.send_preview(bot, ann)

    callback = _callback(ADMIN_ID, f"admin:ann:go:{ann.key}", bot)
    await admin.announcement_approve(callback)
    await announcements.deliver_and_report(bot, ann)

    assert {chat_id for chat_id, _ in bot.sent if chat_id != ADMIN_ID} == {1, 2}
    assert await db_module.get_announcement_status(ann.key) == announcements.STATUS_APPROVED


async def test_decline_button_keeps_the_release_unsent(fresh_db, monkeypatch):
    await _users(fresh_db, ADMIN_ID, 1)
    ann = _announcement()
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])
    monkeypatch.setattr(announcements.asyncio, "sleep", AsyncMock())
    bot = FakeBot()
    await announcements.send_preview(bot, ann)

    await admin.announcement_decline(_callback(ADMIN_ID, f"admin:ann:no:{ann.key}", bot))
    assert await db_module.get_announcement_status(ann.key) == announcements.STATUS_DECLINED

    # И следующий старт бота его не воскрешает.
    after_restart = FakeBot()
    await announcements.run_pending_announcements(after_restart)
    assert after_restart.sent == []


async def test_only_admin_can_approve(fresh_db, monkeypatch):
    await _users(fresh_db, ADMIN_ID, 1)
    ann = _announcement()
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])
    bot = FakeBot()

    await admin.announcement_approve(_callback(1, f"admin:ann:go:{ann.key}", bot))

    assert bot.sent == []
    assert await db_module.get_announcement_status(ann.key) is None


async def test_second_tap_does_not_start_a_second_broadcast(fresh_db, monkeypatch):
    await _users(fresh_db, ADMIN_ID, 1)
    ann = _announcement()
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])
    bot = FakeBot()
    announcements._sending.add(ann.key)
    try:
        callback = _callback(ADMIN_ID, f"admin:ann:go:{ann.key}", bot)
        await admin.announcement_approve(callback)
    finally:
        announcements._sending.discard(ann.key)

    assert bot.sent == []
    callback.answer.assert_awaited_with("Уже рассылаю.")


async def test_concurrent_deliveries_do_not_double_send(fresh_db, monkeypatch):
    """Regression test: the startup task (run_pending_announcements) and the
    admin's own «Разослать всем» button both eventually call
    deliver_and_report. Racing them used to send every recipient the release
    twice — both reads of list_announcement_recipients happened before either
    write of record_push. The guard now lives inside deliver_and_report
    itself, shared by both callers, so a second concurrent call is a no-op."""
    await _users(fresh_db, ADMIN_ID, 1, 2)
    ann = _announcement()
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])
    await announcements.send_preview(FakeBot(), ann)
    await db_module.set_announcement_status(ann.key, announcements.STATUS_APPROVED)

    import asyncio

    bot = FakeBot()
    results = await asyncio.gather(
        announcements.deliver_and_report(bot, ann),
        announcements.deliver_and_report(bot, ann),
    )

    delivered = [chat_id for chat_id, _ in bot.sent if chat_id != ADMIN_ID]
    assert sorted(delivered) == [1, 2]
    assert (0, 0, 0) in results


async def test_restart_mid_broadcast_finishes_the_rest(fresh_db, monkeypatch):
    """Одобренная рассылка после перезапуска идёт дальше, а не с начала."""
    await _users(fresh_db, ADMIN_ID, 1, 2)
    ann = _announcement()
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])
    monkeypatch.setattr(announcements.asyncio, "sleep", AsyncMock())
    await announcements.send_preview(FakeBot(), ann)
    await db_module.set_announcement_status(ann.key, announcements.STATUS_APPROVED)
    # Первому успели отправить до падения контейнера.
    await db_module.record_push(1, ann.key, ann.text, "2026-08-08")

    bot = FakeBot()
    await announcements.run_pending_announcements(bot)

    assert [chat_id for chat_id, _ in bot.sent if chat_id != ADMIN_ID] == [2]


async def test_rewritten_text_comes_back_for_a_second_look(fresh_db, monkeypatch):
    """Переписали анонс после показа — админ увидит новую редакцию, а не узнает
    о ней из рассылки."""
    await _users(fresh_db, ADMIN_ID, 1)
    monkeypatch.setattr(announcements.asyncio, "sleep", AsyncMock())
    ann = _announcement()
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])
    await announcements.run_pending_announcements(FakeBot())

    ann.text = "ПРИВЕТ АТЛЕТ! А вот теперь по-другому."
    bot = FakeBot()
    await announcements.run_pending_announcements(bot)

    assert bot.texts_to(ADMIN_ID)[0] == ann.text


async def test_preview_shown_before_fingerprints_existed_comes_back_once(fresh_db, monkeypatch):
    """Анонс, повисший на проверке до появления отпечатков, показывается заново.

    Живой случай: превью висело со старым текстом, отпечатка у записи не было,
    и переписанная редакция не пришла — хотя весь смысл правки был в том,
    чтобы прийти. Повторяется это ровно один раз: показ пишет отпечаток.
    """
    await _users(fresh_db, ADMIN_ID, 1)
    monkeypatch.setattr(announcements.asyncio, "sleep", AsyncMock())
    ann = _announcement()
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])
    await announcements.run_pending_announcements(FakeBot())
    # Так выглядит запись, сделанная версией без отпечатков.
    await fresh_db.conn().execute(
        "UPDATE announcement_state SET text_hash = NULL WHERE key = ?", (ann.key,)
    )
    await fresh_db.conn().commit()

    bot = FakeBot()
    await announcements.run_pending_announcements(bot)
    assert bot.texts_to(ADMIN_ID)[0] == ann.text

    quiet = FakeBot()
    await announcements.run_pending_announcements(quiet)
    assert quiet.sent == []


async def test_unchanged_text_stays_quiet_across_restarts(fresh_db, monkeypatch):
    await _users(fresh_db, ADMIN_ID, 1)
    monkeypatch.setattr(announcements.asyncio, "sleep", AsyncMock())
    ann = _announcement()
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])
    await announcements.run_pending_announcements(FakeBot())

    bot = FakeBot()
    await announcements.run_pending_announcements(bot)

    assert bot.sent == []


async def test_changed_button_also_needs_a_second_look(fresh_db, monkeypatch):
    """Кнопка — часть обещания: переехал callback, значит проверять заново."""
    await _users(fresh_db, ADMIN_ID, 1)
    monkeypatch.setattr(announcements.asyncio, "sleep", AsyncMock())
    ann = _announcement()
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])
    await announcements.run_pending_announcements(FakeBot())

    ann.buttons = [("🤖 Собрать программу", "ann:buildprog")]
    bot = FakeBot()
    await announcements.run_pending_announcements(bot)

    assert bot.texts_to(ADMIN_ID)[0] == ann.text


async def test_approved_release_ignores_a_later_text_edit(fresh_db, monkeypatch):
    """Одобренная рассылка дорассылается, а не уходит на новый круг проверки."""
    await _users(fresh_db, ADMIN_ID, 1, 2)
    monkeypatch.setattr(announcements.asyncio, "sleep", AsyncMock())
    ann = _announcement()
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])
    await announcements.send_preview(FakeBot(), ann)
    await db_module.set_announcement_status(ann.key, announcements.STATUS_APPROVED)

    ann.text = "ПРИВЕТ АТЛЕТ! Правка на ходу."
    bot = FakeBot()
    await announcements.run_pending_announcements(bot)

    assert {chat_id for chat_id, _ in bot.sent if chat_id != ADMIN_ID} == {1, 2}


# ---------- сама доставка ----------


async def test_reaches_everyone_once(fresh_db):
    await _users(fresh_db, 1, 2, 3)
    bot, ann = FakeBot(), _announcement()

    assert await announcements.send_announcement(bot, ann) == (3, 0, 0)
    assert [chat_id for chat_id, _ in bot.sent] == [1, 2, 3]


async def test_second_run_sends_nothing(fresh_db):
    await _users(fresh_db, 1, 2)
    ann = _announcement()
    await announcements.send_announcement(FakeBot(), ann)

    bot = FakeBot()
    assert await announcements.send_announcement(bot, ann) == (0, 0, 0)
    assert bot.sent == []


async def test_send_announcement_itself_does_not_gate_by_signup_time(fresh_db):
    """send_announcement() — низкоуровневая доставка без approved-отметки в
    announcement_state, так что cutoff по created_at тут не действует (см.
    test_new_signups_after_approval_do_not_get_an_old_release ниже за тем,
    как это устроено в реальном потоке через run_pending_announcements)."""
    await _users(fresh_db, 1)
    ann = _announcement()
    await announcements.send_announcement(FakeBot(), ann)

    await _users(fresh_db, 2)
    bot = FakeBot()
    assert await announcements.send_announcement(bot, ann) == (1, 0, 0)
    assert [chat_id for chat_id, _ in bot.sent] == [2]


async def test_new_signups_after_approval_do_not_get_an_old_release(fresh_db, monkeypatch):
    """Живой прогон: релиз одобрили давно, а спустя недели он продолжал капать
    новым атлетам — тем, для кого фича никогда не была новостью, они с самого
    начала застали её готовой. Кто зарегистрировался ДО одобрения — получает
    релиз как обычно; кто ПОСЛЕ — молча пропускается."""
    monkeypatch.setattr(announcements.asyncio, "sleep", AsyncMock())
    await _users(fresh_db, ADMIN_ID, 1)
    ann = _announcement()
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])
    await announcements.send_preview(FakeBot(), ann)
    await db_module.set_announcement_status(ann.key, announcements.STATUS_APPROVED)

    await _users(fresh_db, 2)
    # На секундную точность часов теста не полагаемся: явно отодвигаем
    # регистрацию нового атлета в будущее относительно момента одобрения,
    # который мог случиться в ту же секунду.
    await fresh_db.conn().execute(
        "UPDATE users SET created_at = '2099-01-01T00:00:00' WHERE telegram_id = 2"
    )
    await fresh_db.conn().commit()

    bot = FakeBot()
    await announcements.run_pending_announcements(bot)

    recipients = {chat_id for chat_id, _ in bot.sent if chat_id != ADMIN_ID}
    assert recipients == {1}, "новый атлет не должен получать релиз, вышедший до его регистрации"


async def test_pushes_off_means_no_announcement(fresh_db):
    await _users(fresh_db, 1, 2)
    await fresh_db.update_user(2, pushes_enabled=0)

    bot = FakeBot()
    await announcements.send_announcement(bot, _announcement())

    assert [chat_id for chat_id, _ in bot.sent] == [1]


async def test_blocked_user_drops_out_of_future_pushes(fresh_db):
    await _users(fresh_db, 1, 2)
    bot = FakeBot(fail_for={1: TelegramForbiddenError(method=None, message="blocked")})

    assert await announcements.send_announcement(bot, _announcement()) == (1, 1, 0)
    row = await fresh_db.get_user(1)
    assert row["pushes_enabled"] == 0
    # Остальные не остаются без релиза из-за одного удалённого чата.
    assert [chat_id for chat_id, _ in bot.sent] == [2]


async def test_failed_send_is_retried_on_the_next_start(fresh_db):
    """Не дошло — не отмечаем доставленным: следующая попытка попробует ещё раз."""
    await _users(fresh_db, 1, 2)
    ann = _announcement()
    first = FakeBot(fail_for={1: TelegramNetworkError(method=None, message="down")})

    assert await announcements.send_announcement(first, ann) == (1, 0, 1)

    second = FakeBot()
    assert await announcements.send_announcement(second, ann) == (1, 0, 0)
    assert [chat_id for chat_id, _ in second.sent] == [1]


# ---------- /announce ----------


async def test_announce_command_reshows_a_declined_release(fresh_db, monkeypatch):
    await _users(fresh_db, ADMIN_ID, 1)
    ann = _announcement()
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])
    await db_module.set_announcement_status(ann.key, announcements.STATUS_DECLINED)

    bot = FakeBot()
    await admin.cmd_announce(_message(ADMIN_ID, bot), )

    assert bot.texts_to(ADMIN_ID)[0] == ann.text
    assert await db_module.get_announcement_status(ann.key) == announcements.STATUS_PREVIEW


async def test_announce_command_reports_a_finished_release(fresh_db, monkeypatch):
    await _users(fresh_db, ADMIN_ID, 1)
    ann = _announcement()
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [ann])
    await announcements.send_announcement(FakeBot(), ann)
    await db_module.set_announcement_status(ann.key, announcements.STATUS_APPROVED)

    bot = FakeBot()
    message = _message(ADMIN_ID, bot)
    await admin.cmd_announce(message)

    assert "разослан целиком" in message.answer.await_args.args[0]
    assert bot.sent == []


async def test_announce_command_ignores_non_admin(fresh_db, monkeypatch):
    monkeypatch.setattr(announcements, "ANNOUNCEMENTS", [_announcement()])
    bot = FakeBot()
    message = _message(1, bot)

    await admin.cmd_announce(message)

    message.answer.assert_not_awaited()
    assert bot.sent == []


# ---------- тексты и кнопки релиза ----------


async def test_release_text_speaks_in_the_coach_voice():
    ann = announcements.RELEASE_AI_PROGRAMS_AND_VIDEO
    assert ann.text.startswith("ПРИВЕТ АТЛЕТ! ")
    assert len(ann.text) <= announcements.CAPTION_LIMIT


async def test_release_buttons_lead_into_the_two_features():
    """Кнопки анонса должны попадать в живые хендлеры, а не в никуда.

    Сверяем с исходником хендлеров: переименуют callback у входа в сборку
    программы — тест упадёт здесь, а не в проде через кнопку под релизом,
    которая молча ничего не делает.
    """
    callbacks = [data for _, data in announcements.RELEASE_AI_PROGRAMS_AND_VIDEO.buttons]
    assert callbacks == ["ann:buildprog", "ai:videohint"]

    source = (Path(__file__).resolve().parent.parent / "handlers" / "ai_trainer.py").read_text()
    for data in callbacks:
        assert f'F.data == "{data}"' in source


async def test_ai_actions_release_text_speaks_in_the_coach_voice():
    ann = announcements.RELEASE_AI_TRAINER_ACTIONS
    assert ann.text.startswith("ПРИВЕТ АТЛЕТ! ")
    assert len(ann.text) <= announcements.CAPTION_LIMIT


async def test_ai_actions_release_button_leads_into_the_trainer_chat():
    callbacks = [data for _, data in announcements.RELEASE_AI_TRAINER_ACTIONS.buttons]
    assert callbacks == ["menu:ai"]

    source = (Path(__file__).resolve().parent.parent / "handlers" / "ai_trainer.py").read_text()
    for data in callbacks:
        assert f'F.data == "{data}"' in source


async def test_ai_actions_release_only_advertises_tools_that_actually_exist():
    """Пуш обязан быть правдой (TONE_OF_VOICE.md) — примеры фраз должны бить в
    реально существующие write-инструменты тренера, а не в то, чего ещё нет."""
    import ai_trainer

    write_tools = set(ai_trainer._ACTION_TOOLS) | set(ai_trainer._UNDOABLE_TOOLS)
    assert {
        "log_bodyweight", "log_food", "create_exercise", "rename_exercise",
        "move_exercise_to_group", "archive_exercise", "copy_program",
        "rename_program", "merge_programs", "delete_program", "share_program",
    } <= write_tools
