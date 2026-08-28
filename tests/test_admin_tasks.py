"""AI-trainer cost logging (db.cost_events) and the admin daily cost report."""

import asyncio
import datetime as dt
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

import admin_tasks
import config


async def test_log_cost_event_and_get_llm_cost_breakdown_groups_by_model(fresh_db, user_id):
    db = fresh_db
    today = db.now_iso()[:10]
    await db.log_cost_event(user_id, "llm_call", model="grok-4-1-fast", prompt_tokens=100, completion_tokens=50)
    await db.log_cost_event(user_id, "llm_call", model="grok-4-1-fast", prompt_tokens=200, completion_tokens=80)
    await db.log_cost_event(user_id, "llm_call", model="grok-4.20-multi-agent", prompt_tokens=10, completion_tokens=5)

    breakdown = await db.get_llm_cost_breakdown(today)

    assert breakdown["grok-4-1-fast"] == {
            "calls": 2, "prompt_tokens": 300, "completion_tokens": 130,
            "cached_tokens": 0, "reasoning_tokens": 0,
        }
    assert breakdown["grok-4.20-multi-agent"] == {
            "calls": 1, "prompt_tokens": 10, "completion_tokens": 5,
            "cached_tokens": 0, "reasoning_tokens": 0,
        }


async def test_get_llm_cost_breakdown_ignores_other_days_and_event_types(fresh_db, user_id):
    db = fresh_db
    await db.log_cost_event(user_id, "transcription", model=config.OPENAI_TRANSCRIBE_MODEL)
    await db.conn().execute(
        "INSERT INTO cost_events (user_id, event_type, model, prompt_tokens, completion_tokens, created_at) "
        "VALUES (?, 'llm_call', 'grok-4-1-fast', 100, 50, ?)",
        (user_id, "2020-01-01T10:00:00"),
    )
    await db.conn().commit()

    today = db.now_iso()[:10]
    assert await db.get_llm_cost_breakdown(today) == {}
    assert await db.get_llm_cost_breakdown("2020-01-01") == {
        "grok-4-1-fast": {
            "calls": 1, "prompt_tokens": 100, "completion_tokens": 50,
            "cached_tokens": 0, "reasoning_tokens": 0,
        }
    }


async def test_get_transcription_count(fresh_db, user_id):
    db = fresh_db
    today = db.now_iso()[:10]
    assert await db.get_transcription_count(today) == 0

    await db.log_cost_event(user_id, "transcription", model=config.OPENAI_TRANSCRIBE_MODEL)
    await db.log_cost_event(user_id, "transcription", model=config.OPENAI_TRANSCRIBE_MODEL)
    await db.log_cost_event(user_id, "llm_call", model="grok-4-1-fast", prompt_tokens=1, completion_tokens=1)

    assert await db.get_transcription_count(today) == 2


async def test_prune_old_cost_events_drops_only_stale_rows(fresh_db, user_id):
    db = fresh_db
    old_date = (dt.date.today() - dt.timedelta(days=200)).isoformat() + "T10:00:00"
    await db.conn().execute(
        "INSERT INTO cost_events (user_id, event_type, model, prompt_tokens, completion_tokens, created_at) "
        "VALUES (?, 'llm_call', 'grok-4-1-fast', 1, 1, ?)",
        (user_id, old_date),
    )
    await db.conn().commit()
    await db.log_cost_event(user_id, "llm_call", model="grok-4-1-fast", prompt_tokens=1, completion_tokens=1)

    deleted = await db.prune_old_cost_events(90)

    assert deleted == 1
    today = db.now_iso()[:10]
    assert await db.get_llm_cost_breakdown(today) == {
        "grok-4-1-fast": {
            "calls": 1, "prompt_tokens": 1, "completion_tokens": 1,
            "cached_tokens": 0, "reasoning_tokens": 0,
        }
    }


def test_llm_cost_prices_by_model_with_default_fallback():
    breakdown = {
        "grok-4-1-fast": {
            "calls": 2, "prompt_tokens": 1000, "completion_tokens": 1000,
            "cached_tokens": 0, "reasoning_tokens": 0,
        },
        "some-unpriced-model": {
            "calls": 1, "prompt_tokens": 1000, "completion_tokens": 1000,
            "cached_tokens": 0, "reasoning_tokens": 0,
        },
    }

    cost, calls, tokens = admin_tasks._llm_cost(breakdown)

    inp, out = config.LLM_PRICES_USD_PER_1K["grok-4-1-fast"]
    default_inp, default_out = config.DEFAULT_LLM_PRICE_USD_PER_1K
    expected = (inp + out) + (default_inp + default_out)
    assert cost == pytest.approx(expected)
    assert calls == 3
    assert tokens == 4000


async def test_daily_trained_users_lists_who_and_how_many(fresh_db, user_id):
    db = fresh_db
    other = await db.get_or_create_user(telegram_id=222, username=None)
    today = db.now_iso()[:10]
    started = f"{today}T09:00:00"
    finished = f"{today}T10:00:00"
    await db.create_finished_workout(user_id, started, finished)
    await db.create_finished_workout(user_id, started, finished)
    await db.create_finished_workout(other["telegram_id"], started, finished)

    rows = await db.daily_trained_users(today)

    report = admin_tasks._format_trained_users(rows)
    assert "@tester (2)" in report
    assert "└ 222\n" in report or report.endswith("└ 222")


async def test_build_cost_report_includes_llm_and_transcription_lines(fresh_db, user_id):
    db = fresh_db
    today = db.now_iso()[:10]
    await db.log_cost_event(user_id, "llm_call", model="grok-4-1-fast", prompt_tokens=1000, completion_tokens=1000)
    await db.log_cost_event(user_id, "transcription", model=config.OPENAI_TRANSCRIBE_MODEL)

    report = await admin_tasks._build_cost_report(today)

    assert "LLM-вызовов: 1" in report
    assert "grok-4-1-fast: 1" in report
    assert "Голосовых распознано: 1" in report
    assert "Итого расходы" in report


async def test_build_cost_report_omits_transcription_line_when_none(fresh_db, user_id):
    db = fresh_db
    today = db.now_iso()[:10]
    await db.log_cost_event(user_id, "llm_call", model="grok-4-1-fast", prompt_tokens=1, completion_tokens=1)

    report = await admin_tasks._build_cost_report(today)

    assert "Голосовых" not in report


@pytest.mark.asyncio
async def test_daily_report_no_longer_prunes_share_cards_itself(fresh_db, monkeypatch):
    """Отчёт админу больше не тащит за собой ретенш-чистку (см.
    test_the_retention_cleanup_runs_on_its_own) — она вынесена в отдельную
    задачу, чтобы не зависеть от ADMIN_ID и доставки самого отчёта."""
    old = (dt.datetime.now() - dt.timedelta(days=config.SHARED_ITEMS_RETENTION_DAYS + 1))
    stale = await fresh_db.create_shared_item(1, "routine", "{}")
    await fresh_db.conn().execute(
        "UPDATE shared_items SET created_at = ? WHERE token = ?",
        (old.isoformat(timespec="seconds"), stale),
    )
    await fresh_db.conn().commit()

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_document = AsyncMock()
    monkeypatch.setattr(config, "ADMIN_ID", 1)

    await admin_tasks._send_daily_report(bot, backup_path=None)

    assert await fresh_db.get_shared_item(stale) is not None


@pytest.mark.asyncio
async def test_the_retention_cleanup_runs_on_its_own(fresh_db, monkeypatch):
    """Ретенш-чистка (cost_events/user_events/limit_acks/shared_items) живёт
    отдельной задачей, а не внутри отчёта админу: там она стояла за
    `bot.send_message`, то есть не выполнялась вовсе без ADMIN_ID и пропадала
    в тот день, когда админ заблокировал бота."""
    monkeypatch.setattr(config, "ADMIN_ID", None)
    old = (dt.datetime.now() - dt.timedelta(days=config.SHARED_ITEMS_RETENTION_DAYS + 1))
    fresh = await fresh_db.create_shared_item(1, "routine", "{}")
    stale = await fresh_db.create_shared_item(1, "routine", "{}")
    await fresh_db.conn().execute(
        "UPDATE shared_items SET created_at = ? WHERE token = ?",
        (old.isoformat(timespec="seconds"), stale),
    )
    await fresh_db.conn().commit()

    # Один проход и выход: задача бесконечная, а тело идёт ПОСЛЕ sleep (как и в
    # run_daily_admin_jobs — ждём назначенный час, потом работаем), так что
    # первый sleep пропускаем, а на втором обрываем цикл.
    calls = 0

    async def _stop(_seconds):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(admin_tasks.asyncio, "sleep", _stop)
    with pytest.raises(asyncio.CancelledError):
        await admin_tasks.run_retention_cleanup_job()

    assert await fresh_db.get_shared_item(fresh) is not None
    assert await fresh_db.get_shared_item(stale) is None


async def test_disk_backup_is_created_next_to_the_db(fresh_db, tmp_path, monkeypatch):
    """Регрессия: единственный бэкап уходил документом в личку ADMIN_ID — если
    админ потерян/сменился, бэкапов не остаётся вовсе. Теперь копия на диске
    не зависит от Telegram и от ADMIN_ID."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "training_log.db"))
    backup_dir = admin_tasks._backup_dir()

    path = await admin_tasks._rotate_disk_backup()

    assert os.path.exists(path)
    assert os.path.dirname(path) == backup_dir


def test_prune_stale_backups_keeps_only_the_newest_n(tmp_path):
    backup_dir = str(tmp_path)
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        open(os.path.join(backup_dir, f"training_log_backup_{day}.db"), "w").close()

    admin_tasks._prune_stale_backups(backup_dir, keep=2)

    files = sorted(os.listdir(backup_dir))
    assert files == ["training_log_backup_2026-01-02.db", "training_log_backup_2026-01-03.db"]


def test_prune_stale_backups_does_nothing_when_keep_is_zero(tmp_path):
    backup_dir = str(tmp_path)
    open(os.path.join(backup_dir, "training_log_backup_2026-01-01.db"), "w").close()

    admin_tasks._prune_stale_backups(backup_dir, keep=0)

    assert os.listdir(backup_dir) == ["training_log_backup_2026-01-01.db"]


async def test_startup_catches_up_a_backup_that_missed_its_daily_window(
    fresh_db, tmp_path, monkeypatch
):
    """Регрессия с прода (26.4 часа без копии): расписание живёт только в памяти
    процесса, и рестарт после ADMIN_REPORT_HOUR отправлял следующий запуск на
    сутки вперёд — пропущенный день не догонял никто."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "training_log.db"))
    monkeypatch.setattr(config, "BACKUP_CATCHUP_HOURS", 24)
    backup_dir = admin_tasks._backup_dir()
    os.makedirs(backup_dir, exist_ok=True)
    stale_path = os.path.join(backup_dir, "training_log_backup_2020-01-01.db")
    open(stale_path, "w").close()
    old_mtime = dt.datetime.now().timestamp() - 26 * 3600
    os.utime(stale_path, (old_mtime, old_mtime))

    await admin_tasks._catch_up_missed_backup()

    today = os.path.join(backup_dir, f"training_log_backup_{dt.date.today().isoformat()}.db")
    assert os.path.exists(today), "пропущенное окно должно догоняться на старте"


async def test_startup_makes_the_very_first_backup_on_a_fresh_disk(
    fresh_db, tmp_path, monkeypatch
):
    """Бэкапов нет вовсе — новый инстанс не должен жить без единой копии до
    первого ADMIN_REPORT_HOUR."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "training_log.db"))

    await admin_tasks._catch_up_missed_backup()

    backup_dir = admin_tasks._backup_dir()
    assert os.listdir(backup_dir), "на свежем диске копия нужна сразу"


async def test_startup_does_not_duplicate_a_fresh_backup(fresh_db, tmp_path, monkeypatch):
    """Само по себе самоограничивается: первый догон обнуляет возраст, и десять
    рестартов подряд не должны сделать десять копий."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "training_log.db"))
    monkeypatch.setattr(config, "BACKUP_CATCHUP_HOURS", 24)
    await admin_tasks._rotate_disk_backup()

    rotate = AsyncMock()
    monkeypatch.setattr(admin_tasks, "_rotate_disk_backup", rotate)
    await admin_tasks._catch_up_missed_backup()

    rotate.assert_not_called()


async def test_catch_up_failure_does_not_stop_the_daily_job(fresh_db, tmp_path, monkeypatch):
    """Диск полон, права слетели — суточная джоба всё равно обязана встать на
    расписание, а не упасть на старте вместе с догоном."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "training_log.db"))
    monkeypatch.setattr(
        admin_tasks, "_rotate_disk_backup", AsyncMock(side_effect=OSError("disk full"))
    )

    await admin_tasks._catch_up_missed_backup()  # не должно бросить


async def test_backup_staleness_check_alerts_admin_when_backup_is_old(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "training_log.db"))
    monkeypatch.setattr(config, "ADMIN_ID", 42)
    monkeypatch.setattr(config, "BACKUP_STALE_ALERT_HOURS", 26)
    backup_dir = admin_tasks._backup_dir()
    os.makedirs(backup_dir, exist_ok=True)
    stale_path = os.path.join(backup_dir, "training_log_backup_2020-01-01.db")
    open(stale_path, "w").close()
    old_mtime = dt.datetime.now().timestamp() - 30 * 3600
    os.utime(stale_path, (old_mtime, old_mtime))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    async def _stop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(admin_tasks.asyncio, "sleep", _stop)
    with pytest.raises(asyncio.CancelledError):
        await admin_tasks.run_backup_staleness_check(bot)

    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == 42
    today = os.path.join(backup_dir, f"training_log_backup_{dt.date.today().isoformat()}.db")
    assert os.path.exists(today), "часовая проверка обязана не только алертить, но и чинить"
    text = bot.send_message.await_args.kwargs["text"]
    assert "сделал сам" in text


async def test_backup_staleness_check_reports_a_failed_repair(fresh_db, tmp_path, monkeypatch):
    """Копия не пишется на диск — это другая беда, чем «джоба не проснулась», и
    админ должен увидеть именно её, с текстом ошибки."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "training_log.db"))
    monkeypatch.setattr(config, "ADMIN_ID", 42)
    monkeypatch.setattr(config, "BACKUP_STALE_ALERT_HOURS", 26)
    backup_dir = admin_tasks._backup_dir()
    os.makedirs(backup_dir, exist_ok=True)
    stale_path = os.path.join(backup_dir, "training_log_backup_2020-01-01.db")
    open(stale_path, "w").close()
    old_mtime = dt.datetime.now().timestamp() - 30 * 3600
    os.utime(stale_path, (old_mtime, old_mtime))
    monkeypatch.setattr(
        admin_tasks, "_rotate_disk_backup", AsyncMock(side_effect=OSError("disk full"))
    )

    bot = MagicMock()
    bot.send_message = AsyncMock()

    async def _stop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(admin_tasks.asyncio, "sleep", _stop)
    with pytest.raises(asyncio.CancelledError):
        await admin_tasks.run_backup_staleness_check(bot)

    text = bot.send_message.await_args.kwargs["text"]
    assert "disk full" in text
    assert "не вышло" in text


async def test_backup_staleness_check_stays_quiet_for_a_fresh_backup(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "training_log.db"))
    monkeypatch.setattr(config, "ADMIN_ID", 42)
    await admin_tasks._rotate_disk_backup()

    bot = MagicMock()
    bot.send_message = AsyncMock()

    async def _stop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(admin_tasks.asyncio, "sleep", _stop)
    with pytest.raises(asyncio.CancelledError):
        await admin_tasks.run_backup_staleness_check(bot)

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_oauth_purge_runs_on_its_own(fresh_db, user_id, monkeypatch):
    """Подключение коннектора, брошенное на полпути, не гасит за собой ничего:
    заявка на согласие и код связывания остаются лежать.

    Прополка живёт отдельной задачей, а не внутри отчёта админу: там она стояла
    за `bot.send_message`, то есть пропадала в тот день, когда админ заблокировал
    бота, — и не запускалась вовсе, если ADMIN_ID не задан.
    """
    monkeypatch.setattr(config, "ADMIN_ID", None)
    await fresh_db.create_oauth_consent_request(
        request_id="stale",
        client_id="client",
        redirect_uri="https://claude.ai/callback",
        redirect_uri_provided_explicitly=True,
        code_challenge="x",
        scopes="[]",
        resource=None,
        state=None,
        expires_at=0.0,
    )
    live = await fresh_db.issue_oauth_link_code(user_id, 300)

    # Один проход и выход: задача бесконечная, а проверяем мы её первый круг.
    async def _stop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(admin_tasks.asyncio, "sleep", _stop)
    with pytest.raises(asyncio.CancelledError):
        await admin_tasks.run_oauth_purge_job()

    assert await fresh_db.get_oauth_consent_request("stale") is None
    # Живой код при этом на месте: прополка не должна ронять подключение,
    # начатое минуту назад.
    cur = await fresh_db.conn().execute("SELECT code FROM oauth_link_codes")
    assert [row["code"] for row in await cur.fetchall()] == [live]
