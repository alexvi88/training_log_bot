"""IndexNow на старте: шлёт sitemap один раз на набор страниц, без ключа или
своего домена молчит, сбой сети старт не роняет."""

import config
import indexnow


def _setup(monkeypatch, tmp_path, key="0123456789abcdef", site="https://athletediary.app"):
    monkeypatch.setattr(config, "INDEXNOW_KEY", key)
    monkeypatch.setattr(config, "SITE_BASE_URL", site)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "training_log.db"))
    sent = []
    monkeypatch.setattr(indexnow, "_post", lambda body: sent.append(body) or 200)
    return sent


async def test_sends_once_per_sitemap(monkeypatch, tmp_path):
    sent = _setup(monkeypatch, tmp_path)
    assert await indexnow.ping_if_changed() is True
    assert await indexnow.ping_if_changed() is False
    assert len(sent) == 1
    body = sent[0]
    assert body["host"] == "athletediary.app"
    assert body["keyLocation"] == "https://athletediary.app/0123456789abcdef.txt"
    assert body["urlList"] and all(u.startswith("https://athletediary.app/") for u in body["urlList"])
    assert "https://athletediary.app/ru/exercises/romanian-deadlift/alternatives" in body["urlList"]


async def test_silent_without_key_or_own_domain(monkeypatch, tmp_path):
    sent = _setup(monkeypatch, tmp_path, key="")
    assert await indexnow.ping_if_changed() is False
    sent = _setup(monkeypatch, tmp_path, site="")
    monkeypatch.setattr(config, "site_base_url", lambda: None)
    assert await indexnow.ping_if_changed() is False
    assert sent == []


async def test_network_failure_is_logged_and_retried_next_start(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    def boom(body):
        raise OSError("down")

    monkeypatch.setattr(indexnow, "_post", boom)
    assert await indexnow.ping_if_changed() is False
    sent = []
    monkeypatch.setattr(indexnow, "_post", lambda body: sent.append(body) or 200)
    assert await indexnow.ping_if_changed() is True and len(sent) == 1
