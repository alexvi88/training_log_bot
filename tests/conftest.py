import asyncio
import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db  # noqa: E402


@pytest.fixture(autouse=True)
def _support_media_dir(tmp_path, monkeypatch):
    """Фото реплик поддержки (/feedback, /support/messages) пишутся на диск —
    в тестах во временный каталог, а не в боевой /data."""
    monkeypatch.setattr(config, "SUPPORT_MEDIA_DIR", str(tmp_path / "support_media"))


@pytest.fixture(autouse=True)
def _no_finish_slides(monkeypatch):
    """Альбом слайдов после «Завершить» рендерится секундами в фоне — тестам
    завершения он не нужен и сыпал бы задачами в чужие моки бота. Свои тесты
    (tests/test_finish_slides.py) включают его явно."""
    monkeypatch.setattr(config, "FINISH_SLIDES_ENABLED", False)


@pytest.fixture(autouse=True)
def _fresh_guest_limits():
    """Потолки гостевых аккаунтов (POST /v1/auth/guest) — счётчики в памяти
    процесса, а все тесты воркера ходят с одного адреса: без сброса шестой
    тест с гостем получал бы 429 от чужих."""
    import api_v1

    api_v1.reset_guest_limits()
    yield
    api_v1.reset_guest_limits()


@pytest.fixture(autouse=True)
def _fresh_bot_username(monkeypatch):
    """handlers.sharing кэширует username бота на весь процесс — в проде он
    один, а в тестах у каждого свой подставной бот («testbot», «TrainLogBot»…).
    Без сброса тест получал имя бота того, кто первым дёрнул кэш в этом
    воркере, и ссылка визитки разъезжалась через раз под -n auto."""
    from handlers import sharing

    monkeypatch.setattr(sharing, "_bot_username", None)


@pytest.fixture(autouse=True)
def _fresh_ai_busy():
    """handlers.ai_trainer._busy — замок «тренер уже отвечает» на весь процесс.
    Тест, который не дождался своего finally (фоновая задача альбома, брошенный
    стрим), оставлял там user_id, и следующий тест того же воркера с тем же
    пользователем падал на `_busy == set()` — красный CI на main через раз."""
    from handlers import ai_trainer

    ai_trainer._busy.clear()
    yield
    ai_trainer._busy.clear()


@pytest_asyncio.fixture
async def fresh_db():
    """A throwaway in-memory DB, fully migrated/seeded like the real one."""
    # db._write_lock is created at import time and binds itself to the loop of
    # the first *contended* acquire. The bot runs one loop for its lifetime, but
    # every test gets a fresh one — so a test that actually exercises
    # concurrency would bind the lock and make the next such test fail with
    # "bound to a different event loop". A new lock per test keeps that
    # module-level state from leaking across loops.
    db._write_lock = asyncio.Lock()
    await db.init_db(":memory:")
    try:
        yield db
    finally:
        await db.close_db()


@pytest_asyncio.fixture
async def user_id(fresh_db):
    row = await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    return row["telegram_id"]
